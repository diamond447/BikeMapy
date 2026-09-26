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
    capture_traces,
    claim_capture_calculation,
    current_capture,
    schedule_capture_calculation,
)
from apps.accounts.competition_services import (
    CURRENT_SHARING_DISCLOSURE_VERSION,
    create_competition,
    grant_sharing_consent,
    join_competition,
    remove_member,
    schedule_recomputation,
    withdraw_sharing_consent,
)
from apps.accounts.models import (
    CaptureCalculation,
    CaptureFaceOwner,
    CapturePlayerArea,
    Competition,
    CompetitionRecomputation,
    CompetitionResult,
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
    face = calculation.faces.first()
    assert face is not None
    assert {trace.player_id for trace in face.owners.all()} == {owner.pk}


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


def test_capture_rejects_oversized_database_geometry_before_fetching_it() -> None:
    owner = _player(1008)
    competition, _ = create_competition(owner, name="Database geometry bounds")
    _grant_capture_consent(owner, competition)
    points = tuple((14.0 + index * 0.000001, 50.0) for index in range(30_001))
    _activity(owner, "oversized-database-geometry", points, 2)

    with patch(
        "apps.accounts.capture_services._line_coordinates",
        side_effect=AssertionError("geometry was materialized before preflight"),
    ):
        with pytest.raises(CaptureCalculationError, match="coordinate limit"):
            capture_traces(competition)


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
        tuple(sorted(row.player_id for row in face.owners.all() if row.player_id is not None))
        for face in first.faces.all()
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

    job = CompetitionRecomputation.objects.get(
        competition=competition, generation=competition.revision
    )
    outcome = recompute_competition_results_task.apply(args=[job.pk]).get()

    assert outcome["status"] == "completed"
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

    job = CompetitionRecomputation.objects.get(
        competition=competition, generation=competition.revision
    )
    assert recompute_competition_results_task.apply(args=[job.pk]).get()["status"] == "completed"
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
    job = CompetitionRecomputation.objects.get(
        competition=competition, generation=competition.revision
    )
    assert recompute_competition_results_task.apply(args=[job.pk]).get()["status"] == "completed"

    current = current_capture(competition)
    assert current is not None
    assert current.generation == competition.capture_revision
    face = current.faces.first()
    assert face is not None
    assert set(face.owners.values_list("player_id", flat=True)) == {member.pk}


def test_withdrawal_masks_current_capture_when_rebuild_fails() -> None:
    owner = _player(1035)
    competition, _ = create_competition(owner, name="Withdrawal masking")
    _grant_capture_consent(owner, competition)
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "withdrawal-mask", square, 2)
    first = calculate_capture(competition)
    first_face = first.faces.first()
    assert first_face is not None
    assert current_capture(competition) is not None

    withdraw_sharing_consent(owner, competition)
    competition.refresh_from_db()
    assert current_capture(competition) is None
    replacement = CaptureCalculation.objects.get(
        competition=competition, generation=competition.capture_revision
    )
    with patch(
        "apps.accounts.capture_services.capture_traces",
        side_effect=RuntimeError("rebuild unavailable"),
    ):
        with pytest.raises(CaptureCalculationError):
            calculate_capture(competition, generation=replacement.generation)

    first.refresh_from_db()
    assert not first.is_current
    assert first.status == CaptureCalculation.Status.FRESH
    assert first.faces.exists()
    assert CaptureFaceOwner.objects.filter(face=first_face, player=owner).exists()
    assert current_capture(competition) is None


def test_scope_downgrade_masks_current_capture_before_rebuild() -> None:
    owner = _player(1036)
    competition, _ = create_competition(owner, name="Scope downgrade masking")
    _grant_capture_consent(owner, competition)
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "scope-downgrade-mask", square, 2)
    calculate_capture(competition)
    assert current_capture(competition) is not None

    grant_sharing_consent(
        owner,
        competition,
        scope="recent",
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
        confirmed=True,
    )

    assert current_capture(competition) is None


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
    member_face_keys = set(
        CaptureFaceOwner.objects.filter(face__calculation=first, player=member).values_list(
            "owner_key", flat=True
        )
    )
    member_area_keys = set(
        CapturePlayerArea.objects.filter(calculation=first, player=member).values_list(
            "owner_key", flat=True
        )
    )
    assert member_face_keys
    assert member_area_keys

    member_id = member.pk
    delete_player(member)
    first.refresh_from_db()
    assert first.faces.exists()
    assert any(
        owner_row.player_id is None for face in first.faces.all() for owner_row in face.owners.all()
    )
    assert (
        set(
            CaptureFaceOwner.objects.filter(
                face__calculation=first, player__isnull=True
            ).values_list("owner_key", flat=True)
        )
        >= member_face_keys
    )
    assert (
        set(
            CapturePlayerArea.objects.filter(calculation=first, player__isnull=True).values_list(
                "owner_key", flat=True
            )
        )
        >= member_area_keys
    )
    assert current_capture(competition) is None
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


def test_standalone_dispatcher_uses_capture_generation_when_revisions_diverge() -> None:
    owner = _player(1062)
    competition, _ = create_competition(owner, name="Dispatcher capture generation")
    dispatch_capture_calculations_task.apply(args=[1]).get()
    job = schedule_recomputation(competition)
    replacement = schedule_capture_calculation(competition, force_new_generation=True)

    assert job.capture_generation is not None
    assert replacement.generation != job.capture_generation
    assert dispatch_capture_calculations_task.apply(args=[1]).get() == {
        "processed": 1,
        "failed": 0,
    }
    replacement.refresh_from_db()
    assert replacement.status == CaptureCalculation.Status.FRESH
    stale = CaptureCalculation.objects.get(
        competition=competition, generation=job.capture_generation
    )
    assert stale.status == CaptureCalculation.Status.PENDING
    assert stale.attempts == 0
    job.refresh_from_db()
    assert job.status == CompetitionRecomputation.Status.PENDING


def test_stale_linked_score_job_relinks_to_current_capture_and_completes() -> None:
    owner = _player(1064)
    member = _player(1065)
    competition, _ = create_competition(owner, name="Stale linked score job")
    _grant_capture_consent(owner, competition)
    square = ((14.0, 50.0), (14.01, 50.0), (14.01, 50.01), (14.0, 50.01), (14.0, 50.0))
    _activity(owner, "linked-owner", square, 2)
    calculate_capture(competition)

    # An activity change queues generation 1. The member joins before that
    # worker runs, advancing capture_revision and creating a newer capture.
    first_job = schedule_recomputation(competition)
    join_competition(member, invite_code=competition.invite_code)
    current_job = schedule_recomputation(competition)
    _activity(member, "linked-member", square, 3)
    _grant_capture_consent(member, competition)
    replacement = schedule_capture_calculation(competition, force_new_generation=True)

    competition.refresh_from_db()
    assert first_job.capture_generation is not None
    assert first_job.capture_generation < competition.capture_revision
    current_generation = competition.capture_revision
    current_job.refresh_from_db()
    assert current_job.capture_generation is not None
    assert current_job.capture_generation < current_generation
    assert replacement.generation == current_generation

    # The standalone worker may publish the newest capture first, but it must
    # leave the older linked generation owned by the score job.
    assert dispatch_capture_calculations_task.apply(args=[10]).get() == {
        "processed": 1,
        "failed": 0,
    }
    stale_capture = CaptureCalculation.objects.get(
        competition=competition, generation=first_job.capture_generation
    )
    assert stale_capture.status == CaptureCalculation.Status.PENDING

    assert (
        recompute_competition_results_task.apply(args=[first_job.pk]).get()["status"] == "completed"
    )
    first_job.refresh_from_db()
    assert first_job.capture_generation == current_generation
    assert first_job.status == CompetitionRecomputation.Status.COMPLETED

    assert (
        recompute_competition_results_task.apply(args=[current_job.pk]).get()["status"]
        == "completed"
    )
    current = current_capture(competition)
    assert current is not None
    assert current.generation == current_generation
    assert current.status == CaptureCalculation.Status.FRESH
    assert current.trace_count == 2
    results = CompetitionResult.objects.filter(competition=competition)
    assert set(results.values_list("player_id", flat=True)) == {owner.pk, member.pk}
    assert set(results.values_list("computed_revision", flat=True)) == {current_job.generation}


def test_dispatcher_respects_retryable_failed_recomputation_backoff() -> None:
    owner = _player(1063)
    competition, _ = create_competition(owner, name="Dispatcher retry ownership")
    dispatch_capture_calculations_task.apply(args=[1]).get()
    job = schedule_recomputation(competition)
    retry_generation = schedule_recomputation(competition, bump_revision=False)
    assert retry_generation.pk == job.pk
    competition.refresh_from_db()
    assert competition.revision == 1
    assert competition.capture_revision == 2

    retry_at = timezone.now() + timedelta(minutes=5)
    CompetitionRecomputation.objects.filter(pk=job.pk).update(
        status=CompetitionRecomputation.Status.FAILED,
        attempts=1,
        next_attempt_at=retry_at,
        error="temporary recomputation failure",
    )
    assert retry_generation.capture_generation is not None
    retry_capture = CaptureCalculation.objects.get(
        competition=competition, generation=retry_generation.capture_generation
    )
    outcome = dispatch_capture_calculations_task.apply(args=[10]).get()

    assert outcome == {"processed": 0, "failed": 0}
    retry_capture.refresh_from_db()
    assert retry_capture.status == CaptureCalculation.Status.PENDING
    assert current_capture(competition) is not None
    current = current_capture(competition)
    assert current is not None
    assert current.generation == 0
    stale = CaptureCalculation.objects.get(competition=competition, generation=1)
    assert stale.status == CaptureCalculation.Status.FAILED
    assert stale.attempts == 5


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
