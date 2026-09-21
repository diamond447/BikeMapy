from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.gis.geos import GEOSGeometry
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.activity_services import (
    StravaActivityError,
    _retry_after_seconds,
    activity_is_eligible,
    import_activity,
    process_sync_job,
    queue_sync,
    validate_webhook_payload,
)
from apps.accounts.activity_tasks import dispatch_strava_sync_task
from apps.accounts.competition_services import create_competition
from apps.accounts.models import (
    CompetitionMembership,
    CompetitionRecomputation,
    CompetitionResult,
    ImportedActivity,
    Player,
    PlayerCredential,
    RevocationJob,
    StravaSyncJob,
    StravaSyncState,
    StravaWebhookEvent,
)
from apps.accounts.services import delete_player, disconnect_player, save_connection

pytestmark = pytest.mark.django_db


def _settings() -> dict[str, object]:
    return {
        "GAME_ENABLED": True,
        "STRAVA_OAUTH_CLIENT_ID": "client-id",
        "STRAVA_OAUTH_CLIENT_SECRET": "webhook-secret",
        "STRAVA_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "STRAVA_IDENTITY_GUARD_KEY": "strava-identity-guard-test-key-1234567890",
        "STRAVA_WEBHOOK_VERIFY_TOKEN": "verify-token",
        "STRAVA_WEBHOOK_SUBSCRIPTION_ID": 9,
    }


def _activity(activity_id: int = 42, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": activity_id,
        "type": "Ride",
        "sport_type": "Ride",
        "visibility": "everyone",
        "private": False,
        "start_date": "2025-01-02T12:30:00Z",
        "updated_at": "2025-01-02T13:30:00Z",
        "map": {"summary_polyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"},
    }
    value.update(overrides)
    return value


def _player(athlete_id: int = 42) -> Player:
    user = get_user_model().objects.create_user(username=f"strava-{athlete_id}")
    player = Player.objects.create(user=user, strava_athlete_id=athlete_id)
    PlayerCredential.objects.create(
        player=player,
        access_token="access-token",
        refresh_token="refresh-token",
        expires_at="2033-05-18T03:33:20Z",
    )
    return player


@override_settings(**_settings())
def test_activity_eligibility_rejects_private_virtual_and_geometryless_payloads() -> None:
    assert activity_is_eligible(_activity())
    assert not activity_is_eligible(_activity(type="VirtualRide", sport_type="VirtualRide"))
    assert not activity_is_eligible(_activity(private=True))
    assert not activity_is_eligible(_activity(map={}))
    assert not activity_is_eligible(_activity(type="Run", sport_type="Run"))
    assert not activity_is_eligible(_activity(map={"summary_polyline": "truncated"}))
    assert not activity_is_eligible(_activity(map={"summary_polyline": "!" * 250_001}))
    assert import_activity(_player(43), _activity(43, athlete={"id": 999})) == "ignored"


@override_settings(**_settings())
def test_import_is_idempotent_shared_and_privacy_downgrade_removes_results() -> None:
    player = _player()
    competition, _ = create_competition(player, name="Rides")
    assert import_activity(player, _activity()) == "imported"
    assert import_activity(player, _activity()) == "updated"
    activity = ImportedActivity.objects.get(player=player)
    assert ImportedActivity.objects.filter(player=player).count() == 1
    job = CompetitionRecomputation.objects.latest("pk")
    from apps.accounts.tasks import recompute_competition_results_task

    recompute_competition_results_task.apply(args=[job.pk]).get()
    assert CompetitionResult.objects.filter(competition=competition, activity=activity).exists()
    assert import_activity(player, _activity(private=True)) == "removed"
    assert not ImportedActivity.objects.filter(pk=activity.pk).exists()
    assert not CompetitionResult.objects.filter(competition=competition, activity=activity).exists()


@override_settings(**_settings())
def test_initial_and_full_history_sync_are_bounded_and_resumable() -> None:
    player = save_connection(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 2_000_000_000,
            "scope": ["read", "activity:read"],
            "athlete": {"id": 42, "firstname": "A", "lastname": "Rider"},
        }
    )
    initial = StravaSyncJob.objects.get(player=player)
    assert initial.kind == StravaSyncJob.Kind.INITIAL
    assert StravaSyncState.objects.get(player=player).history_start is not None
    with patch(
        "apps.accounts.activity_services.fetch_activity_page", return_value=[_activity()]
    ) as fetch:
        assert process_sync_job(initial.pk) == "completed"
    fetch.assert_called_once()
    assert ImportedActivity.objects.filter(player=player).count() == 1


@override_settings(**_settings())
def test_official_webhook_challenge_replay_and_subscription_are_verified() -> None:
    _player(42)
    client = Client()
    challenge = client.get(
        reverse("game-strava-webhook"),
        {"hub.mode": "subscribe", "hub.verify_token": "verify-token", "hub.challenge": "abc"},
    )
    assert challenge.status_code == 200
    assert challenge.json() == {"hub.challenge": "abc"}
    payload = {
        "object_type": "activity",
        "object_id": 42,
        "owner_id": 42,
        "aspect_type": "delete",
        "event_time": 123,
        "subscription_id": 9,
    }
    raw = json.dumps(payload).encode()
    first = client.post(
        reverse("game-strava-webhook"),
        raw,
        content_type="application/json",
    )
    second = client.post(
        reverse("game-strava-webhook"),
        raw,
        content_type="application/json",
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert StravaWebhookEvent.objects.count() == 1
    wrong_subscription = {**payload, "subscription_id": 10}
    response = client.post(
        reverse("game-strava-webhook"),
        json.dumps(wrong_subscription).encode(),
        content_type="application/json",
    )
    assert response.status_code == 400
    spoofed_owner = {**payload, "owner_id": 999}
    response = client.post(
        reverse("game-strava-webhook"),
        json.dumps(spoofed_owner).encode(),
        content_type="application/json",
    )
    assert response.status_code == 404
    assert Player.objects.filter(strava_athlete_id=999).count() == 0


@override_settings(**_settings())
def test_webhook_refetches_authoritative_activity_and_rejects_cross_athlete() -> None:
    player = _player(43)
    client = Client()
    payload = {
        "object_type": "activity",
        "object_id": 430,
        "owner_id": 43,
        "aspect_type": "create",
        "event_time": 123,
        "subscription_id": 9,
    }
    raw = json.dumps(payload).encode()
    assert (
        client.post(
            reverse("game-strava-webhook"), raw, content_type="application/json"
        ).status_code
        == 200
    )
    event = StravaWebhookEvent.objects.get()
    job = StravaSyncJob.objects.get(webhook_event=event)
    authoritative = _activity(430, athlete={"id": 43})
    with patch("apps.accounts.activity_services.fetch_activity", return_value=authoritative):
        assert process_sync_job(job.pk) == "completed"
    assert ImportedActivity.objects.filter(player=player, provider_activity_id="430").exists()

    payload = {**payload, "object_id": 431}
    raw = json.dumps(payload).encode()
    assert (
        client.post(
            reverse("game-strava-webhook"), raw, content_type="application/json"
        ).status_code
        == 200
    )
    event = StravaWebhookEvent.objects.get(object_id=431)
    job = StravaSyncJob.objects.get(webhook_event=event)
    with patch(
        "apps.accounts.activity_services.fetch_activity",
        return_value=_activity(431, athlete={"id": 999}),
    ):
        assert process_sync_job(job.pk) == "completed"
    event.refresh_from_db()
    assert event.last_error
    assert not ImportedActivity.objects.filter(provider_activity_id="431").exists()


@override_settings(**_settings())
def test_webhook_update_accepts_official_updates_but_refetches_privacy_authoritatively() -> None:
    player = _player(44)
    payload = {
        "object_type": "activity",
        "object_id": 440,
        "owner_id": 44,
        "aspect_type": "update",
        "event_time": 123,
        "subscription_id": 9,
        "updates": {"private": True, "visibility": "only_you"},
    }
    assert validate_webhook_payload(payload) is not None
    client = Client()
    response = client.post(
        reverse("game-strava-webhook"),
        json.dumps(payload).encode(),
        content_type="application/json",
    )
    assert response.status_code == 200
    event = StravaWebhookEvent.objects.get()
    job = StravaSyncJob.objects.get(webhook_event=event)
    with patch(
        "apps.accounts.activity_services.fetch_activity",
        return_value=_activity(440, athlete={"id": 44}, private=True, visibility="only_you"),
    ) as fetch:
        assert process_sync_job(job.pk) == "completed"
    fetch.assert_called_once_with(player.pk, "440")
    assert not ImportedActivity.objects.filter(player=player, provider_activity_id="440").exists()


@override_settings(**_settings())
def test_webhook_updates_are_strictly_typed_and_scoped_to_update_events() -> None:
    base = {
        "object_type": "activity",
        "object_id": 441,
        "owner_id": 44,
        "aspect_type": "update",
        "event_time": 123,
        "subscription_id": 9,
    }
    assert validate_webhook_payload({**base, "updates": {"title": "Ride"}}) is not None
    assert validate_webhook_payload({**base, "updates": {"private": 1}}) is None
    assert validate_webhook_payload({**base, "updates": {"unknown": "value"}}) is None
    assert validate_webhook_payload({**base, "aspect_type": "create", "updates": {}}) is None
    assert validate_webhook_payload({**base, "updates": None}) is None


@override_settings(**_settings(), STRAVA_SYNC_MAX_RETRY_AFTER=30)
def test_retry_after_and_quota_reset_hints_are_defensive_and_bounded() -> None:
    request = httpx.Request("GET", "https://www.strava.com/api/v3/athlete/activities")
    huge = httpx.Response(429, headers={"Retry-After": "100000000000000000000"}, request=request)
    assert _retry_after_seconds(huge) == 30
    negative = httpx.Response(429, headers={"Retry-After": "-5"}, request=request)
    assert _retry_after_seconds(negative) is None
    future = datetime.now(UTC) + timedelta(seconds=10)
    date_hint = httpx.Response(
        429,
        headers={"Retry-After": future.strftime("%a, %d %b %Y %H:%M:%S GMT")},
        request=request,
    )
    assert 0 <= (_retry_after_seconds(date_hint) or 0) <= 30
    reset = httpx.Response(
        429,
        headers={"Retry-After": "invalid", "X-RateLimit-Reset": str(future.timestamp())},
        request=request,
    )
    assert 0 <= (_retry_after_seconds(reset) or 0) <= 30


@override_settings(**_settings(), STRAVA_SYNC_MAX_RETRY_AFTER=30)
def test_stale_worker_does_not_apply_fetched_page_after_lease_replacement() -> None:
    player = _player(45)
    job = queue_sync(player, kind=StravaSyncJob.Kind.INCREMENTAL)

    def replace_lease(*args: object, **kwargs: object) -> list[dict[str, object]]:
        current = StravaSyncJob.objects.get(pk=job.pk)
        current.lease_token = "replacement-worker"
        current.lease_until = timezone.now() + timedelta(minutes=5)
        current.save(update_fields=("lease_token", "lease_until", "updated_at"))
        return [_activity(450, athlete={"id": 45})]

    with patch("apps.accounts.activity_services.fetch_activity_page", side_effect=replace_lease):
        assert process_sync_job(job.pk) == "in_progress"
    assert not ImportedActivity.objects.filter(player=player).exists()
    state = StravaSyncState.objects.get(player=player)
    assert (state.processed_count, state.imported_count, state.rejected_count) == (0, 0, 0)


@override_settings(**_settings())
def test_activity_settings_action_queues_full_history_without_secrets() -> None:
    player = _player()
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    session_response = client.get(reverse("game-player-session"))
    response = client.post(
        reverse("game-player-full-history"),
        HTTP_X_CSRFTOKEN=session_response.cookies["csrftoken"].value,
    )
    assert response.status_code == 200
    assert response.json()["sync"]["mode"] == "full-history"
    assert "access-token" not in json.dumps(response.json())
    assert StravaSyncJob.objects.filter(player=player, kind="full-history").exists()


@override_settings(**_settings())
def test_rate_limit_failure_is_visible_and_retryable_without_credentials() -> None:
    player = _player(77)
    job = StravaSyncJob.objects.create(
        player=player,
        kind=StravaSyncJob.Kind.INITIAL,
        idempotency_key="rate-limit-job",
    )
    with patch(
        "apps.accounts.activity_services.fetch_activity_page",
        side_effect=StravaActivityError("Strava rate limit reached.", retry_after=47),
    ):
        assert process_sync_job(job.pk) == "failed"
    job.refresh_from_db()
    assert job.last_error == "Strava rate limit reached."
    assert job.next_attempt_at > job.created_at
    assert "access-token" not in job.last_error


@override_settings(**_settings())
def test_older_webhook_delete_cannot_remove_newer_activity() -> None:
    player = _player(88)
    import_activity(player, _activity(activity_id=88))
    payload = {
        "object_type": "activity",
        "object_id": 88,
        "owner_id": 88,
        "aspect_type": "delete",
        "event_time": 1,
    }
    from apps.accounts.activity_services import queue_webhook_event

    event = queue_webhook_event(payload, "older-delete")
    job = StravaSyncJob.objects.get(webhook_event=event)
    assert process_sync_job(job.pk) == "completed"
    assert ImportedActivity.objects.filter(player=player, provider_activity_id="88").exists()


@override_settings(**_settings(), STRAVA_SYNC_PAGE_SIZE=2, STRAVA_SYNC_PAGES_PER_RUN=1)
def test_sync_cursor_resumes_after_each_bounded_page() -> None:
    player = _player(90)
    job = queue_sync(player, kind=StravaSyncJob.Kind.INCREMENTAL)
    pages = [
        [_activity(90), _activity(91)],
        [_activity(92), _activity(93)],
        [_activity(94)],
    ]
    with patch("apps.accounts.activity_services.fetch_activity_page", side_effect=pages) as fetch:
        assert process_sync_job(job.pk) == "more"
        job.refresh_from_db()
        assert job.page == 2
        assert StravaSyncState.objects.get(player=player).cursor_page == 2
        assert process_sync_job(job.pk) == "more"
        job.refresh_from_db()
        assert job.page == 3
        assert process_sync_job(job.pk) == "completed"
    assert fetch.call_args_list[0].kwargs["page"] == 1
    assert fetch.call_args_list[1].kwargs["page"] == 2
    assert fetch.call_args_list[2].kwargs["page"] == 3
    assert ImportedActivity.objects.filter(player=player).count() == 5


@override_settings(**_settings())
def test_api_refreshes_expired_activity_token_once() -> None:
    player = _player(91)
    import httpx

    first = httpx.Response(
        401,
        json={"message": "unauthorized"},
        request=httpx.Request("GET", "https://www.strava.com/api/v3/athlete/activities"),
    )
    second = httpx.Response(
        200,
        json=[],
        request=httpx.Request("GET", "https://www.strava.com/api/v3/athlete/activities"),
    )
    refreshed = httpx.Response(
        200,
        json={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": 2_000_000_000,
        },
        request=httpx.Request("POST", "https://www.strava.com/oauth/token"),
    )
    with (
        patch("apps.accounts.activity_services.httpx.get", side_effect=[first, second]) as get,
        patch("apps.accounts.services.httpx.post", return_value=refreshed) as post,
    ):
        from apps.accounts.activity_services import fetch_activity_page

        assert fetch_activity_page(player.pk, page=1) == []
    assert get.call_count == 2
    assert post.call_count == 1
    player.credential.refresh_from_db()
    assert player.credential.access_token == "new-access"


@override_settings(**_settings())
def test_duplicate_dispatch_has_one_database_claim() -> None:
    player = _player(92)
    job = queue_sync(player, kind=StravaSyncJob.Kind.INCREMENTAL)
    with patch("apps.accounts.activity_tasks.sync_strava_activities_task.apply_async") as publish:
        assert dispatch_strava_sync_task(limit=1) == {"dispatched": 1}
        assert dispatch_strava_sync_task(limit=1) == {"dispatched": 0}
    assert publish.call_count == 1
    job.refresh_from_db()
    assert job.dispatch_token
    job.dispatch_lease_until = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=("dispatch_lease_until",))
    with patch("apps.accounts.activity_tasks.sync_strava_activities_task.apply_async") as publish:
        assert dispatch_strava_sync_task(limit=1) == {"dispatched": 1}
    assert publish.call_count == 1


@override_settings(**_settings())
def test_dispatch_recovers_expired_worker_lease_but_not_fresh_worker() -> None:
    player = _player(95)
    stale = StravaSyncJob.objects.create(
        player=player,
        kind=StravaSyncJob.Kind.INCREMENTAL,
        idempotency_key="stale-worker",
        status=StravaSyncJob.Status.RUNNING,
        lease_token="dead-worker",
        lease_until=timezone.now() - timedelta(seconds=1),
    )
    fresh = StravaSyncJob.objects.create(
        player=player,
        kind=StravaSyncJob.Kind.INCREMENTAL,
        idempotency_key="fresh-worker",
        status=StravaSyncJob.Status.RUNNING,
        lease_token="live-worker",
        lease_until=timezone.now() + timedelta(minutes=5),
    )
    with patch("apps.accounts.activity_tasks.sync_strava_activities_task.apply_async") as publish:
        assert dispatch_strava_sync_task(limit=10) == {"dispatched": 1}
    stale.refresh_from_db()
    fresh.refresh_from_db()
    assert stale.status == StravaSyncJob.Status.FAILED
    assert stale.dispatch_token
    assert fresh.status == StravaSyncJob.Status.RUNNING
    assert fresh.lease_token == "live-worker"
    assert publish.call_count == 1


@override_settings(**_settings())
def test_disconnect_pauses_sync_retains_activity_and_reconnect_cancels_deletion() -> None:
    player = _player(93)
    import_activity(player, _activity(93))
    queue_sync(player, kind=StravaSyncJob.Kind.INCREMENTAL)
    disconnect_player(player, revoke=False)
    player.refresh_from_db()
    assert player.lifecycle == Player.Lifecycle.PENDING_DELETION
    assert player.deletion_deadline is not None
    assert not PlayerCredential.objects.filter(player=player).exists()
    assert ImportedActivity.objects.filter(player=player).exists()
    assert StravaSyncState.objects.get(player=player).status == StravaSyncState.Status.PAUSED
    assert not StravaSyncJob.objects.filter(
        player=player, status=StravaSyncJob.Status.PENDING
    ).exists()

    reconnected = save_connection(
        {
            "access_token": "access-again",
            "refresh_token": "refresh-again",
            "expires_at": 2_000_000_000,
            "scope": ["read", "activity:read"],
            "athlete": {"id": 93, "firstname": "A", "lastname": "Rider"},
        }
    )
    assert reconnected.pk == player.pk
    reconnected.refresh_from_db()
    assert reconnected.lifecycle == Player.Lifecycle.CONNECTED
    assert reconnected.deletion_deadline is None
    assert PlayerCredential.objects.filter(player=player).exists()


@override_settings(**_settings())
def test_account_deletion_purges_activity_competition_and_credentials() -> None:
    player = _player(94)
    competition, _ = create_competition(player, name="Purge")
    activity = ImportedActivity.objects.create(
        player=player,
        provider_activity_id="94",
        calendar_date="2026-09-21",
        geometry=GEOSGeometry(
            '{"type":"LineString","coordinates":[[14.4,50.0],[14.5,50.1]]}', srid=4326
        ),
    )
    CompetitionResult.objects.create(competition=competition, player=player, activity=activity)
    user_id = player.user_id
    with patch("apps.accounts.services._revoke_access_token", return_value=True):
        delete_player(player)
    assert not Player.objects.filter(pk=player.pk).exists()
    assert not PlayerCredential.objects.filter(player_id=player.pk).exists()
    assert not ImportedActivity.objects.filter(pk=activity.pk).exists()
    assert not CompetitionResult.objects.filter(activity_id=activity.pk).exists()
    assert not CompetitionMembership.objects.filter(player_id=player.pk).exists()
    assert not StravaSyncJob.objects.filter(player_id=player.pk).exists()
    assert not StravaSyncState.objects.filter(player_id=player.pk).exists()
    assert not RevocationJob.objects.filter(access_token__isnull=False).exists()
    assert not get_user_model().objects.filter(pk=user_id).exists()
