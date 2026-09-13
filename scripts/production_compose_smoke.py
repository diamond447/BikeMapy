#!/usr/bin/env python3
"""Exercise the production Compose contract with a disposable local stack."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "compose.production.yml"
PUBLIC_HOST = "compose-smoke.example.invalid"
DIND_IMAGE = (
    "docker:29.8.0-dind@sha256:5efed980cba3fc126cf54e21a5a6ff8849d05b6e0623d6e7612f48e9cd6cd17e"
)


def run(command: list[str], *, capture: bool = False) -> str:
    print("$", " ".join(command), flush=True)
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def wait_for_health(compose: list[str], service: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        container = run([*compose, "ps", "-q", service], capture=True)
        if container:
            status = run(
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
                capture=True,
            )
            if status == "healthy":
                return
        time.sleep(2)
    raise RuntimeError(
        f"Timed out waiting for {service} health (last status: {status or 'missing'})"
    )


def trigger_unexpected_exit(container: str, service: str) -> None:
    """Stop the service process from inside the container, like a crash."""
    commands: dict[str, list[str]] = {
        "db": [
            "su",
            "postgres",
            "-c",
            'pg_ctl -D "$PGDATA" -m immediate stop',
        ],
        "redis": ["redis-cli", "shutdown", "nosave"],
        "proxy": ["nginx", "-s", "stop"],
    }
    if service in commands:
        command = ["docker", "exec", container, *commands[service]]
        print("$", " ".join(command), flush=True)
        subprocess.run(command, cwd=ROOT, check=False)
        return

    marker = {
        "backend": "gunicorn",
        "worker": "config worker",
        "beat": "config beat",
    }[service]
    python = (
        "import os, signal\n"
        "targets = []\n"
        "for pid in os.listdir('/proc'):\n"
        "    if pid.isdigit() and int(pid) > 1 and int(pid) != os.getpid():\n"
        "        try:\n"
        "            command = open('/proc/' + pid + '/cmdline', 'rb').read()\n"
        "            command = command.replace(b'\\0', b' ').decode()\n"
        "        except OSError:\n"
        "            continue\n"
        f"        if {marker!r} in command: targets.append(int(pid))\n"
        "if not targets: raise SystemExit('service process not found')\n"
        "os.kill(targets[0], signal.SIGKILL)"
    )
    command = [
        "docker",
        "exec",
        container,
        "uv",
        "run",
        "--locked",
        "--no-dev",
        "python",
        "-c",
        python,
    ]
    print("$ docker exec", container, "<process scan>", flush=True)
    subprocess.run(command, cwd=ROOT, check=False)


def verify_restart_after_failure(compose: list[str], service: str, timeout: float = 90) -> None:
    """Stop a service unexpectedly and verify its restart policy recovers it."""
    container = run([*compose, "ps", "-q", service], capture=True)
    if not container:
        raise RuntimeError(f"Cannot test restart policy: {service} has no container")
    before = run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.RestartCount}}",
            container,
        ],
        capture=True,
    )
    # A direct `docker kill` is treated as an operator stop by the daemon and
    # intentionally suppresses an `unless-stopped` restart. Stop the service
    # process from inside its container to model an unexpected failure.
    trigger_unexpected_exit(container, service)
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        last_state = run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}} {{.RestartCount}}",
                container,
            ],
            capture=True,
        )
        running, restart_count = last_state.split()
        if running == "true" and int(restart_count) > int(before):
            return
        time.sleep(2)
    raise RuntimeError(
        f"{service} did not recover after failure (last state: {last_state or 'missing'})"
    )


def wait_for_ready(url: str, host: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    request = urllib.request.Request(url, headers={"Host": host})
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                return
        except (OSError, urllib.error.HTTPError) as exc:
            last_error = str(exc)
            time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_daemon_restart_smoke(env_file: Path, image: str, proxy_port: int) -> None:
    """Rehearse a Docker daemon restart in an isolated privileged DinD host."""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker is required for the daemon restart smoke")

    daemon = f"bikemapy-daemon-smoke-{os.getpid()}"
    project = f"bikemapy-daemon-recovery-{os.getpid()}"
    volume = f"{daemon}-data"
    daemon_image = f"bikemapy-daemon-backend:{os.getpid()}"
    daemon_proxy_port = free_port()
    inner_env = env_file.with_name("daemon-runtime.env")
    inner_env.write_text(
        env_file.read_text()
        .replace(f"BIKEMAPY_BACKEND_IMAGE={image}", f"BIKEMAPY_BACKEND_IMAGE={daemon_image}")
        .replace(f"BIKEMAPY_ENV_FILE={env_file}", "BIKEMAPY_ENV_FILE=/run/runtime.env")
        .replace(f"PROXY_PORT={proxy_port}", f"PROXY_PORT={daemon_proxy_port}"),
        encoding="utf-8",
    )
    compose = [
        docker,
        "exec",
        daemon,
        "docker",
        "compose",
        "--project-name",
        project,
        "--env-file",
        "/run/runtime.env",
        "-f",
        "/workspace/deploy/compose.production.yml",
    ]

    try:
        run(
            [
                docker,
                "run",
                "--detach",
                "--privileged",
                "--name",
                daemon,
                "--volume",
                f"{volume}:/var/lib/docker",
                "--volume",
                f"{ROOT}:/workspace:ro",
                "--volume",
                f"{inner_env}:/run/runtime.env:ro",
                DIND_IMAGE,
                "--host=unix:///var/run/docker.sock",
                "--tls=false",
            ]
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = subprocess.run(
                [docker, "exec", daemon, "docker", "info"],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError("Timed out waiting for isolated Docker daemon")

        run(
            [
                docker,
                "exec",
                "--workdir",
                "/workspace",
                daemon,
                "docker",
                "build",
                "--tag",
                daemon_image,
                "--file",
                "/workspace/docker/backend.Dockerfile",
                "/workspace",
            ]
        )
        run([*compose, "up", "-d", "db", "redis"])
        run(
            [
                *compose,
                "run",
                "--rm",
                "backend",
                "uv",
                "run",
                "--locked",
                "--no-dev",
                "python",
                "backend/manage.py",
                "migrate",
                "--noinput",
            ]
        )
        run([*compose, "up", "-d", "backend", "worker", "beat", "proxy"])
        wait_for_nested_proxy(compose, docker, daemon, daemon_image)

        print("$ docker restart", daemon, "(isolated daemon host)", flush=True)
        run([docker, "restart", daemon])
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = subprocess.run(
                [docker, "exec", daemon, "docker", "info"],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError("Isolated Docker daemon did not return after restart")

        for service in ("db", "redis", "backend", "worker", "beat", "proxy"):
            wait_for_nested_running(compose, docker, daemon, service)
        wait_for_nested_proxy(compose, docker, daemon, daemon_image)
        print("Disposable Docker daemon restart smoke passed.")
    finally:
        subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], cwd=ROOT, check=False)
        subprocess.run([docker, "rm", "--force", daemon], cwd=ROOT, check=False)
        subprocess.run([docker, "volume", "rm", "--force", volume], cwd=ROOT, check=False)


def wait_for_nested_running(compose: list[str], docker: str, daemon: str, service: str) -> None:
    """Wait for one service in the nested daemon to be running."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = subprocess.run(
            [*compose, "ps", "-q", service],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        container = result.stdout.strip()
        if container:
            state = subprocess.run(
                [
                    docker,
                    "exec",
                    daemon,
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    container,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if state.stdout.strip() == "true":
                return
        time.sleep(2)
    raise RuntimeError(f"{service} did not return after isolated Docker daemon restart")


def wait_for_nested_proxy(compose: list[str], docker: str, daemon: str, image: str) -> None:
    """Probe Nginx inside its own nested container network namespace."""
    deadline = time.monotonic() + 180
    probe = (
        "import urllib.request; "
        "request=urllib.request.Request("
        "'http://127.0.0.1/health/ready/', "
        f"headers={{'Host': {PUBLIC_HOST!r}}}); "
        "response=urllib.request.urlopen(request, timeout=5); "
        "assert response.status == 200"
    )
    while time.monotonic() < deadline:
        result = subprocess.run(
            [*compose, "ps", "-q", "proxy"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        proxy = result.stdout.strip()
        if proxy:
            probe_result = subprocess.run(
                [
                    docker,
                    "exec",
                    daemon,
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    f"container:{proxy}",
                    image,
                    "uv",
                    "run",
                    "--locked",
                    "--no-dev",
                    "python",
                    "-c",
                    probe,
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            if probe_result.returncode == 0:
                return
        time.sleep(2)
    raise RuntimeError("Nested proxy did not become ready after daemon restart")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--daemon-restart",
        action="store_true",
        help="also run the isolated Docker-in-Docker daemon restart rehearsal",
    )
    args = parser.parse_args()
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker is required for the production Compose smoke")

    project = f"bikemapy-production-smoke-{os.getpid()}"
    image = f"bikemapy-production-smoke:{os.getpid()}"
    proxy_port = free_port()
    with tempfile.TemporaryDirectory(prefix="bikemapy-production-smoke-") as directory:
        env_file = Path(directory) / "runtime.env"
        env_file.write_text(
            "\n".join(
                [
                    f"BIKEMAPY_BACKEND_IMAGE={image}",
                    f"BIKEMAPY_ENV_FILE={env_file}",
                    "POSTGRES_DB=bikemapy",
                    "POSTGRES_USER=bikemapy",
                    "POSTGRES_PASSWORD=compose-smoke-only",
                    "POSTGRES_HOST=db",
                    "POSTGRES_PORT=5432",
                    "DJANGO_DEBUG=false",
                    "DJANGO_SECRET_KEY=compose-smoke-only-secret-9f7b3c1d5e8a2b4c6d0f9e1a3b5c7d9e2f4a6b8c0d2e4f6a8b1c3d5e7f9",
                    f"DJANGO_ALLOWED_HOSTS={PUBLIC_HOST},backend",
                    "DJANGO_DATABASE_ENGINE=django.contrib.gis.db.backends.postgis",
                    "DJANGO_CACHE_URL=redis://redis:6379/1",
                    "CELERY_BROKER_URL=redis://redis:6379/0",
                    "CELERY_RESULT_BACKEND=redis://redis:6379/0",
                    f"PUBLIC_HOST={PUBLIC_HOST}",
                    "USE_X_FORWARDED_HOST=false",
                    "CORS_ALLOWED_ORIGINS=https://compose-smoke.example.invalid",
                    "GPX_REDISTRIBUTION_APPROVED=false",
                    "GPX_INTERNAL_REDIRECT=false",
                    "BIKEFORUM_DNS_CHECK=false",
                    "BIKEFORUM_CHECK_SOURCES=false",
                    "REPORT_TURNSTILE_SECRET_KEY=",
                    "REPORT_RATE_LIMIT_HMAC_SECRET=",
                    "REPORT_CLIENT_IP_MODE=direct",
                    f"PROXY_PORT={proxy_port}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        compose = [
            docker,
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(env_file),
            "-f",
            str(COMPOSE_FILE),
        ]
        try:
            run(
                [
                    docker,
                    "build",
                    "--tag",
                    image,
                    "--file",
                    str(ROOT / "docker/backend.Dockerfile"),
                    ".",
                ]
            )
            run([*compose, "up", "-d", "db", "redis"])
            run(
                [
                    *compose,
                    "run",
                    "--rm",
                    "backend",
                    "uv",
                    "run",
                    "--locked",
                    "--no-dev",
                    "python",
                    "backend/manage.py",
                    "migrate",
                    "--noinput",
                ]
            )
            run([*compose, "up", "-d", "backend", "worker", "beat", "proxy"])
            wait_for_health(compose, "backend")
            wait_for_ready(f"http://127.0.0.1:{proxy_port}/health/ready/", PUBLIC_HOST)
            for service in ("db", "redis", "backend", "worker", "beat", "proxy"):
                verify_restart_after_failure(compose, service)
                if service in {"db", "redis", "backend"}:
                    wait_for_health(compose, "backend")
                if service == "proxy":
                    wait_for_ready(f"http://127.0.0.1:{proxy_port}/health/ready/", PUBLIC_HOST)
            print(
                "Production Compose smoke passed: readiness, proxy, and all service "
                "restart policies recovered after failure."
            )
            if args.daemon_restart:
                run_daemon_restart_smoke(env_file, image, proxy_port)
        finally:
            try:
                run([*compose, "down", "--volumes", "--remove-orphans"])
            finally:
                subprocess.run([docker, "image", "rm", "--force", image], cwd=ROOT, check=False)


if __name__ == "__main__":
    main()
