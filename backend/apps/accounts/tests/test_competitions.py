from __future__ import annotations

from datetime import timedelta
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.competition_services import (
    CompetitionError,
    create_competition,
    delete_competition,
    join_competition,
    leave_competition,
    remove_member,
    schedule_recomputation,
    set_member_color,
    transfer_ownership,
)
from apps.accounts.models import (
    Competition,
    CompetitionMembership,
    CompetitionRecomputation,
    CompetitionResult,
    ImportedActivity,
    Player,
)
from apps.accounts.tasks import (
    dispatch_competition_recomputations_task,
    recompute_competition_results_task,
)

pytestmark = pytest.mark.django_db


SETTINGS = {
    "GAME_ENABLED": True,
    "STRAVA_OAUTH_CLIENT_ID": "client-id",
    "STRAVA_OAUTH_CLIENT_SECRET": "client-secret",
    "STRAVA_TOKEN_ENCRYPTION_KEY": "test-key",
    "STRAVA_IDENTITY_GUARD_KEY": "test-identity-key",
}


def player(athlete_id: int) -> Player:
    user = get_user_model().objects.create_user(username=f"player-{athlete_id}")
    return Player.objects.create(
        user=user, strava_athlete_id=athlete_id, strava_display_name=f"Rider {athlete_id}"
    )


def authenticated_client(current: Player) -> Client:
    client = Client()
    session = client.session
    session["player_id"] = current.pk
    session["player_session_epoch"] = current.session_epoch
    session.save()
    return client


@override_settings(**SETTINGS)
def test_create_join_switch_and_non_member_access_is_not_enumerable() -> None:
    owner = player(1)
    member = player(2)
    outsider = player(3)
    owner_client = authenticated_client(owner)
    created = owner_client.post(
        reverse("game-competition-list"), {"name": "Dawn rides", "color": "#E45756"}
    )
    assert created.status_code == 201
    competition = created.json()["competition"]
    code = competition["invite_code"]

    member_client = authenticated_client(member)
    joined = member_client.post(
        reverse("game-competition-join"), {"invite_code": code, "color": "#3A86FF"}
    )
    assert joined.status_code == 200
    assert joined.json()["competition"]["is_owner"] is False
    assert member_client.get(reverse("game-competition-list")).json()["active_competition_id"]

    outsider_client = authenticated_client(outsider)
    identifier = competition["id"]
    assert (
        outsider_client.get(reverse("game-competition-detail", args=[identifier])).status_code
        == 404
    )
    assert (
        outsider_client.post(reverse("game-competition-switch", args=[identifier])).status_code
        == 404
    )
    assert outsider_client.get(reverse("game-competition-list")).json()["competitions"] == []

    switched = member_client.post(reverse("game-competition-switch", args=[identifier]))
    assert switched.status_code == 200


@override_settings(**SETTINGS)
def test_rotation_invalidates_old_code_and_owner_actions_are_object_scoped() -> None:
    owner = player(10)
    member = player(11)
    competition, _ = create_competition(owner, name="Rotating code")
    old_code = competition.invite_code
    owner_client = authenticated_client(owner)
    rotated = owner_client.post(reverse("game-competition-rotate-invite", args=[competition.pk]))
    assert rotated.status_code == 200
    new_code = rotated.json()["competition"]["invite_code"]
    assert new_code != old_code
    assert (
        authenticated_client(member)
        .post(reverse("game-competition-join"), {"invite_code": old_code})
        .status_code
        == 400
    )
    assert (
        authenticated_client(member)
        .post(reverse("game-competition-join"), {"invite_code": new_code})
        .status_code
        == 200
    )
    assert (
        authenticated_client(member)
        .post(reverse("game-competition-rotate-invite", args=[competition.pk]))
        .status_code
        == 403
    )


@override_settings(**SETTINGS)
def test_owner_leave_transfer_removal_cleanup_and_deterministic_recompute() -> None:
    owner = player(20)
    member = player(21)
    competition, _ = create_competition(owner, name="Cleanup")
    join_competition(member, invite_code=competition.invite_code, color="#3A86FF")
    activity = ImportedActivity.objects.create(player=member, provider_activity_id="strava-1")
    result = CompetitionResult.objects.create(
        competition=competition, player=member, activity=activity, points=42
    )
    owner_result = CompetitionResult.objects.create(
        competition=competition, player=owner, activity=activity, points=7
    )
    with pytest.raises(CompetitionError, match="Transfer ownership"):
        leave_competition(owner, competition)
    transfer_ownership(owner, competition, member)
    leave_competition(owner, competition)
    result.refresh_from_db()
    assert not CompetitionMembership.objects.filter(competition=competition, player=owner).exists()
    assert CompetitionResult.objects.filter(pk=result.pk).exists()
    assert not CompetitionResult.objects.filter(pk=owner_result.pk).exists()
    assert ImportedActivity.objects.filter(pk=activity.pk).exists()
    competition.refresh_from_db()
    job = CompetitionRecomputation.objects.latest("pk")
    assert job.generation == competition.revision
    assert job.affected_player_id == owner.pk
    first = recompute_competition_results_task.apply(args=[job.pk]).get()
    second = recompute_competition_results_task.apply(args=[job.pk]).get()
    assert first["status"] == second["status"] == "completed"
    assert second["updated"] == 0


@override_settings(**SETTINGS)
def test_colors_are_per_competition_and_near_collisions_are_rejected() -> None:
    owner = player(30)
    member = player(31)
    other = player(32)
    competition, _ = create_competition(owner, name="Colors", color="#00FF00")
    join_competition(member, invite_code=competition.invite_code, color="#0000FF")
    with pytest.raises(CompetitionError, match="too close"):
        join_competition(other, invite_code=competition.invite_code, color="#0100FF")
    with pytest.raises(CompetitionError, match="too close"):
        set_member_color(member, competition, color="#00FE00")


@override_settings(**SETTINGS)
def test_delete_competition_preserves_player_and_shared_activity() -> None:
    owner = player(40)
    activity = ImportedActivity.objects.create(player=owner, provider_activity_id="strava-2")
    competition, _ = create_competition(owner, name="Delete me")
    CompetitionResult.objects.create(competition=competition, player=owner, activity=activity)
    delete_competition(owner, competition)
    assert Player.objects.filter(pk=owner.pk).exists()
    assert ImportedActivity.objects.filter(pk=activity.pk).exists()
    assert not CompetitionMembership.objects.filter(competition=competition).exists()


@override_settings(**SETTINGS)
def test_remove_member_is_not_allowed_for_non_owner() -> None:
    owner = player(50)
    member = player(51)
    competition, _ = create_competition(owner, name="Roles")
    join_competition(member, invite_code=competition.invite_code)
    with pytest.raises(CompetitionError, match="owner"):
        remove_member(member, competition, owner)


@override_settings(**SETTINGS)
def test_non_member_cannot_distinguish_missing_competition_and_private_headers_are_consistent() -> (
    None
):
    owner = player(60)
    outsider = player(61)
    competition, _ = create_competition(owner, name="Hidden")
    client = authenticated_client(outsider)
    inaccessible = client.get(reverse("game-competition-detail", args=[competition.pk]))
    missing = client.get(reverse("game-competition-detail", args=[uuid4()]))
    assert inaccessible.status_code == missing.status_code == 404
    assert inaccessible.json() == missing.json() == {"detail": "Competition not found."}
    assert inaccessible["Cache-Control"] == missing["Cache-Control"] == "private, no-store"


@override_settings(**SETTINGS)
def test_validation_and_csrf_failures_use_private_structured_json() -> None:
    owner = player(62)
    validation = authenticated_client(owner).post(reverse("game-competition-list"), {})
    assert validation.status_code == 400
    assert validation.json()["code"] == "validation_error"
    assert validation["Cache-Control"] == "private, no-store"

    csrf_client = Client(enforce_csrf_checks=True)
    session = csrf_client.session
    session["player_id"] = owner.pk
    session["player_session_epoch"] = owner.session_epoch
    session.save()
    csrf = csrf_client.post(reverse("game-competition-list"), {"name": "Blocked"})
    assert csrf.status_code == 403
    assert csrf.json() == {"detail": "CSRF validation failed.", "code": "csrf_failed"}
    assert csrf["Cache-Control"] == "private, no-store"


@override_settings(**SETTINGS)
def test_removal_selects_remaining_membership_and_preserves_newer_generation() -> None:
    owner = player(70)
    member = player(71)
    other = player(72)
    first, _ = create_competition(owner, name="First")
    second, _ = create_competition(other, name="Second")
    join_competition(member, invite_code=first.invite_code)
    member.refresh_from_db()
    member.active_competition = first
    member.save(update_fields=("active_competition",))
    join_competition(member, invite_code=second.invite_code)
    member.refresh_from_db()
    remove_member(owner, first, member)
    member.refresh_from_db()
    assert member.active_competition_id == second.pk

    first.refresh_from_db()
    newer = CompetitionRecomputation.objects.get(competition=first, generation=first.revision)
    older = CompetitionRecomputation.objects.create(competition=first, generation=0)
    result = CompetitionResult.objects.create(competition=first, player=owner, points=3)
    recompute_competition_results_task.apply(args=[newer.pk]).get()
    recompute_competition_results_task.apply(args=[older.pk]).get()
    result.refresh_from_db()
    assert result.computed_revision == 2


@override_settings(**SETTINGS)
def test_color_comparison_rejects_low_delta_e_cyan_variants() -> None:
    owner = player(80)
    member = player(81)
    competition, _ = create_competition(owner, name="Contrast", color="#00FFFF")
    with pytest.raises(CompetitionError, match="too close"):
        join_competition(member, invite_code=competition.invite_code, color="#48FFFF")


@override_settings(**SETTINGS)
def test_invite_creation_and_rotation_retry_integrity_collisions() -> None:
    owner = player(90)
    original_create = Competition.objects.create
    create_calls = 0

    def flaky_create(**fields: object) -> Competition:
        nonlocal create_calls
        create_calls += 1
        if create_calls == 1:
            from django.db import IntegrityError

            raise IntegrityError("invite race")
        return original_create(**fields)

    with patch.object(Competition.objects, "create", side_effect=flaky_create):
        competition, _ = create_competition(owner, name="Retry")
    assert competition.invite_code and create_calls == 2

    save_calls = 0

    def flaky_save(*args: object, **kwargs: object) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            from django.db import IntegrityError

            raise IntegrityError("invite race")

    with patch.object(Competition, "save", side_effect=flaky_save):
        from apps.accounts.competition_services import rotate_invite_code

        rotate_invite_code(owner, competition)
    assert save_calls == 2


@override_settings(**SETTINGS)
def test_dispatch_failure_is_recorded_for_sweeper_retry() -> None:
    owner = player(100)
    competition, _ = create_competition(owner, name="Outbox")
    from apps.accounts.competition_services import _dispatch_recomputation

    started = monotonic()
    with patch(
        "apps.accounts.tasks.recompute_competition_results_task.apply_async",
        side_effect=RuntimeError("redis down"),
    ) as publish:
        job = schedule_recomputation(competition)
        assert monotonic() - started < 1
        publish.assert_not_called()
        _dispatch_recomputation(job.pk)
    job.refresh_from_db()
    assert job.status == CompetitionRecomputation.Status.PENDING
    assert job.attempts == 1
    assert job.error == "redis down"


@override_settings(**SETTINGS)
def test_recompute_failure_persists_attempts_and_stops_at_retry_limit() -> None:
    owner = player(110)
    competition, _ = create_competition(owner, name="Failure")
    job = CompetitionRecomputation.objects.create(competition=competition, generation=1)
    result = CompetitionResult.objects.create(competition=competition, player=owner, points=1)
    with patch.object(CompetitionResult, "save", side_effect=RuntimeError("score failure")):
        first = recompute_competition_results_task.apply(args=[job.pk]).get()
    job.refresh_from_db()
    assert first["status"] == "failed"
    assert job.attempts == 1
    job.attempts = 4
    job.status = CompetitionRecomputation.Status.FAILED
    job.next_attempt_at = timezone.now()
    job.save(update_fields=("attempts", "status", "next_attempt_at"))
    with patch.object(CompetitionResult, "save", side_effect=RuntimeError("score failure")):
        terminal = recompute_competition_results_task.apply(args=[job.pk]).get()
    job.refresh_from_db()
    result.refresh_from_db()
    assert terminal["status"] == "failed"
    assert job.attempts == 5
    assert job.error


@override_settings(**SETTINGS)
def test_dispatch_failure_after_deletion_does_not_stop_the_batch() -> None:
    owner = player(111)
    competition, _ = create_competition(owner, name="Deleted outbox")
    first = schedule_recomputation(competition)
    second = schedule_recomputation(competition)

    def publish(args: tuple[int]) -> None:
        if args[0] == first.pk:
            CompetitionRecomputation.objects.filter(pk=first.pk).delete()
            raise RuntimeError("redis down")

    with patch(
        "apps.accounts.tasks.recompute_competition_results_task.apply_async",
        side_effect=publish,
    ):
        result = dispatch_competition_recomputations_task.apply(args=[2]).get()

    assert result == {"dispatched": 1}
    second.refresh_from_db()
    assert second.dispatched_at is not None


@override_settings(**SETTINGS)
def test_dispatch_claim_skips_competition_deleted_after_reference_read() -> None:
    first_owner = player(112)
    second_owner = player(113)
    first_competition, _ = create_competition(first_owner, name="Deleted competition")
    second_competition, _ = create_competition(second_owner, name="Surviving competition")
    first = schedule_recomputation(first_competition)
    second = schedule_recomputation(second_competition)
    original_get = CompetitionRecomputation.objects.get
    deleted = False

    def read_then_delete(*args: object, **kwargs: object) -> CompetitionRecomputation:
        nonlocal deleted
        job = original_get(*args, **kwargs)
        if not deleted and job.pk == first.pk:
            deleted = True
            Competition.objects.filter(pk=first_competition.pk).delete()
        return job

    with (
        patch.object(CompetitionRecomputation.objects, "get", side_effect=read_then_delete),
        patch("apps.accounts.tasks.recompute_competition_results_task.apply_async") as publish,
    ):
        result = dispatch_competition_recomputations_task.apply(args=[2]).get()

    assert result == {"dispatched": 1}
    publish.assert_called_once_with(args=(second.pk,))
    second.refresh_from_db()
    assert second.dispatched_at is not None


@override_settings(**SETTINGS)
def test_stale_worker_recovery_skips_competition_deleted_after_reference_read() -> None:
    first_owner = player(114)
    second_owner = player(115)
    first_competition, _ = create_competition(first_owner, name="Expired deleted")
    second_competition, _ = create_competition(second_owner, name="Expired surviving")
    first = CompetitionRecomputation.objects.create(
        competition=first_competition,
        generation=1,
        status=CompetitionRecomputation.Status.RUNNING,
        attempts=1,
        lease_token="dead-first",
        lease_until=timezone.now() - timedelta(minutes=1),
    )
    second = CompetitionRecomputation.objects.create(
        competition=second_competition,
        generation=1,
        status=CompetitionRecomputation.Status.RUNNING,
        attempts=1,
        lease_token="dead-second",
        lease_until=timezone.now() - timedelta(minutes=1),
    )
    original_get = CompetitionRecomputation.objects.get
    deleted = False

    def read_then_delete(*args: object, **kwargs: object) -> CompetitionRecomputation:
        nonlocal deleted
        job = original_get(*args, **kwargs)
        if not deleted and job.pk == first.pk:
            deleted = True
            Competition.objects.filter(pk=first_competition.pk).delete()
        return job

    with patch.object(CompetitionRecomputation.objects, "get", side_effect=read_then_delete):
        result = dispatch_competition_recomputations_task.apply(args=[2]).get()

    assert result == {"dispatched": 0}
    second.refresh_from_db()
    assert second.status == CompetitionRecomputation.Status.FAILED
    assert second.error == "Worker lease expired."


@override_settings(**SETTINGS)
def test_recomputation_worker_lease_distinguishes_fresh_and_stale_jobs() -> None:
    owner = player(120)
    competition, _ = create_competition(owner, name="Leases")
    job = CompetitionRecomputation.objects.create(
        competition=competition,
        generation=1,
        status=CompetitionRecomputation.Status.RUNNING,
        attempts=1,
        lease_token="live-worker",
        lease_until=timezone.now() + timedelta(minutes=5),
    )
    fresh = recompute_competition_results_task.apply(args=[job.pk]).get()
    assert fresh["status"] == "in_progress"
    job.refresh_from_db()
    assert job.attempts == 1

    job.lease_until = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=("lease_until",))
    recovered = recompute_competition_results_task.apply(args=[job.pk]).get()
    assert recovered["status"] == "completed"
    job.refresh_from_db()
    assert job.attempts == 2
    assert job.lease_token == ""
    assert job.lease_until is None


@override_settings(**SETTINGS)
def test_stale_worker_lease_is_recovered_by_dispatch_sweeper() -> None:
    owner = player(121)
    competition, _ = create_competition(owner, name="Expired lease")
    job = CompetitionRecomputation.objects.create(
        competition=competition,
        generation=1,
        status=CompetitionRecomputation.Status.RUNNING,
        attempts=1,
        lease_token="dead-worker",
        lease_until=timezone.now() - timedelta(minutes=1),
    )
    result = dispatch_competition_recomputations_task.apply(args=[1]).get()
    assert result == {"dispatched": 0}
    job.refresh_from_db()
    assert job.status == CompetitionRecomputation.Status.FAILED
    assert job.error == "Worker lease expired."
    assert job.lease_token == ""
    assert job.lease_until is None


@override_settings(**SETTINGS)
def test_dispatch_claim_is_single_use_until_its_lease_expires() -> None:
    owner = player(122)
    competition, _ = create_competition(owner, name="Dispatch claim")
    job = schedule_recomputation(competition)
    from apps.accounts.competition_services import _claim_recomputation_dispatch

    first = _claim_recomputation_dispatch(job.pk)
    second = _claim_recomputation_dispatch(job.pk)
    assert first is not None
    assert second is None
    job.refresh_from_db()
    assert job.dispatch_token == first[1]
    assert job.dispatch_lease_until is not None
