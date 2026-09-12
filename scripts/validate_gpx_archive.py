#!/usr/bin/env python3
"""Validate GPX backup archive members without line-oriented path parsing."""

from __future__ import annotations

import sys
import tarfile
from pathlib import PurePosixPath


def _validate_member(member: tarfile.TarInfo) -> None:
    """Reject unsafe member types and names outside the production media root."""

    if not member.isdir() and not member.isreg():
        raise ValueError(f"GPX archive contains unsupported member type: {member.name!r}")
    name = member.name
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"GPX archive contains traversal: {name!r}")
    normalized = name.removeprefix("./")
    if normalized not in {"", ".", "media"} and not normalized.startswith("media/"):
        raise ValueError(f"GPX archive contains a path outside media/: {name!r}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} ARCHIVE", file=sys.stderr)
        return 2
    archive = argv[1]
    try:
        with tarfile.open(archive, mode="r:gz") as stream:
            for member in stream:
                _validate_member(member)
    except (OSError, tarfile.TarError, ValueError) as exc:
        print(f"GPX archive validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
