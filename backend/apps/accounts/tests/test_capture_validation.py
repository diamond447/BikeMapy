from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db import OperationalError, connection

from apps.accounts.capture_validation import (
    MAX_COORDINATES,
    MAX_FACES,
    MAX_TRACES,
    CaptureBatch,
    CaptureTrace,
    CaptureValidationError,
    CaptureValidationTransientError,
    ValidationResult,
    safe_validate,
    validate_capture,
    validate_capture_incremental,
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


def _gapped_ring(latitude: float, gap_longitude: float) -> list[CaptureTrace]:
    points = [
        [14.0, latitude],
        [14.01, latitude],
        [14.01, latitude + 0.01],
        [14.0, latitude + 0.01],
        [14.0, latitude],
    ]
    points[0][0] += gap_longitude
    return _edges(points, "threshold", prefix=f"ring-{latitude}")


@pytest.mark.parametrize(
    ("latitude", "under_50m_longitude", "over_50m_longitude"),
    ((0.0, 0.00044, 0.00046), (80.0, 0.0024, 0.0027)),
)
def test_geodesic_join_threshold_is_accurate_at_equator_and_80n(
    latitude: float, under_50m_longitude: float, over_50m_longitude: float
) -> None:
    joined = validate_capture(_gapped_ring(latitude, under_50m_longitude))
    rejected = validate_capture(_gapped_ring(latitude, over_50m_longitude))
    assert len(joined.faces) == 1
    assert joined.faces[0].owner_ids == ("threshold",)
    assert rejected.faces == ()


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


def test_nested_loop_geometry_keeps_new_inner_owner_and_does_not_merge_loops() -> None:
    result = validate_capture(
        _edges(CASES["unit_square"], "old", prefix="outer")
        + _edges(CASES["inner_square"], "new", prefix="inner", day=3)
    )
    assert len(result.faces) == 2
    inner = min(result.faces, key=lambda face: face.area_m2)
    annulus = max(result.faces, key=lambda face: face.area_m2)
    assert inner.owner_ids == ("new",)
    assert inner.geometry_wkt.startswith("POLYGON((14.000249")
    assert annulus.owner_ids == ("old",)


def test_same_day_overlapping_claims_are_shared() -> None:
    traces = _edges(CASES["unit_square"], "alice", prefix="alice")
    traces += _edges(CASES["overlap_square"], "bob", prefix="bob")
    result = validate_capture(traces)
    by_owners = {face.owner_ids: face for face in result.faces}
    assert set(by_owners) == {("alice",), ("alice", "bob"), ("bob",)}
    assert all(face.area_m2 > 0 for face in result.faces)
    assert by_owners[("alice", "bob")].area_m2 == pytest.approx(4_000, rel=0.3)


def test_cross_owner_fully_newer_retake_wins_and_delivery_order_is_irrelevant() -> None:
    old = _edges(CASES["unit_square"], "alice", prefix="alice", day=2)
    newer = _edges(CASES["unit_square"], "bob", prefix="bob", day=5)
    result = validate_capture(old + newer)
    shuffled = validate_capture(list(reversed(newer + old)))
    assert result == shuffled
    assert result.faces[0].owner_ids == ("bob",)
    assert result.faces[0].effective_date is not None
    assert result.faces[0].effective_date.isoformat() == "2025-01-05"


def test_mixed_date_boundary_uses_oldest_required_segment() -> None:
    traces = _edges(CASES["unit_square"], "mixed", prefix="mixed")
    traces = [
        CaptureTrace(
            trace.trace_id,
            trace.owner_id,
            datetime(2025, 1, 2 + index, tzinfo=UTC),
            trace.coordinates,
        )
        for index, trace in enumerate(traces)
    ]
    result = validate_capture(traces)
    assert result.faces[0].effective_date is not None
    assert result.faces[0].effective_date.isoformat() == "2025-01-02"


def test_partial_same_day_overlap_can_be_completed_by_each_owner_and_shared() -> None:
    alice = _edges(CASES["unit_square"], "alice", prefix="alice")
    bob = _edges(CASES["overlap_square"], "bob", prefix="bob")
    result = validate_capture(alice[:2] + bob[:2] + alice[2:] + bob[2:])
    assert ("alice", "bob") in {face.owner_ids for face in result.faces}


def test_full_rebuild_equals_incremental_replay() -> None:
    traces = _edges(CASES["unit_square"], "alice", prefix="alice")
    traces += _edges(CASES["second_square"], "bob", prefix="bob", day=3)
    removed = traces[-1]
    full = validate_capture(traces[:-1])
    for batches in (
        (
            CaptureBatch(additions=tuple(reversed(traces))),
            CaptureBatch(removals=(removed.trace_id,)),
        ),
        (
            CaptureBatch(removals=(removed.trace_id,)),
            CaptureBatch(additions=tuple(reversed(traces))),
        ),
    ):
        assert validate_capture_incremental(batches) == full


def test_incremental_remove_before_add_dominates_a_delayed_boundary_trace() -> None:
    boundary = _edges(CASES["unit_square"], "alice", prefix="boundary")
    assert len(validate_capture(boundary).faces) == 1

    added_then_removed = validate_capture_incremental(
        (
            CaptureBatch(additions=tuple(boundary)),
            CaptureBatch(removals=(boundary[-1].trace_id,)),
        )
    )
    removed_then_added = validate_capture_incremental(
        (
            CaptureBatch(removals=(boundary[-1].trace_id,)),
            CaptureBatch(additions=tuple(boundary)),
        )
    )
    assert added_then_removed == removed_then_added
    assert added_then_removed.faces == ()


def test_cross_owner_bitten_apple_partial_boundary_does_not_beat_old_complete_claim() -> None:
    square = CASES["unit_square"]
    old_partial = _edges(square, "alice", prefix="old", day=2)
    alice = old_partial[:-1] + [_trace("old-final", "alice", [square[-2], square[-1]], day=6)]
    bob = _edges(CASES["inner_square"], "bob", prefix="new-inner", day=4)
    result = validate_capture(alice + bob)
    assert {face.owner_ids for face in result.faces} == {("alice",), ("bob",)}
    outer = max(result.faces, key=lambda face: face.area_m2)
    inner = min(result.faces, key=lambda face: face.area_m2)
    assert outer.owner_ids == ("alice",)
    assert inner.owner_ids == ("bob",)
    assert outer.effective_date is not None
    assert outer.effective_date.isoformat() == "2025-01-02"
    assert inner.effective_date is not None
    assert inner.effective_date.isoformat() == "2025-01-04"


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
        [[14.01, 50.0], [14.010003, 50.0], [14.010003, 50.000003], [14.01, 50.0]],
    )
    result = validate_capture([bowtie, sliver])
    assert len(result.faces) == 2
    assert all(face.area_m2 > 1 for face in result.faces)


def test_geodesic_area_is_reported_for_world_scale_geometry() -> None:
    result = validate_capture([_trace("global", "world", CASES["global_square"])])
    assert result.faces[0].area_m2 == pytest.approx(12_300_000_000, rel=0.03)


def test_unrelated_owner_endpoints_do_not_block_another_owners_gap_snap() -> None:
    alice = _gapped_ring(latitude=50.0, gap_longitude=0.00044)
    gap_start = alice[0].coordinates[0]
    gap_end = alice[-1].coordinates[-1]
    bob = [
        _trace("bob-start", "bob", [list(gap_start), [gap_start[0] + 0.001, gap_start[1]]]),
        _trace("bob-end", "bob", [list(gap_end), [gap_end[0] + 0.001, gap_end[1]]]),
    ]

    result = validate_capture(alice + bob)

    assert any(face.owner_ids == ("threshold",) for face in result.faces)


def test_dateline_crossing_ring_does_not_cover_disjoint_local_ring() -> None:
    dateline = [
        [179.0, 10.0],
        [179.5, 10.0],
        [179.5, 10.5],
        [-179.5, 10.5],
        [179.0, 10.0],
    ]
    local = [
        [14.0, 10.0],
        [14.5, 10.0],
        [14.5, 10.5],
        [14.0, 10.5],
        [14.0, 10.0],
    ]

    result = validate_capture(
        _edges(dateline, "dateline", prefix="dateline") + _edges(local, "local", prefix="local")
    )

    owners = {face.owner_ids for face in result.faces}
    assert ("dateline",) in owners
    assert ("local",) in owners
    local_face = next(face for face in result.faces if face.owner_ids == ("local",))
    assert local_face.area_m2 == pytest.approx(3_000_000_000, rel=0.2)


def test_dateline_projection_keeps_greenwich_ring_disjoint() -> None:
    alice = [
        [179.0, 10.0],
        [-179.0, 10.0],
        [-179.0, 11.0],
        [179.0, 11.0],
        [179.0, 10.0],
    ]
    bob = [
        [-0.5, 10.2],
        [0.5, 10.2],
        [0.5, 10.3],
        [-0.5, 10.3],
        [-0.5, 10.2],
    ]

    result = validate_capture(
        _edges(alice, "alice", prefix="alice") + _edges(bob, "bob", prefix="bob")
    )

    assert len(result.faces) == 2
    by_owner = {face.owner_ids: face for face in result.faces}
    assert by_owner[("alice",)].area_m2 == pytest.approx(24_200_000_000, rel=0.08)
    assert by_owner[("bob",)].area_m2 == pytest.approx(1_210_000_000, rel=0.08)


def test_invalid_input_and_bounded_safe_failure_preserve_last_valid_result() -> None:
    previous = ValidationResult((), 1, 2)
    invalid = [_trace("invalid", "rider", [[14.0, 50.0], [float("nan"), 50.0]])]
    safe = safe_validate(previous, invalid)
    assert safe.result == previous
    assert not safe.accepted
    assert safe.attempts == 1
    assert safe.error is not None


def test_transient_postgis_failure_retries_but_permanent_validation_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = ValidationResult((), 1, 2)
    calls = 0

    def flaky(_: object) -> ValidationResult:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise CaptureValidationTransientError("temporary")
        return previous

    monkeypatch.setattr("apps.accounts.capture_validation.validate_capture", flaky)
    recovered = safe_validate(previous, [])
    assert recovered.accepted
    assert recovered.attempts == 3
    assert calls == 3

    monkeypatch.setattr(
        "apps.accounts.capture_validation.validate_capture",
        lambda _: (_ for _ in ()).throw(CaptureValidationError("permanent")),
    )
    rejected = safe_validate(previous, [])
    assert not rejected.accepted
    assert rejected.attempts == 1


def test_postgis_topology_failure_is_classified_as_transient_and_preserves_previous() -> None:
    class FailingCursor:
        def __enter__(self) -> FailingCursor:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, query: str, params: object = None) -> None:
            if query.startswith("SET LOCAL statement_timeout"):
                return
            raise OperationalError("simulated topology connection loss")

        def fetchall(self) -> list[object]:
            return []

    previous = ValidationResult((), 1, 2)
    with patch("apps.accounts.capture_validation.connection.cursor", return_value=FailingCursor()):
        result = safe_validate(previous, [_trace("retry", "rider", CASES["unit_square"])])
    assert not result.accepted
    assert result.result == previous
    assert result.attempts == 3


def test_real_postgis_statement_timeout_retries_and_preserves_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.accounts.capture_validation.STATEMENT_TIMEOUT_MS", 1)
    previous = ValidationResult((), 1, 2)
    result = safe_validate(previous, [_trace("timeout", "rider", CASES["global_square"])])
    assert not result.accepted
    assert result.result == previous
    assert result.attempts == 3
    assert result.error is not None


def test_generated_face_bound_is_explicit() -> None:
    assert MAX_FACES == 200


def test_real_postgis_generated_face_limit_rejects_pathological_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.accounts.capture_validation.MAX_FACES", 1)
    traces = _edges(CASES["unit_square"], "a", prefix="a")
    traces += _edges(CASES["second_square"], "b", prefix="b")
    with pytest.raises(CaptureValidationError, match="generated face limit"):
        validate_capture(traces)


def test_real_postgis_response_byte_bound_rejects_large_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apps.accounts.capture_validation.MAX_RESULT_BYTES", 1)
    with pytest.raises(CaptureValidationError, match="response limit"):
        validate_capture([_trace("bytes", "rider", CASES["unit_square"])])


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
    for index in range(60):
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
    assert len(result.faces) == 60
    assert elapsed < 5
