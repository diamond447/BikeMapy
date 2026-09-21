"""Small deterministic benchmark for representative completion fixtures.

Run with ``uv run python -m apps.reference_routes.benchmarks.completion_benchmark``
from the backend directory. The recorded baseline is kept in the adjacent JSON
fixture; deployment-specific timings should be regenerated on the PostGIS
service before changing the tolerance policy.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
from django.contrib.gis.geos import GEOSGeometry  # noqa: E402

django.setup()

from apps.reference_routes.completion_services import METRIC_SRID  # noqa: E402

FIXTURE = Path(__file__).with_name("completion_benchmark.json")


def run() -> dict[str, object]:
    fixture = json.loads(FIXTURE.read_text())
    started = time.perf_counter()
    route = GEOSGeometry(json.dumps(fixture["route"]), srid=4326)
    route.transform(METRIC_SRID)
    union = None
    for activity in fixture["activities"]:
        geometry = GEOSGeometry(json.dumps(activity), srid=4326)
        geometry.transform(METRIC_SRID)
        corridor = geometry.buffer(50, quadsegs=8)
        union = corridor if union is None else union.union(corridor)
    covered = route.intersection(union).length if union is not None else 0.0
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "fixture": fixture["name"],
        "activity_count": len(fixture["activities"]),
        "total_length_meters": round(route.length, 3),
        "covered_length_meters": round(covered, 3),
        "elapsed_ms": round(elapsed_ms, 3),
    }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
