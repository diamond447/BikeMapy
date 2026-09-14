#!/usr/bin/env python3
"""Run disposable deployment rollback and crawler-resumption rehearsals.

The rehearsal builds two local, content-addressed backend images, runs them
against disposable PostGIS and Redis services, and writes non-production JSON
evidence. It intentionally never contacts the real BikeForum or Mapy hosts.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_COMPOSE = ROOT / "deploy" / "compose.production.yml"
REHEARSAL_COMPOSE = ROOT / "deploy" / "compose.rehearsal.yml"


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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def image_id(image: str) -> str:
    return run(["docker", "image", "inspect", "--format", "{{.Id}}", image], capture=True)


def build_images(previous_ref: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory(prefix="bikemapy-previous-context-") as directory:
        previous_context = Path(directory)
        archive = subprocess.run(
            ["git", "archive", "--format=tar", previous_ref],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        import io

        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
            source.extractall(previous_context, filter="data")
        tag_suffix = str(os.getpid())
        previous = f"bikemapy-rehearsal:{tag_suffix}-previous"
        candidate = f"bikemapy-rehearsal:{tag_suffix}-candidate"
        run(
            [
                "docker",
                "build",
                "--label",
                f"org.opencontainers.image.revision={previous_ref}",
                "-t",
                previous,
                "-f",
                str(previous_context / "docker/backend.Dockerfile"),
                str(previous_context),
            ]
        )
        run(
            [
                "docker",
                "build",
                "--label",
                "org.opencontainers.image.revision=working-tree",
                "-t",
                candidate,
                "-f",
                str(ROOT / "docker/backend.Dockerfile"),
                str(ROOT),
            ]
        )
    previous_id = image_id(previous)
    candidate_id = image_id(candidate)
    if previous_id == candidate_id:
        raise RuntimeError("candidate and previous image IDs unexpectedly match")
    return {
        "previous_ref": previous_ref,
        "previous_image": previous,
        "previous_image_id": previous_id,
        "candidate_ref": "working-tree",
        "candidate_image": candidate,
        "candidate_image_id": candidate_id,
    }


def runtime_env(
    path: Path,
    image: str,
    *,
    forum_port: int | None = None,
    backup_dir: Path | None = None,
) -> None:
    synthetic_crawler = forum_port is not None
    origin = (
        f"http://host.docker.internal:{forum_port}" if synthetic_crawler else "http://localhost:1"
    )
    rehearsal_backup_dir = backup_dir or path.parent / "backup"
    rehearsal_backup_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "BIKEMAPY_BACKEND_IMAGE": image,
        "BIKEMAPY_ENV_FILE": str(path),
        "POSTGRES_DB": "bikemapy",
        "POSTGRES_USER": "bikemapy",
        "POSTGRES_PASSWORD": "rehearsal-only",
        "DJANGO_DATABASE_ENGINE": "django.contrib.gis.db.backends.postgis",
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_DEBUG": "false",
        "DJANGO_SECRET_KEY": (
            "launch-rehearsal-only-9f7b3c1d5e8a2b4c6d0f9e1a3b5c7d9e2f4a6b8c0d2e4f6a8b1c3d5e7f9"
        ),
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,backend",
        "DJANGO_CACHE_URL": "redis://redis:6379/1",
        "CELERY_BROKER_URL": "redis://redis:6379/0",
        "CELERY_RESULT_BACKEND": "redis://redis:6379/0",
        "BIKEFORUM_ALLOWED_ORIGINS": origin,
        "BIKEFORUM_INCREMENTAL_URL": f"{origin}/t/42",
        "REHEARSAL_FORUM_PORT": str(forum_port if forum_port is not None else ""),
        # The only enabled crawler runtime is this disposable local forum;
        # it serves checked-in fixtures and never contacts a real provider.
        # Keep all three production approval gates false in ordinary and
        # rollback rehearsals, where no synthetic forum is configured.
        "BIKEFORUM_CRAWL_ENABLED": str(synthetic_crawler).lower(),
        "BIKEFORUM_PROVIDER_AUTHORIZED": str(synthetic_crawler).lower(),
        "BIKEFORUM_OPERATOR_APPROVED": str(synthetic_crawler).lower(),
        "BIKEFORUM_DNS_CHECK": "false",
        "BIKEFORUM_CHECK_SOURCES": "false",
        "BIKEFORUM_LEASE_SECONDS": "5",
        "BIKEFORUM_PAGE_ATTEMPTS": "3",
        "GPX_REDISTRIBUTION_APPROVED": "false",
        "GPX_INTERNAL_REDIRECT": "false",
        "REHEARSAL_BACKUP_DIR": str(rehearsal_backup_dir),
    }
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


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

    def compose(self, *args: str, capture: bool = False) -> str:
        return run([*self.base, *args], capture=capture)

    def compose_unchecked(
        self, *args: str, capture: bool = False
    ) -> subprocess.CompletedProcess[str]:
        command = [*self.base, *args]
        print("$", " ".join(command), flush=True)
        return subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            text=True,
            capture_output=capture,
        )

    def stop(self) -> None:
        self.compose("down", "--volumes", "--remove-orphans")

    def backend_port(self) -> int:
        value = self.compose("port", "backend", "8000", capture=True)
        return int(value.rsplit(":", 1)[1])

    def backend_image_id(self) -> str:
        container = self.compose("ps", "-q", "backend", capture=True).splitlines()[-1]
        return run(["docker", "inspect", "--format", "{{.Image}}", container], capture=True)

    def backend_container_id(self) -> str:
        return self.compose("ps", "-q", "backend", capture=True).splitlines()[-1]


def wait_http(url: str, *, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                return
        except Exception as exc:  # noqa: BLE001 - retry until service readiness deadline
            last_error = str(exc)
            time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def wait_for(url: str, *, timeout: float = 90) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            # Rehearsal publishes the backend port directly instead of using
            # Nginx, so preserve the HTTPS scheme the trusted proxy provides.
            request = urllib.request.Request(url, headers={"X-Forwarded-Proto": "https"})
            with urllib.request.urlopen(request, timeout=3) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                return cast(dict[str, Any], json.loads(response.read()))
        except Exception as exc:  # noqa: BLE001 - retry until service readiness deadline
            last_error = str(exc)
            time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def api_json(base: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{base}{path}", headers={"X-Forwarded-Proto": "https"})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} for {path}")
        return cast(dict[str, Any], json.loads(response.read()))


def exec_backend(stack: Stack, *command: str, capture: bool = False) -> str:
    return stack.compose("exec", "-T", "backend", *command, capture=capture)


def migrate_and_start(stack: Stack, *, worker: bool) -> int:
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
    stack.compose("up", "-d", "--force-recreate", "backend", *("worker",) if worker else ())
    port = stack.backend_port()
    wait_for(f"http://127.0.0.1:{port}/health/ready/")
    return port


def seed_catalogue(stack: Stack) -> None:
    exec_backend(
        stack,
        "uv",
        "run",
        "--locked",
        "--no-dev",
        "python",
        "scripts/seed_rehearsal_catalogue.py",
    )


def candidate_startup_gate(images: dict[str, str]) -> dict[str, Any]:
    """Prove a candidate exits instead of serving before schema migration."""
    project = f"bikemapy-migration-gate-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="bikemapy-migration-gate-env-") as directory:
        env_file = Path(directory) / "runtime.env"
        runtime_env(env_file, images["candidate_image"])
        stack = Stack(project, env_file)
        try:
            stack.compose("up", "-d", "--wait", "db", "redis")
            migration_command = (
                "uv",
                "run",
                "--locked",
                "--no-dev",
                "python",
                "backend/manage.py",
                "migrate",
                "--check",
                "--noinput",
            )
            plan = stack.compose_unchecked(
                "run",
                "--rm",
                "--no-deps",
                "backend",
                *migration_command[:-2],
                "--plan",
                capture=True,
            )
            plan_output = f"{plan.stdout}\n{plan.stderr}"
            if plan.returncode != 0 or "Planned operations:" not in plan_output:
                raise RuntimeError(
                    "migration gate dependencies/command did not pass before the expected check "
                    f"failure (exit={plan.returncode}): {plan_output}"
                )
            check = stack.compose_unchecked(
                "run", "--rm", "--no-deps", "backend", *migration_command, capture=True
            )
            check_output = f"{check.stdout}\n{check.stderr}"
            if check.returncode != 1:
                raise RuntimeError(
                    "migrate --check did not report unapplied migrations "
                    f"(exit={check.returncode}): {check_output}"
                )
            result = stack.compose_unchecked("run", "--rm", "--no-deps", "backend", capture=True)
            startup_output = f"{result.stdout}\n{result.stderr}"
            if result.returncode == 0 or "migration" not in startup_output.lower():
                raise RuntimeError("candidate unexpectedly started before migrations")
            return {
                "verified": True,
                "candidate_image": images["candidate_image"],
                "exit_code": result.returncode,
                "reason": "migrate --check rejected the unapplied schema",
                "migration_plan_output": plan_output.strip(),
                "migration_check_output": check_output.strip(),
                "startup_guard_output": startup_output.strip(),
            }
        finally:
            stack.stop()


def run_backup(
    stack: Stack,
    backup_dir: Path,
    *,
    backup_id: str,
    gpx_volume: str | None = None,
) -> subprocess.CompletedProcess[str]:
    compose = " ".join(shlex.quote(part) for part in stack.base)
    environment = os.environ.copy()
    environment.update(
        {
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_ID": backup_id,
            "COMPOSE": compose,
            "GPX_VOLUME": gpx_volume or f"{stack.project}_gpx_data",
        }
    )
    return subprocess.run(
        ["bash", str(ROOT / "deploy/backup.sh")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def rollback_rehearsal(images: dict[str, str], evidence: Path) -> dict[str, Any]:
    project = f"bikemapy-rollback-{os.getpid()}"
    with tempfile.TemporaryDirectory(prefix="bikemapy-rollback-env-") as directory:
        env_file = Path(directory) / "runtime.env"
        backup_dir = Path(directory) / "backup"
        runtime_env(env_file, images["previous_image"], backup_dir=backup_dir)
        stack = Stack(project, env_file)
        try:
            migration_gate = candidate_startup_gate(images)
            previous_port = migrate_and_start(stack, worker=False)
            seed_catalogue(stack)

            backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_result = run_backup(stack, backup_dir, backup_id=backup_id)
            if backup_result.returncode != 0:
                error_output = f"{backup_result.stdout}\n{backup_result.stderr}"
                raise RuntimeError(f"backup rehearsal failed:\n{error_output}")
            manifest = json.loads((backup_dir / f"manifest-{backup_id}.json").read_text())

            failed_backup_id = f"{backup_id}-failure"
            # Leave the previous container serving while selecting the
            # candidate in Compose. If backup.sh accidentally used the
            # selected environment during recovery, this check would observe
            # the candidate image instead of the previous image.
            runtime_env(env_file, images["candidate_image"], backup_dir=backup_dir)
            previous_container_before_failure = stack.backend_container_id()
            failed_backup = run_backup(
                stack,
                backup_dir,
                backup_id=failed_backup_id,
                gpx_volume="/dev/null",
            )
            if failed_backup.returncode == 0:
                raise RuntimeError("failed backup rehearsal unexpectedly succeeded")
            previous_port = stack.backend_port()
            previous_base = f"http://127.0.0.1:{previous_port}"
            previous_after_backup_failure = wait_for(f"{previous_base}/health/ready/")
            previous_container_after_failure = stack.backend_container_id()
            if previous_container_after_failure == previous_container_before_failure:
                raise RuntimeError("backup failure did not recreate the previous backend")
            previous_after_failure_image_id = stack.backend_image_id()
            if previous_after_failure_image_id != images["previous_image_id"]:
                raise RuntimeError("backup failure did not restore the previous image")

            stack.compose("stop", "backend", "worker", "beat", "proxy")
            stack.compose(
                "run",
                "--no-deps",
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
            stack.compose("up", "-d", "--force-recreate", "backend")
            candidate_port = stack.backend_port()
            base = f"http://127.0.0.1:{candidate_port}"
            candidate_health = wait_for(f"{base}/health/ready/")
            candidate_running_image_id = stack.backend_image_id()
            if candidate_running_image_id != images["candidate_image_id"]:
                raise RuntimeError("candidate container did not resolve to its built image ID")
            seed_catalogue(stack)
            candidate_routes = api_json(base, "/api/v1/routes/")
            if len(candidate_routes.get("results", [])) != 1:
                raise RuntimeError("candidate route read did not return one seeded route")

            runtime_env(env_file, images["previous_image"], backup_dir=backup_dir)
            stack.compose("up", "-d", "--force-recreate", "--no-deps", "backend")
            previous_port = stack.backend_port()
            previous_base = f"http://127.0.0.1:{previous_port}"
            previous_health = wait_for(f"{previous_base}/health/ready/")
            previous_running_image_id = stack.backend_image_id()
            if previous_running_image_id != images["previous_image_id"]:
                raise RuntimeError("rollback container did not resolve to the previous image ID")
            previous_routes = api_json(previous_base, "/api/v1/routes/")
            if len(previous_routes.get("results", [])) != 1:
                raise RuntimeError("previous route read did not return one seeded route")
            result = {
                "kind": "disposable-deployment-rollback-rehearsal",
                "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "project": project,
                "candidate": {
                    "source_ref": images["candidate_ref"],
                    "image": images["candidate_image"],
                    "image_id": images["candidate_image_id"],
                    "running_image_id": candidate_running_image_id,
                    "health": candidate_health,
                    "route_count": len(candidate_routes["results"]),
                },
                "previous": {
                    "source_ref": images["previous_ref"],
                    "image": images["previous_image"],
                    "image_id": images["previous_image_id"],
                    "running_image_id": previous_running_image_id,
                    "health": previous_health,
                    "route_count": len(previous_routes["results"]),
                },
                "migration_gate": migration_gate,
                "backup": {
                    "backup_id": backup_id,
                    "manifest": manifest,
                    "database_dump": f"db-{backup_id}.dump",
                    "gpx_archive": f"gpx-{backup_id}.tar.gz",
                },
                "backup_failure_recovery": {
                    "backup_id": failed_backup_id,
                    "failed_exit_code": failed_backup.returncode,
                    "previous_health": previous_after_backup_failure,
                    "container_before": previous_container_before_failure,
                    "container_after": previous_container_after_failure,
                    "running_image_id": previous_after_failure_image_id,
                },
                "environment": "local disposable Docker Compose PostGIS/Redis stack",
                "scope": "non-production rehearsal; production rollback remains an owner action",
            }
            evidence.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
        finally:
            stack.stop()


def state(stack: Stack) -> dict[str, Any]:
    code = (
        "import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); "
        "django.setup(); import json; "
        "from apps.catalogue.models import ForumPost, RouteSource, Route; "
        "from apps.ingestion.models import CrawlCheckpoint, CrawlPageWork, CrawlTask; "
        "cp=CrawlCheckpoint.objects.get(stream='backfill:http://host.docker.internal:' + "
        "__import__('os').environ['REHEARSAL_FORUM_PORT'] + '/t/42'); "
        "print(json.dumps({'posts':ForumPost.objects.count(),'sources':RouteSource.objects.count(),"
        "'routes':Route.objects.count(),'page_number':cp.page_number,'next_url':cp.next_url,"
        "'work':list(CrawlPageWork.objects.values('url','status','attempts')),"
        "'task_statuses':list(CrawlTask.objects.filter(stream=cp.stream).values_list('status',flat=True))}))"
    )
    output = exec_backend(
        stack,
        "uv",
        "run",
        "--locked",
        "--no-dev",
        "python",
        "-c",
        code,
        capture=True,
    )
    return cast(dict[str, Any], json.loads(output.splitlines()[-1]))


def dispatch(stack: Stack, url: str) -> None:
    code = (
        "import django; django.setup(); from apps.ingestion.tasks import backfill_bikeforum; "
        f"print(backfill_bikeforum.delay({url!r}, max_pages=2).id)"
    )
    exec_backend(
        stack,
        "uv",
        "run",
        "--locked",
        "--no-dev",
        "python",
        "-c",
        code,
    )


def crawler_rehearsal(images: dict[str, str], evidence: Path) -> dict[str, Any]:
    project = f"bikemapy-crawler-{os.getpid()}"
    forum_port = free_port()
    env_dir = tempfile.TemporaryDirectory(prefix="bikemapy-crawler-env-")
    env_file = Path(env_dir.name) / "runtime.env"
    runtime_env(env_file, images["candidate_image"], forum_port=forum_port)
    env = os.environ.copy()
    env["REHEARSAL_FORUM_PORT"] = str(forum_port)
    server = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts/rehearsal_forum_server.py"),
            "--port",
            str(forum_port),
        ],
        cwd=ROOT,
        env=env,
    )
    stack = Stack(project, env_file)
    try:
        wait_http(f"http://127.0.0.1:{forum_port}/t/42")
        port = migrate_and_start(stack, worker=True)
        base = f"http://127.0.0.1:{port}"
        before = api_json(base, "/api/v1/routes/")
        if before.get("results"):
            raise RuntimeError("crawler rehearsal catalogue was not empty before backfill")
        url = f"http://host.docker.internal:{forum_port}/t/42"
        dispatch(stack, url)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            current = state(stack)
            if current["posts"] >= 1 and current["page_number"] >= 1:
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"first crawler page did not commit: {current}")
        seed_catalogue(stack)
        middle = api_json(base, "/api/v1/routes/")
        if len(middle.get("results", [])) != 1 or not middle["results"][0]["sources"]:
            raise RuntimeError("browse/detail/source API did not expose the partial catalogue")
        first_route_id = middle["results"][0]["id"]
        middle_detail = api_json(base, f"/api/v1/routes/{first_route_id}/")
        if not middle_detail["sources"]:
            raise RuntimeError("partial detail response did not expose source attribution")

        stack.compose("kill", "-s", "SIGKILL", "worker")
        interrupted = state(stack)
        time.sleep(7)
        stack.compose("up", "-d", "worker")
        dispatch(stack, url)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            resumed = state(stack)
            if (
                resumed["posts"] >= 2
                and resumed["sources"] >= 2
                and "completed" in resumed["task_statuses"]
            ):
                break
            time.sleep(1)
        else:
            raise RuntimeError(f"crawler did not resume after worker kill: {resumed}")
        seed_catalogue(stack)
        after = api_json(base, "/api/v1/routes/")
        if len(after.get("results", [])) != 2:
            raise RuntimeError("resumed catalogue did not expose both routes through browse API")
        detail_sources = []
        for item in after["results"]:
            detail = api_json(base, f"/api/v1/routes/{item['id']}/")
            detail_sources.append(len(detail["sources"]))
        if detail_sources != [1, 1]:
            raise RuntimeError(f"unexpected source attribution after resume: {detail_sources}")
        result = {
            "kind": "disposable-postgis-redis-celery-resume-rehearsal",
            "verified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "project": project,
            "worker_interruption": {
                "signal": "SIGKILL",
                "interrupted_state": interrupted,
                "resumed_state": resumed,
            },
            "api": {
                "before_backfill_route_count": len(before["results"]),
                "partial_browse_route_count": len(middle["results"]),
                "partial_detail_source_count": len(middle_detail["sources"]),
                "after_resume_route_count": len(after["results"]),
                "after_resume_detail_source_counts": detail_sources,
            },
            "environment": "local disposable Docker Compose PostGIS/Redis/Celery stack",
            "scope": "synthetic fixture only; production worker and catalogue remain owner actions",
        }
        evidence.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        return result
    finally:
        try:
            stack.stop()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            env_dir.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("all", "rollback", "crawler"), default="all")
    parser.add_argument("--previous-ref", default="HEAD")
    parser.add_argument("--evidence-dir", type=Path, default=ROOT / "docs" / "evidence")
    args = parser.parse_args()
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    images = build_images(args.previous_ref)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if args.mode in {"all", "rollback"}:
        result = rollback_rehearsal(images, args.evidence_dir / f"issue-18-rollback-{stamp}.json")
        print(json.dumps(result, indent=2))
    if args.mode in {"all", "crawler"}:
        result = crawler_rehearsal(images, args.evidence_dir / f"issue-18-crawler-{stamp}.json")
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
