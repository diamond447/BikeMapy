#!/usr/bin/env python3
"""Validate one restored GPX payload with the production ingestion parser."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from apps.ingestion.gpx import GpxValidationError, parse_gpx  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} GPX_FILE", file=sys.stderr)
        return 2
    payload_path = Path(argv[1])
    try:
        parsed = parse_gpx(payload_path.read_bytes())
    except (OSError, GpxValidationError) as exc:
        print(f"Restored GPX validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Restored GPX is valid ({len(parsed.geometry['coordinates'])} points): {payload_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
