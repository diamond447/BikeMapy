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


def test_launch_rehearsal_runtime_env_disables_debug(tmp_path: Path) -> None:
    env_file = tmp_path / "runtime.env"
    runtime_env(env_file, "bikemapy-rehearsal:test")

    assert "DJANGO_DEBUG=false\n" in env_file.read_text()
