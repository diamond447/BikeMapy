#!/usr/bin/env python3
"""Smoke-test Django Admin static assets through the production proxy.

The script builds the current backend image, runs the production Compose file
with disposable PostGIS/Redis services, and requests the assets from Nginx.
It is intentionally independent of a registry image so CI tests the image
that was just built, including its collectstatic layer.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_COMPOSE = ROOT / "deploy" / "compose.production.yml"
REHEARSAL_COMPOSE = ROOT / "deploy" / "compose.rehearsal.yml"


def run(command: list[str], *, capture: bool = False, check: bool = True) -> str:
    print("$", " ".join(command), flush=True)
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=check,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Stack:
    def __init__(self, project: str, env_file: Path) -> None:
        self.project = project
        self.env_file = env_file
        self.base = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            str(env_file),
            "-f",
            str(PRODUCTION_COMPOSE),
            "-f",
            str(REHEARSAL_COMPOSE),
        ]

    def compose(self, *args: str, capture: bool = False, check: bool = True) -> str:
        return run([*self.base, *args], capture=capture, check=check)


def write_runtime_env(path: Path, image: str) -> None:
    values = {
        "BIKEMAPY_BACKEND_IMAGE": image,
        "BIKEMAPY_ENV_FILE": str(path),
        "PROXY_PORT": str(free_port()),
        "POSTGRES_DB": "bikemapy",
        "POSTGRES_USER": "bikemapy",
        "POSTGRES_PASSWORD": "static-smoke-only",
        "DJANGO_DATABASE_ENGINE": "django.contrib.gis.db.backends.postgis",
        "DJANGO_SECRET_KEY": (
            "static-smoke-only-9f7b3c1d5e8a2b4c6d0f9e1a3b5c7d9e2f4a6b8c0d2e4f6a8b1c3d5e7f9"
        ),
        "DJANGO_DEBUG": "false",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,backend",
        "DJANGO_CACHE_URL": "redis://redis:6379/1",
        "CELERY_BROKER_URL": "redis://redis:6379/0",
        "CELERY_RESULT_BACKEND": "redis://redis:6379/0",
        "GITHUB_OWNER_IDS": "",
        "GPX_REDISTRIBUTION_APPROVED": "false",
        "GPX_INTERNAL_REDIRECT": "false",
    }
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


def wait_for(url: str, *, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                return
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            last_error = str(exc)
            time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def fetch(url: str) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.headers.get_content_type(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get_content_type(), exc.read()


def assert_asset(base: str, path: str, content_type: str, marker: bytes) -> dict[str, object]:
    status, actual_type, body = fetch(f"{base}{path}")
    if status != 200:
        raise RuntimeError(f"{path} returned HTTP {status}")
    if actual_type != content_type:
        raise RuntimeError(
            f"{path} returned Content-Type {actual_type!r}, expected {content_type!r}"
        )
    if marker not in body:
        raise RuntimeError(f"{path} did not contain the expected Django Admin marker")
    return {"path": path, "status": status, "content_type": actual_type, "bytes": len(body)}


def main() -> int:
    project = f"bikemapy-static-smoke-{os.getpid()}"
    image = f"bikemapy-static-smoke:{os.getpid()}"
    stack: Stack | None = None
    with tempfile.TemporaryDirectory(prefix="bikemapy-static-smoke-") as directory:
        env_file = Path(directory) / "runtime.env"
        write_runtime_env(env_file, image)
        stack = Stack(project, env_file)
        try:
            run(
                ["docker", "build", "-t", image, "-f", str(ROOT / "docker/backend.Dockerfile"), "."]
            )
            stack.compose("up", "-d", "db", "redis")
            stack.compose(
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
            )
            stack.compose("up", "-d", "backend", "proxy")
            proxy_port = stack.compose("port", "proxy", "80", capture=True).rsplit(":", 1)[1]
            base = f"http://127.0.0.1:{proxy_port}"
            wait_for(f"{base}/health/ready/")

            status, content_type, body = fetch(f"{base}/admin/login/")
            if status != 200 or b"BikeMapy owner administration" not in body:
                raise RuntimeError(f"/admin/login/ returned unexpected response: HTTP {status}")
            assets = [
                assert_asset(base, "/static/admin/css/base.css", "text/css", b"body"),
                assert_asset(
                    base, "/static/admin/js/nav_sidebar.js", "text/javascript", b"navSidebar"
                ),
            ]
            print(
                {
                    "proxy": base,
                    "admin_login": {"status": status, "content_type": content_type},
                    "assets": assets,
                }
            )
            return 0
        finally:
            if stack is not None:
                stack.compose("down", "--volumes", "--remove-orphans", check=False)
            run(["docker", "image", "rm", "-f", image], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
