import json
import shutil
import subprocess
from pathlib import Path

import pytest


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
    env_file = tmp_path / ".env.production"
    example = (root / "deploy" / ".env.production.example").read_text()
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
