from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from apps.accounts.activity_services import remove_activity
from apps.accounts.capture_services import (
    CaptureCalculationBusy,
    CaptureCalculationError,
    _line_coordinates,
    calculate_capture,
    claim_capture_calculation,
    current_capture,
)
from apps.accounts.competition_services import (
    CURRENT_SHARING_DISCLOSURE_VERSION,
    create_competition,
    grant_sharing_consent,
    join_competition,
    remove_member,
    schedule_recomputation,
)
from apps.accounts.models import (
    CaptureCalculation,
    CapturePlayerArea,
    Competition,
    CompetitionRecomputation,
    ImportedActivity,
    Player,
)
from apps.accounts.services import delete_player
from apps.accounts.tasks import (
    dispatch_capture_calculations_task,
    rebuild_capture_algorithm_task,
    recompute_competition_results_task,
)

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
    year: int = 2025,
) -> ImportedActivity:
    started_at = datetime(year, 1, day, 12, tzinfo=UTC)
    return ImportedActivity.objects.create(
        player=player,
        provider_activity_id=provider_id,
        started_at=started_at,
        calendar_date=started_at.date(),
        geometry=_line(points),
        geometry_hash=provider_id,
    )


def test_capture_excludes_unconsented_and_out_of_window_activity() -> None:
    owner = _player(1005)
    member = _player(1006)
    competition, _ = create_competition(owner, name="Capture privacy")
    _grant_capture_consent(owner, competition)
    join_competition(member, invite_code=competition.invite_code)
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "owner-recent", square, 2, year=2026)
    _activity(member, "member-without-consent", square, 3, year=2026)

    calculation = calculate_capture(competition)

    assert calculation.trace_count == 1
    assert {trace.player_id for trace in calculation.faces.first().owners.all()} == {owner.pk}


def test_recent_consent_excludes_activity_before_the_cutoff() -> None:
    owner = _player(1007)
    competition, _ = create_competition(owner, name="Recent capture privacy")
    grant_sharing_consent(
        owner,
        competition,
        scope="recent",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "old-activity", square, 2, year=2024)
    _activity(owner, "recent-activity", square, 3, year=2026)

    calculation = calculate_capture(competition)

    assert calculation.trace_count == 1


def _grant_capture_consent(player: Player, competition: Competition) -> None:
    grant_sharing_consent(
        player,
        competition,
        scope="full_history",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )


def test_capture_rejects_oversized_geometry_before_coordinate_materialization() -> None:
    geometry = SimpleNamespace(
        empty=False,
        geom_type="LineString",
        num_coords=30_001,
        srid=4326,
        coords=(),
    )

    with pytest.raises(CaptureCalculationError, match="coordinate limit"):
        _line_coordinates(geometry, max_coordinates=30_000, max_parts=1)


def _projection_signature(calculation: CaptureCalculation) -> tuple[object, ...]:
    faces = tuple(
        (
            face.face_id,
            face.geometry.wkb.hex(),
            face.area_m2,
            face.effective_date,
            tuple(sorted((owner.player_id, owner.shared_area_m2) for owner in face.owners.all())),
        )
        for face in calculation.faces.all()
    )
    areas = tuple(
        sorted((area.player_id, area.owned_area_m2) for area in calculation.player_areas.all())
    )
    return faces, areas


def test_persists_atomic_faces_shared_owners_and_equal_player_areas() -> None:
    owner = _player(1001)
    member = _player(1002)
    competition, _ = create_competition(owner, name="Persistent capture")
    join_competition(member, invite_code=competition.invite_code)
    _grant_capture_consent(owner, competition)
    _grant_capture_consent(member, competition)
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
    _grant_capture_consent(owner, competition)
    _grant_capture_consent(member, competition)
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
    _grant_capture_consent(owner, competition)
    points = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "queued-boundary", points, 2)

    outcome = dispatch_capture_calculations_task.apply(args=[1]).get()

    assert outcome == {"processed": 1, "failed": 0}
    current = current_capture(competition)
    assert current is not None
    assert current.status == CaptureCalculation.Status.FRESH
    assert current.trace_count == 1


def test_join_after_current_snapshot_queues_and_publishes_new_generation() -> None:
    owner = _player(1031)
    member = _player(1032)
    competition, _ = create_competition(owner, name="Join after snapshot")
    _grant_capture_consent(owner, competition)
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "owner-boundary", square, 2)
    _activity(member, "member-boundary", square, 4)

    assert dispatch_capture_calculations_task.apply(args=[1]).get() == {
        "processed": 1,
        "failed": 0,
    }
    competition.refresh_from_db()
    first = current_capture(competition)
    assert first is not None
    assert first.generation == competition.capture_revision

    joined_competition, _ = join_competition(member, invite_code=competition.invite_code)
    _grant_capture_consent(member, competition)
    assert joined_competition.revision == 0

    competition.refresh_from_db()
    assert competition.revision == 0
    queued = CaptureCalculation.objects.get(
        competition=competition, generation=competition.capture_revision
    )
    assert queued.status == CaptureCalculation.Status.PENDING
    dispatch_capture_calculations_task.apply(args=[1]).get()

    current = current_capture(competition)
    assert current is not None
    assert current.generation == competition.capture_revision
    face = current.faces.first()
    assert face is not None
    assert set(face.owners.values_list("player_id", flat=True)) == {member.pk}


def test_activity_removal_publishes_generation_without_removed_trace() -> None:
    owner = _player(1081)
    member = _player(1082)
    competition, _ = create_competition(owner, name="Activity removal")
    join_competition(member, invite_code=competition.invite_code)
    _grant_capture_consent(owner, competition)
    _grant_capture_consent(member, competition)
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "activity-owner", square, 2)
    _activity(member, "activity-removed", square, 4)
    calculate_capture(competition)

    assert remove_activity(member, "activity-removed", reason="privacy")
    competition.refresh_from_db()
    job = CompetitionRecomputation.objects.get(
        competition=competition, generation=competition.revision
    )
    recompute_competition_results_task.apply(args=[job.pk]).get()

    current = current_capture(competition)
    assert current is not None
    assert current.generation == competition.capture_revision
    assert current.trace_count == 1
    assert all(
        owner_row.player_id != member.pk
        for face in current.faces.all()
        for owner_row in face.owners.all()
    )
    assert not CapturePlayerArea.objects.filter(calculation=current, player_id=member.pk).exists()


def test_member_removal_publishes_generation_without_removed_owner() -> None:
    owner = _player(1091)
    member = _player(1092)
    competition, _ = create_competition(owner, name="Member removal")
    join_competition(member, invite_code=competition.invite_code)
    _grant_capture_consent(owner, competition)
    _grant_capture_consent(member, competition)
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "member-removal-owner", square, 2)
    _activity(member, "member-removal-member", square, 4)
    calculate_capture(competition)

    job = remove_member(owner, competition, member)
    recompute_competition_results_task.apply(args=[job.pk]).get()

    competition.refresh_from_db()
    current = current_capture(competition)
    assert current is not None
    assert current.generation == competition.capture_revision
    assert current.trace_count == 1
    assert all(
        owner_row.player_id != member.pk
        for face in current.faces.all()
        for owner_row in face.owners.all()
    )


def test_account_deletion_publishes_generation_without_deleted_member_input() -> None:
    owner = _player(1101)
    member = _player(1102)
    competition, _ = create_competition(owner, name="Account deletion")
    join_competition(member, invite_code=competition.invite_code)
    _grant_capture_consent(owner, competition)
    _grant_capture_consent(member, competition)
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "deletion-owner", square, 2)
    _activity(member, "deletion-member", square, 4)
    first = calculate_capture(competition)

    member_id = member.pk
    delete_player(member)
    first.refresh_from_db()
    assert first.faces.exists()
    assert any(
        owner_row.player_id is None for face in first.faces.all() for owner_row in face.owners.all()
    )
    competition.refresh_from_db()
    job = CompetitionRecomputation.objects.get(
        competition=competition, generation=competition.revision
    )
    recompute_competition_results_task.apply(args=[job.pk]).get()

    current = current_capture(competition)
    assert current is not None
    assert current.generation == competition.capture_revision
    assert current.trace_count == 1
    assert not Player.objects.filter(pk=member_id).exists()
    assert all(
        owner_row.player_id != member_id
        for face in current.faces.all()
        for owner_row in face.owners.all()
    )


def test_stale_generation_is_persisted_without_replacing_current_snapshot() -> None:
    owner = _player(1041)
    competition, _ = create_competition(owner, name="Stale generation")
    _grant_capture_consent(owner, competition)
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "stale-boundary", square, 2)
    first = calculate_capture(competition)

    older_job = schedule_recomputation(competition)
    older, token = claim_capture_calculation(competition, generation=competition.capture_revision)
    assert token is not None
    schedule_recomputation(competition)

    completed = calculate_capture(competition, generation=older.generation, lease_token=token)
    signature = _projection_signature(completed)
    digest = completed.input_digest
    attempts = completed.attempts

    claimed_again, retry_token = claim_capture_calculation(competition, generation=older.generation)
    retried = calculate_capture(competition, generation=older.generation)

    assert completed.status == CaptureCalculation.Status.FRESH
    assert not completed.is_current
    assert retry_token is None
    assert claimed_again.pk == completed.pk
    assert retried.pk == completed.pk
    assert retried.input_digest == digest
    assert retried.attempts == attempts
    assert _projection_signature(retried) == signature
    current = current_capture(competition)
    assert current is not None
    assert current.pk == first.pk
    assert (
        recompute_competition_results_task.apply(args=[older_job.pk]).get()["status"] == "completed"
    )


def test_replaced_capture_lease_cannot_publish_or_mark_replacement_failed() -> None:
    owner = _player(1051)
    competition, _ = create_competition(owner, name="Lease fencing")
    calculation, token = claim_capture_calculation(competition)
    assert token is not None
    replacement = "replacement-token"
    CaptureCalculation.objects.filter(pk=calculation.pk).update(
        lease_token=replacement,
        lease_until=timezone.now() + timedelta(minutes=5),
    )

    with pytest.raises(CaptureCalculationBusy):
        calculate_capture(competition, generation=calculation.generation, lease_token=token)

    calculation.refresh_from_db()
    assert calculation.status == CaptureCalculation.Status.RUNNING
    assert calculation.lease_token == replacement


def test_standalone_dispatcher_skips_recomputation_owned_generation() -> None:
    owner = _player(1061)
    competition, _ = create_competition(owner, name="Dispatcher ownership")
    dispatch_capture_calculations_task.apply(args=[1]).get()
    job = schedule_recomputation(competition)
    queued = CaptureCalculation.objects.get(competition=competition, generation=job.generation)

    assert dispatch_capture_calculations_task.apply(args=[10]).get() == {
        "processed": 0,
        "failed": 0,
    }
    queued.refresh_from_db()
    assert queued.status == CaptureCalculation.Status.PENDING


def test_algorithm_rollout_limit_counts_only_mismatched_snapshots() -> None:
    first_owner = _player(1071)
    second_owner = _player(1072)
    first, _ = create_competition(first_owner, name="Already current")
    second, _ = create_competition(second_owner, name="Needs rollout")
    calculate_capture(first)
    second_snapshot = calculate_capture(second)
    CaptureCalculation.objects.filter(pk=second_snapshot.pk).update(algorithm_version="legacy")

    assert rebuild_capture_algorithm_task.apply(args=[1]).get() == {"queued": 1}
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.revision == 0
    assert second.revision == 1
