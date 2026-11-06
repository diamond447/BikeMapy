"""Tell GitHub Actions which parts of the project a pull request changes.

Outside pull requests every area counts as changed. Results are printed and
appended to ``$GITHUB_OUTPUT``::

    python scripts/ci_changed_areas.py --base "$BASE_SHA" --head "$HEAD_SHA"
"""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Iterable

BACKEND_PREFIXES = ("backend/", "scripts/", "docker/", "deploy/")
BACKEND_FILES = frozenset(
    {"pyproject.toml", "uv.lock", "docker-compose.yml", ".dockerignore", ".env.example", "Makefile"}
)
FRONTEND_PREFIXES = ("frontend/",)
WORKFLOW_PREFIXES = (".github/",)


def classify(paths: Iterable[str]) -> dict[str, bool]:
    """Return the changed areas and the job groups derived from them."""

    paths = list(paths)
    backend = any(path.startswith(BACKEND_PREFIXES) or path in BACKEND_FILES for path in paths)
    frontend = any(path.startswith(FRONTEND_PREFIXES) for path in paths)
    workflows = any(path.startswith(WORKFLOW_PREFIXES) for path in paths)
    return {
        "backend": backend or workflows,
        "frontend": frontend or workflows,
        # Workflow-only changes skip Playwright.
        "e2e": frontend,
        "api_contract": backend or frontend or workflows,
    }


def all_changed() -> dict[str, bool]:
    return {"backend": True, "frontend": True, "e2e": True, "api_contract": True}


def changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    args = parser.parse_args(argv)
    if args.base and args.head:
        areas = classify(changed_paths(args.base, args.head))
    else:
        areas = all_changed()
    lines = [f"{name}={'true' if value else 'false'}" for name, value in areas.items()]
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
