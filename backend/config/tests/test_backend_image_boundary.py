import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest


def _build_image(docker: str, context: Path, dockerfile: Path, tag: str) -> None:
    result = subprocess.run(
        [docker, "build", "--file", str(dockerfile), "--tag", tag, str(context)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr


def _assert_image_paths(docker: str, tag: str, paths: tuple[str, ...]) -> None:
    command = " && ".join(f"test ! -e {path}" for path in paths)
    result = subprocess.run(
        [docker, "run", "--rm", tag, "sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_backend_image_uses_an_explicit_source_boundary() -> None:
    root = Path(__file__).parents[3]
    dockerfile = (root / "docker/backend.Dockerfile").read_text()
    dockerignore = (root / ".dockerignore").read_text().splitlines()
    ignored_paths = {line for line in dockerignore if line and not line.startswith("#")}

    assert "COPY backend ./backend" in dockerfile
    assert "COPY scripts ./scripts" in dockerfile
    assert "COPY . ." not in dockerfile
    assert {
        "**/.env",
        "**/.env.*",
        "**/*.dump",
        "*.dump",
        "**/*.tar.gz",
        "*.tar.gz",
        "**/*.age",
        "*.age",
        "backup/",
        "**/backup/",
        "**/AGENTS.md",
        "**/idea.txt",
        "**/review.md",
        "**/storage/",
        "restore/",
        "**/restore/",
        "restore-drill/",
        "**/restore-drill/",
        "restore-drill-*.gpx",
        "**/restore-drill-*.gpx",
        "review/",
        "**/review/",
    } <= ignored_paths


def test_backend_image_excludes_synthetic_operational_canaries() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is required to probe the backend build context")

    root = Path(__file__).parents[3]
    canaries = (
        "deploy/.env.production",
        "backup/db-review.dump",
        "review.md",
    )
    images: list[str] = []
    with TemporaryDirectory(prefix="bikemapy-image-boundary-") as temporary:
        context = Path(temporary)
        for source in ("backend", "scripts"):
            shutil.copytree(root / source, context / source)
        for source in (".dockerignore", "docker/backend.Dockerfile", "pyproject.toml", "uv.lock"):
            target = context / source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / source, target)

        for canary in canaries:
            path = context / canary
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic canary; this value must never enter an image\n")

        probe_dockerfile = context / "context-probe.Dockerfile"
        probe_dockerfile.write_text("FROM alpine:3.21\nCOPY . /probe\n")
        probe_tag = f"bikemapy-context-probe:{uuid4().hex}"
        backend_tag = f"bikemapy-backend-boundary:{uuid4().hex}"
        images.extend((probe_tag, backend_tag))
        try:
            _build_image(docker, context, probe_dockerfile, probe_tag)
            _assert_image_paths(docker, probe_tag, tuple(f"/probe/{path}" for path in canaries))

            _build_image(docker, context, context / "docker/backend.Dockerfile", backend_tag)
            _assert_image_paths(docker, backend_tag, tuple(f"/app/{path}" for path in canaries))
            result = subprocess.run(
                [docker, "run", "--rm", backend_tag, "sh", "-c", "test -f /app/backend/manage.py"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stderr
        finally:
            subprocess.run([docker, "image", "rm", "--force", *images], check=False)
