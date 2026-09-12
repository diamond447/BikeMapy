#!/usr/bin/env python3
"""Validate GPX backup archive members without line-oriented path parsing."""

from __future__ import annotations

import gzip
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
        with open(archive, "rb") as raw_archive:
            # Keep the gzip layer explicit: tarfile can finish after the last
            # member without consuming the gzip trailer. Reading to EOF makes
            # truncated data and a bad CRC fail before restore mutates storage.
            with gzip.GzipFile(fileobj=raw_archive, mode="rb") as compressed:
                with tarfile.open(fileobj=compressed, mode="r:") as stream:
                    for member in stream:
                        _validate_member(member)
                compressed.read()
    except (EOFError, OSError, gzip.BadGzipFile, tarfile.TarError, ValueError) as exc:
        print(f"GPX archive validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
