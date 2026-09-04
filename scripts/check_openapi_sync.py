"""Verify the committed frontend client is reproducible from the Django schema."""

from __future__ import annotations

import difflib
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATED_SCHEMA = ROOT / "frontend" / "src" / "api" / "generated" / "schema.ts"


def run(*command: str) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bikemapy-openapi-") as directory:
        temporary = Path(directory)
        openapi = temporary / "openapi.yaml"
        generated = temporary / "schema.ts"
        run(
            sys.executable,
            "backend/manage.py",
            "spectacular",
            "--file",
            str(openapi),
            "--validate",
        )
        run(
            "frontend/node_modules/.bin/openapi-typescript",
            str(openapi),
            "-o",
            str(generated),
        )
        run(
            "frontend/node_modules/.bin/prettier",
            "--config",
            "frontend/.prettierrc",
            "--write",
            str(generated),
        )
        expected = GENERATED_SCHEMA.read_text(encoding="utf-8")
        actual = generated.read_text(encoding="utf-8")
        if expected == actual:
            return 0
        diff = difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=str(GENERATED_SCHEMA),
            tofile="regenerated schema",
        )
        sys.stderr.writelines(diff)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
