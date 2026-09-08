#!/usr/bin/env python3
"""Exercise the production Compose contract with a disposable local stack."""

from __future__ import annotations

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


def main() -> None:
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
                    "DJANGO_SECRET_KEY=compose-smoke-only-secret",
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
            run([*compose, "up", "-d", "backend", "proxy"])
            wait_for_health(compose, "backend")
            wait_for_ready(f"http://127.0.0.1:{proxy_port}/health/ready/", PUBLIC_HOST)
            proxy_container = run([*compose, "ps", "-q", "proxy"], capture=True)
            if not proxy_container:
                raise RuntimeError("Proxy container did not remain running")
            proxy_running = run(
                ["docker", "inspect", "--format", "{{.State.Running}}", proxy_container],
                capture=True,
            )
            if proxy_running != "true":
                raise RuntimeError("Proxy container is not running")
            print("Production Compose smoke passed: DEBUG=False readiness and healthy proxy.")
        finally:
            try:
                run([*compose, "down", "--volumes", "--remove-orphans"])
            finally:
                subprocess.run([docker, "image", "rm", "--force", image], cwd=ROOT, check=False)


if __name__ == "__main__":
    main()
