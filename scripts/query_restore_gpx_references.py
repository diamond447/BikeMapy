#!/usr/bin/env python3
"""Emit restored GPX database references as one lossless JSON document."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from apps.catalogue.models import Route, RouteLifecycle, RouteVersion  # noqa: E402


def live_references() -> list[list[str]]:
    """Return every non-removed GPX reference, preserving each storage key."""

    rows = RouteVersion.objects.filter(
        original_gpx_storage_key__gt="",
        payload_removed_at__isnull=True,
    ).values_list("id", "original_gpx_storage_key", "checksum", "source__route_id")
    return [[str(value) for value in row] for row in rows.order_by("id")]


def approved_references() -> list[list[str]]:
    """Return public routes and their current approved GPX references."""

    rows = Route.objects.filter(
        lifecycle=RouteLifecycle.PUBLISHED,
        current_approved_version__original_gpx_storage_key__gt="",
        current_approved_version__payload_removed_at__isnull=True,
    ).values_list(
        "id",
        "current_approved_version__original_gpx_storage_key",
        "current_approved_version__checksum",
    )
    return [[str(value) for value in row] for row in rows.order_by("id")]


def representative_approved_reference() -> list[str]:
    """Select the stable first public route for representative endpoint checks."""

    references = approved_references()
    return references[0] if references else []


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args(argv[1:])
    references: Any = representative_approved_reference() if args.approved else live_references()
    json.dump(references, sys.stdout, ensure_ascii=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
