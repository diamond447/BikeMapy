"""Small deterministic benchmark for representative completion fixtures.

Run with ``uv run python -m apps.reference_routes.benchmarks.completion_benchmark``
from the backend directory. The recorded baseline is kept in the adjacent JSON
fixture; deployment-specific timings should be regenerated on the PostGIS
service before changing the tolerance policy.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from math import ceil
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
from django.contrib.gis.geos import GEOSGeometry  # noqa: E402

django.setup()

from apps.reference_routes.completion_services import METRIC_SRID  # noqa: E402

FIXTURE = Path(__file__).with_name("completion_benchmark.json")


def run() -> dict[str, object]:
    fixture = json.loads(FIXTURE.read_text())
    runs = max(5, int(os.getenv("COMPLETION_BENCHMARK_RUNS", "15")))

    def calculate() -> tuple[float, float]:
        route = GEOSGeometry(json.dumps(fixture["route"]), srid=4326)
        route.transform(METRIC_SRID)
        union = None
        for activity in fixture["activities"]:
            geometry = GEOSGeometry(json.dumps(activity), srid=4326)
            geometry.transform(METRIC_SRID)
            corridor = geometry.buffer(50, quadsegs=8)
            union = corridor if union is None else union.union(corridor)
        covered = route.intersection(union).length if union is not None else 0.0
        return route.length, covered

    samples = []
    total_length, covered = calculate()
    for _ in range(runs):
        started = time.perf_counter()
        total_length, covered = calculate()
        samples.append((time.perf_counter() - started) * 1000)
    expected = float(fixture.get("expected_coverage_percent", 100.0))
    coverage_percent = 0.0 if total_length <= 0 else covered / total_length * 100
    accuracy_error = abs(coverage_percent - expected)
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, ceil(len(ordered) * 0.95) - 1)]
    accuracy_threshold = float(fixture.get("max_accuracy_error_percent", 0.5))
    median_threshold = float(fixture.get("max_median_ms", 500.0))
    return {
        "fixture": fixture["name"],
        "activity_count": len(fixture["activities"]),
        "total_length_meters": round(total_length, 3),
        "covered_length_meters": round(covered, 3),
        "coverage_percent": round(coverage_percent, 3),
        "accuracy_error_percent": round(accuracy_error, 3),
        "accuracy_threshold_percent": accuracy_threshold,
        "accuracy_pass": accuracy_error <= accuracy_threshold,
        "runs": runs,
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(p95, 3),
        "median_threshold_ms": median_threshold,
        "performance_pass": statistics.median(samples) <= median_threshold,
    }


if __name__ == "__main__":
    print(json.dumps(run(), sort_keys=True))
