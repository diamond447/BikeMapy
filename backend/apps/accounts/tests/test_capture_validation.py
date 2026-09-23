from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.db import connection

from apps.accounts.capture_validation import (
    MAX_COORDINATES,
    MAX_TRACES,
    CaptureTrace,
    CaptureValidationError,
    ValidationResult,
    safe_validate,
    validate_capture,
)

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="capture topology harness requires PostGIS",
    ),
]
FIXTURES = Path(__file__).with_name("fixtures") / "capture_validation_cases.json"
CASES: dict[str, list[list[float]]] = json.loads(FIXTURES.read_text())


def _trace(
    trace_id: str,
    owner_id: str,
    points: list[list[float]],
    *,
    day: int = 2,
    hour: int = 12,
) -> CaptureTrace:
    return CaptureTrace(
        trace_id=trace_id,
        owner_id=owner_id,
        recorded_at=datetime(2025, 1, day, hour, tzinfo=UTC),
        coordinates=tuple((point[0], point[1]) for point in points),
    )


def _edges(
    points: list[list[float]], owner_id: str, *, prefix: str = "edge", day: int = 2
) -> list[CaptureTrace]:
    return [
        _trace(f"{prefix}-{index}", owner_id, [points[index], points[index + 1]], day=day)
        for index in range(len(points) - 1)
    ]


def test_noisy_gps_joins_edges_within_50_metres() -> None:
    square = CASES["unit_square"]
    noisy = [
        [square[0][0] + 0.00015, square[0][1] + 0.00005],
        square[1],
        [square[2][0] - 0.00012, square[2][1] - 0.00004],
        square[3],
        square[0],
    ]
    result = validate_capture(_edges(noisy, "rider"))
    assert len(result.faces) == 1
    assert result.faces[0].owner_ids == ("rider",)


def test_prague_chronology_is_used_at_midnight_and_delivery_order_is_irrelevant() -> None:
    traces = _edges(CASES["unit_square"], "rider")
    traces = [
        CaptureTrace(
            trace.trace_id,
            trace.owner_id,
            datetime(2025, 1, 1, 23, 30, tzinfo=UTC),
            trace.coordinates,
        )
        for trace in traces
    ]
    result = validate_capture(list(reversed(traces)))
    assert result.faces[0].effective_date is not None
    assert result.faces[0].effective_date.isoformat() == "2025-01-02"
    assert result == validate_capture(traces)


def test_intersections_nested_loops_and_disconnected_traces_produce_bounded_faces() -> None:
    outer = _edges(CASES["unit_square"], "old", prefix="outer")
    inner = _edges(CASES["inner_square"], "new", prefix="inner", day=3)
    disconnected = _edges(CASES["second_square"], "other", prefix="second")
    result = validate_capture(outer + inner + disconnected)
    assert len(result.faces) == 3
    assert sum(face.area_m2 for face in result.faces) > 0


def test_same_day_overlapping_claims_are_shared() -> None:
    traces = _edges(CASES["unit_square"], "alice", prefix="alice")
    traces += _edges(CASES["unit_square"], "bob", prefix="bob")
    result = validate_capture(traces)
    assert result.faces[0].owner_ids == ("alice", "bob")


def test_bitten_apple_partial_new_boundary_does_not_beat_old_complete_boundary() -> None:
    square = CASES["unit_square"]
    old = _edges(square, "rider", prefix="old", day=2)
    newer_partial = [_trace("new-partial", "rider", [square[0], square[1]], day=4)]
    result = validate_capture(old + newer_partial)
    assert result.faces[0].effective_date is not None
    assert result.faces[0].effective_date.isoformat() == "2025-01-02"


def test_fully_newer_retake_wins_over_old_network() -> None:
    traces = _edges(CASES["unit_square"], "rider", prefix="old", day=2)
    traces += _edges(CASES["unit_square"], "rider", prefix="retake", day=5)
    result = validate_capture(traces)
    assert result.faces[0].effective_date is not None
    assert result.faces[0].effective_date.isoformat() == "2025-01-05"


def test_self_intersection_is_noded_and_submetre_sliver_is_discarded() -> None:
    bowtie = _trace(
        "bowtie",
        "rider",
        [
            [14.002, 50.0],
            [14.003, 50.001],
            [14.002, 50.001],
            [14.003, 50.0],
            [14.002, 50.0],
        ],
    )
    sliver = _trace(
        "sliver",
        "rider",
        [[14.01, 50.0], [14.0100001, 50.0], [14.0100001, 50.0000001], [14.01, 50.0]],
    )
    result = validate_capture([bowtie, sliver])
    assert len(result.faces) == 2
    assert all(face.area_m2 > 1 for face in result.faces)


def test_geodesic_area_is_reported_for_world_scale_geometry() -> None:
    result = validate_capture([_trace("global", "world", CASES["global_square"])])
    assert result.faces[0].area_m2 == pytest.approx(12_300_000_000, rel=0.03)


def test_invalid_input_and_bounded_safe_failure_preserve_last_valid_result() -> None:
    previous = ValidationResult((), 1, 2)
    invalid = [_trace("invalid", "rider", [[14.0, 50.0], [float("nan"), 50.0]])]
    safe = safe_validate(previous, invalid)
    assert safe.result == previous
    assert not safe.accepted
    assert safe.attempts == 3
    assert safe.error is not None


def test_input_limits_are_rejected_before_database_work() -> None:
    traces = [
        _trace(str(index), "rider", [[14, 50], [14.001, 50]]) for index in range(MAX_TRACES + 1)
    ]
    with pytest.raises(CaptureValidationError, match="trace limit"):
        validate_capture(traces)
    many_points = tuple((14 + index * 0.000001, 50.0) for index in range(MAX_COORDINATES + 1))
    with pytest.raises(CaptureValidationError, match="coordinate limit"):
        validate_capture([CaptureTrace("large", "rider", datetime.now(UTC), many_points)])


@pytest.mark.benchmark
@pytest.mark.skipif(
    os.getenv("RUN_CAPTURE_VALIDATION_BENCHMARK") != "1",
    reason="opt-in benchmark; run against the Compose PostGIS database",
)
def test_capture_validation_benchmark() -> None:
    traces: list[CaptureTrace] = []
    for index in range(100):
        longitude = 10 + (index % 10) * 0.01
        latitude = 45 + (index // 10) * 0.01
        square = [
            [longitude, latitude],
            [longitude + 0.005, latitude],
            [longitude + 0.005, latitude + 0.005],
            [longitude, latitude + 0.005],
            [longitude, latitude],
        ]
        traces.extend(_edges(square, "benchmark", prefix=f"square-{index}"))
    started = time.perf_counter()
    result = validate_capture(traces)
    elapsed = time.perf_counter() - started
    print(
        f"capture_validation traces={len(traces)} faces={len(result.faces)} seconds={elapsed:.3f}"
    )
    assert len(result.faces) == 100
    assert elapsed < 5
