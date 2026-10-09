import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from scripts.rehearse_launch import runtime_env


@pytest.mark.parametrize("public_host", ["api.example.invalid", "staging.example.invalid"])
def test_production_compose_readiness_and_proxy_survive_public_host_change(
    tmp_path: Path, public_host: str
) -> None:
    """Render production Compose without pulling images or starting services."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is required to render the production Compose model")

    root = Path(__file__).parents[3]
    compose_file = root / "deploy" / "compose.production.yml"
    example_file = root / "deploy" / ".env.production.example"
    assert example_file.is_file(), "tracked production environment example is required"
    env_file = tmp_path / ".env.production"
    example = example_file.read_text()
    env_file.write_text(
        f"BIKEMAPY_ENV_FILE={env_file}\n{example.replace('api.example.invalid', public_host)}"
    )

    result = subprocess.run(
        [
            docker,
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    model = json.loads(result.stdout)
    services = model["services"]
    backend = services["backend"]
    proxy = services["proxy"]

    long_running = {"db", "redis", "backend", "worker", "beat", "proxy"}
    assert long_running <= services.keys()
    assert all(services[name]["restart"] == "unless-stopped" for name in long_running)
    for name in ("backend", "worker", "beat"):
        assert all(
            dependency["restart"] is True for dependency in services[name]["depends_on"].values()
        )
    assert services["proxy"]["depends_on"]["backend"]["restart"] is True

    assert backend["environment"]["DJANGO_DEBUG"] == "false"
    assert backend["environment"]["DJANGO_ALLOWED_HOSTS"] == f"{public_host},backend"
    assert "http://backend:8000/health/ready/" in backend["healthcheck"]["test"][-1]
    assert proxy["depends_on"]["backend"]["condition"] == "service_healthy"


def test_production_compose_requires_explicit_debug_false(tmp_path: Path) -> None:
    """Do not allow production Compose to fall back to local DEBUG defaults."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is required to render the production Compose model")

    root = Path(__file__).parents[3]
    compose_file = root / "deploy" / "compose.production.yml"
    example_file = root / "deploy" / ".env.production.example"
    assert example_file.is_file(), "tracked production environment example is required"
    env_file = tmp_path / ".env.production"
    example = example_file.read_text()
    env_file.write_text(
        f"BIKEMAPY_ENV_FILE={env_file}\n{example.replace('DJANGO_DEBUG=false\n', '')}"
    )

    environment = os.environ.copy()
    environment.pop("DJANGO_DEBUG", None)
    result = subprocess.run(
        [
            docker,
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(env_file),
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
    )
    assert result.returncode != 0
    assert "DJANGO_DEBUG" in result.stderr


def test_lean_compose_override_runs_only_database_api_and_proxy(tmp_path: Path) -> None:
    """The lean override keeps background services behind the `jobs` profile."""
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is required to render the production Compose model")

    root = Path(__file__).parents[3]
    env_file = tmp_path / ".env.production"
    example = (root / "deploy" / ".env.production.example").read_text()
    env_file.write_text(f"BIKEMAPY_ENV_FILE={env_file}\n{example}")
    environment = {key: value for key, value in os.environ.items() if key != "COMPOSE_PROFILES"}

    result = subprocess.run(
        [
            docker,
            "compose",
            "--project-directory",
            str(root),
            "--env-file",
            str(env_file),
            "-f",
            str(root / "deploy" / "compose.production.yml"),
            "-f",
            str(root / "deploy" / "compose.lean.yml"),
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services.keys() == {"db", "backend", "proxy"}
    backend = services["backend"]
    assert backend["environment"]["BACKGROUND_JOBS_ENABLED"] == "false"
    assert backend["depends_on"].keys() == {"db"}
    assert backend["depends_on"]["db"]["restart"] is True


def test_launch_rehearsal_runtime_env_disables_debug(tmp_path: Path) -> None:
    env_file = tmp_path / "runtime.env"
    runtime_env(env_file, "bikemapy-rehearsal:test")

    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert values["DJANGO_DEBUG"] == "false"
    assert values["BIKEFORUM_CRAWL_ENABLED"] == "false"
    assert values["BIKEFORUM_PROVIDER_AUTHORIZED"] == "false"
    assert values["BIKEFORUM_OPERATOR_APPROVED"] == "false"


def test_launch_rehearsal_runtime_env_enables_only_synthetic_crawler(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "runtime.env"
    runtime_env(env_file, "bikemapy-rehearsal:test", forum_port=43123)

    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert values["BIKEFORUM_CRAWL_ENABLED"] == "true"
    assert values["BIKEFORUM_PROVIDER_AUTHORIZED"] == "true"
    assert values["BIKEFORUM_OPERATOR_APPROVED"] == "true"
    assert values["BIKEFORUM_ALLOWED_ORIGINS"] == "http://host.docker.internal:43123"


@pytest.mark.parametrize(("flag", "lean"), [("true", True), ("false", False), (None, False)])
def test_compose_wrapper_adds_the_lean_override_only_when_enabled(
    tmp_path: Path, flag: str | None, lean: bool
) -> None:
    root = Path(__file__).parents[3]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake_docker.chmod(0o755)
    env_file = tmp_path / ".env.production"
    env_file.write_text("POSTGRES_DB=bikemapy\n" + (f"BIKEMAPY_LEAN={flag}\n" if flag else ""))

    result = subprocess.run(
        [str(root / "deploy" / "compose.sh"), "config", "--services"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "BIKEMAPY_COMPOSE_ENV_FILE": str(env_file),
        },
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    args = result.stdout.splitlines()
    assert args[:3] == ["compose", "--env-file", str(env_file)]
    assert args[-2:] == ["config", "--services"]
    assert any(arg.endswith("compose.production.yml") for arg in args)
    assert any(arg.endswith("compose.lean.yml") for arg in args) is lean
