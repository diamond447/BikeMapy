#!/usr/bin/env python3
"""Validate every live database-to-GPX reference in a restored volume."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path, PurePosixPath

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from apps.ingestion.gpx import GpxValidationError, parse_gpx  # noqa: E402


def _payload_path(storage_root: Path, storage_key: str) -> Path:
    """Resolve a Django storage key without allowing it to escape the volume."""

    key = PurePosixPath(storage_key)
    if key.is_absolute() or len(key.parts) < 2 or key.parts[0] != "gpx" or ".." in key.parts:
        raise ValueError(f"storage key is outside gpx/: {storage_key}")
    root = storage_root.resolve()
    path = (root / Path(*key.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"storage key escapes the restored volume: {storage_key}") from exc
    return path


def _validate_reference(storage_root: Path, row: str) -> str:
    fields = row.rstrip("\n").split("\t")
    if len(fields) != 4 or any(not field for field in fields):
        raise ValueError(
            "reference row must contain version ID, storage key, checksum, and route ID"
        )
    version_id, storage_key, expected_checksum, route_id = fields
    path = _payload_path(storage_root, storage_key)
    if not path.is_file():
        raise FileNotFoundError(f"version {version_id} ({route_id}) is missing {storage_key}")
    payload = path.read_bytes()
    actual_checksum = hashlib.sha256(payload).hexdigest()
    if actual_checksum != expected_checksum:
        raise ValueError(
            f"version {version_id} ({route_id}) checksum mismatch for {storage_key}: "
            f"expected {expected_checksum}, got {actual_checksum}"
        )
    try:
        parsed = parse_gpx(payload)
    except GpxValidationError as exc:
        raise ValueError(
            f"version {version_id} ({route_id}) has invalid GPX {storage_key}: {exc}"
        ) from exc
    return (
        f"version {version_id} ({route_id}) valid: {storage_key} "
        f"({len(parsed.geometry['coordinates'])} points)"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, required=True)
    args = parser.parse_args(argv[1:])

    rows = [line for line in sys.stdin if line.strip()]
    if not rows:
        print("Restored GPX validation failed: no live database GPX references", file=sys.stderr)
        return 1

    failures: list[str] = []
    for row in rows:
        try:
            print(_validate_reference(args.storage_root, row))
        except (OSError, ValueError) as exc:
            failures.append(str(exc))
    if failures:
        for failure in failures:
            print(f"Restored GPX validation failed: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
