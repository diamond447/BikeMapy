from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

from apps.accounts.capture_services import (
    CaptureCalculationError,
    calculate_capture,
    current_capture,
)
from apps.accounts.competition_services import (
    create_competition,
    join_competition,
    schedule_recomputation,
)
from apps.accounts.models import (
    CaptureCalculation,
    CapturePlayerArea,
    ImportedActivity,
    Player,
)
from apps.accounts.tasks import dispatch_capture_calculations_task

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="persistent capture projections require PostGIS",
    ),
]


def _player(athlete_id: int) -> Player:
    user = get_user_model().objects.create_user(username=f"capture-{athlete_id}")
    return Player.objects.create(user=user, strava_athlete_id=athlete_id)


def _line(points: tuple[tuple[float, float], ...]) -> Any:
    from django.contrib.gis.geos import GEOSGeometry

    return GEOSGeometry(
        "LINESTRING ("
        + ", ".join(f"{longitude} {latitude}" for longitude, latitude in points)
        + ")",
        srid=4326,
    )


def _activity(
    player: Player,
    provider_id: str,
    points: tuple[tuple[float, float], ...],
    day: int,
) -> ImportedActivity:
    started_at = datetime(2025, 1, day, 12, tzinfo=UTC)
    return ImportedActivity.objects.create(
        player=player,
        provider_activity_id=provider_id,
        started_at=started_at,
        calendar_date=started_at.date(),
        geometry=_line(points),
        geometry_hash=provider_id,
    )


def test_persists_atomic_faces_shared_owners_and_equal_player_areas() -> None:
    owner = _player(1001)
    member = _player(1002)
    competition, _ = create_competition(owner, name="Persistent capture")
    join_competition(member, invite_code=competition.invite_code)
    outer = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    overlap = ((14.005, 50.0), (14.015, 50.0), (14.015, 50.01), (14.005, 50.01), (14.005, 50.0))
    _activity(owner, "outer", outer, 2)
    _activity(member, "overlap", overlap, 2)

    calculation = calculate_capture(competition)

    assert calculation.status == CaptureCalculation.Status.FRESH
    assert calculation.is_current
    assert calculation.face_count == 3
    shared_found = False
    for face in calculation.faces.prefetch_related("owners"):
        owners = list(face.owners.all())
        if len(owners) == 2:
            shared_found = True
            assert owners[0].shared_area_m2 == owners[1].shared_area_m2
    assert shared_found

    totals = {
        row.player_id: row.owned_area_m2
        for row in CapturePlayerArea.objects.filter(calculation=calculation)
    }
    assert totals[owner.pk] > Decimal("0")
    assert totals[member.pk] > Decimal("0")
    current = current_capture(competition)
    assert current is not None
    assert current.pk == calculation.pk


def test_newer_inner_claim_wins_and_failed_rebuild_preserves_current() -> None:
    owner = _player(1011)
    member = _player(1012)
    competition, _ = create_competition(owner, name="Chronology")
    join_competition(member, invite_code=competition.invite_code)
    outer = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    inner = (
        (14.002, 50.002),
        (14.008, 50.002),
        (14.008, 50.008),
        (14.002, 50.008),
        (14.002, 50.002),
    )
    for index, points in enumerate(zip(outer[:-1], outer[1:], strict=True)):
        _activity(owner, f"outer-{index}", (points[0], points[1]), 2)
    _activity(owner, "outer-final", (outer[-2], outer[-1]), 6)
    _activity(member, "inner", inner, 4)

    first = calculate_capture(competition)
    assert {
        tuple(sorted(row.player_id for row in face.owners.all())) for face in first.faces.all()
    } == {
        (owner.pk,),
        (member.pk,),
    }

    scheduled = schedule_recomputation(competition)
    with pytest.raises(CaptureCalculationError):
        with patch(
            "apps.accounts.capture_services.capture_traces",
            side_effect=RuntimeError("topology unavailable"),
        ):
            calculate_capture(competition, generation=scheduled.generation)
    current = current_capture(competition)
    assert current is not None
    assert current.pk == first.pk
    failed = CaptureCalculation.objects.get(
        competition=competition, generation=scheduled.generation
    )
    assert failed.status == CaptureCalculation.Status.FAILED


def test_membership_capture_generation_is_bounded_and_dispatched() -> None:
    owner = _player(1021)
    competition, _ = create_competition(owner, name="Queued capture")
    points = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "queued-boundary", points, 2)

    outcome = dispatch_capture_calculations_task.apply(args=[1]).get()

    assert outcome == {"processed": 1, "failed": 0}
    current = current_capture(competition)
    assert current is not None
    assert current.status == CaptureCalculation.Status.FRESH
    assert current.trace_count == 1
