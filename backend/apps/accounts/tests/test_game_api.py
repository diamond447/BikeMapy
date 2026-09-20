from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import (
    OAuthState,
    Player,
    PlayerCredential,
    PlayerDeletionTombstone,
    RevocationJob,
)
from apps.accounts.services import purge_expired_players, retry_revocations, save_connection

pytestmark = pytest.mark.django_db


def _settings() -> dict[str, object]:
    return {
        "GAME_ENABLED": True,
        "STRAVA_OAUTH_CLIENT_ID": "client-id",
        "STRAVA_OAUTH_CLIENT_SECRET": "client-secret",
        "STRAVA_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }


def _payload(athlete_id: int = 123) -> dict[str, object]:
    return {
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "expires_at": 2_000_000_000,
        "scope": ["read", "activity:read"],
        "athlete": {
            "id": athlete_id,
            "firstname": "Ada",
            "lastname": "Cyclist",
            "profile": "https://example.test/profile.png",
        },
    }


def test_game_is_disabled_by_default() -> None:
    response = Client().get(reverse("game-strava-authorize"))
    assert response.status_code == 404
    assert not OAuthState.objects.exists()


@override_settings(**_settings())
def test_oauth_state_is_bound_to_session_and_callback_is_single_use() -> None:
    client = Client()
    response = client.get(reverse("game-strava-authorize"))
    assert response.status_code == 302
    original_session_key = client.session.session_key
    state = str(response["Location"]).split("state=", 1)[1]
    assert OAuthState.objects.count() == 1

    token_response = Mock()
    token_response.json.return_value = _payload()
    token_response.raise_for_status.return_value = None
    with patch("apps.accounts.services.httpx.post", return_value=token_response):
        callback = client.get(reverse("game-strava-callback"), {"state": state, "code": "one-time"})
    assert callback.status_code == 302
    assert callback["Location"].endswith("/game?game_auth=success")
    assert client.session.session_key != original_session_key
    assert Player.objects.get().strava_athlete_id == 123
    assert PlayerCredential.objects.get().access_token == "access-secret"

    replay = client.get(reverse("game-strava-callback"), {"state": state, "code": "one-time"})
    assert replay.status_code == 302
    assert replay["Location"].endswith("/game?game_auth=error")
    assert Player.objects.count() == 1


@override_settings(**_settings())
def test_denied_callback_consumes_state_without_creating_player() -> None:
    client = Client()
    authorize = client.get(reverse("game-strava-authorize"))
    state = str(authorize["Location"]).split("state=", 1)[1]
    response = client.get(
        reverse("game-strava-callback"), {"state": state, "error": "access_denied"}
    )
    assert response.status_code == 302
    assert response["Location"].endswith("/game?game_auth=denied")
    assert Player.objects.count() == 0
    assert OAuthState.objects.get().used_at is not None


@override_settings(**_settings())
def test_github_owner_session_does_not_grant_player_access() -> None:
    user = get_user_model().objects.create_user(username="owner")
    Player.objects.create(user=user, strava_athlete_id=123)
    client = Client()
    client.force_login(user)
    session = client.session
    session["account_authentication_methods"] = [
        {"method": "socialaccount", "provider": "github", "uid": "999"}
    ]
    session.save()
    response = client.get(reverse("game-player-account"))
    assert response.status_code == 401


@override_settings(**_settings())
def test_nickname_and_disconnect_do_not_expose_credentials() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(
        user=user, strava_athlete_id=123, strava_display_name="Ada Cyclist"
    )
    PlayerCredential.objects.create(
        player=player,
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at="2033-05-18T03:33:20Z",
    )
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    response = client.patch(
        reverse("game-player-account"), {"nickname": "Ada"}, content_type="application/json"
    )
    assert response.status_code == 200
    assert response.json()["player"]["nickname"] == "Ada"
    assert "access_token" not in response.json()["player"]
    with patch("apps.accounts.services.httpx.post") as revoke:
        disconnected = client.post(reverse("game-player-disconnect"))
    assert disconnected.status_code == 200
    revoke.assert_called_once()
    assert revoke.call_args.kwargs["data"] == {"access_token": "access-secret"}
    assert "params" not in revoke.call_args.kwargs
    assert not PlayerCredential.objects.filter(player=player).exists()
    player.refresh_from_db()
    assert player.strava_display_name == ""
    assert player.strava_profile_image_url == ""
    assert player.nickname == ""


@override_settings(**_settings())
def test_failed_revocation_is_persisted_as_bounded_encrypted_retry_job() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    PlayerCredential.objects.create(
        player=player,
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at="2033-05-18T03:33:20Z",
    )
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    response = Mock()
    response.raise_for_status.side_effect = httpx.ConnectError("provider unavailable")
    with patch("apps.accounts.services.httpx.post", return_value=response):
        assert client.post(reverse("game-player-disconnect")).status_code == 200
    job = RevocationJob.objects.get()
    assert job.attempts == 1
    assert job.status == RevocationJob.Status.PENDING
    assert job.access_token == "access-secret"
    with connection.cursor() as cursor:
        cursor.execute("SELECT access_token FROM accounts_revocationjob")
        assert "access-secret" not in cursor.fetchone()[0]


@override_settings(**_settings())
def test_refresh_network_failure_is_retryable_and_keeps_session() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    PlayerCredential.objects.create(
        player=player,
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at="2033-05-18T03:33:20Z",
    )
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    response = Mock()
    response.raise_for_status.side_effect = httpx.ConnectError("provider unavailable")
    with patch("apps.accounts.services.httpx.post", return_value=response):
        refreshed = client.post(reverse("game-player-refresh"))
    assert refreshed.status_code == 503
    player.refresh_from_db()
    assert player.lifecycle == Player.Lifecycle.CONNECTED
    assert PlayerCredential.objects.filter(player=player).exists()
    assert client.get(reverse("game-player-account")).status_code == 200


@override_settings(**_settings())
def test_refresh_success_rotates_encrypted_credentials_without_exposing_them() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    PlayerCredential.objects.create(
        player=player,
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at="2033-05-18T03:33:20Z",
    )
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    response = Mock()
    response.json.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_at": 2_000_000_000,
        "scope": ["read", "activity:read"],
    }
    response.raise_for_status.return_value = None
    with patch("apps.accounts.services.httpx.post", return_value=response) as refresh:
        refreshed = client.post(reverse("game-player-refresh"))
    assert refreshed.status_code == 200
    assert refreshed.json()["player"]["athlete_id"] == "123"
    refresh.assert_called_once()
    assert refresh.call_args.kwargs["data"]["refresh_token"] == "old-refresh"
    credential = PlayerCredential.objects.get(player=player)
    assert credential.access_token == "new-access"
    assert credential.refresh_token == "new-refresh"
    with connection.cursor() as cursor:
        cursor.execute("SELECT access_token, refresh_token FROM accounts_playercredential")
        raw_access, raw_refresh = cursor.fetchone()
    assert "new-access" not in raw_access
    assert "new-refresh" not in raw_refresh


@override_settings(**_settings())
def test_reconnection_updates_existing_athlete_without_creating_duplicate() -> None:
    client = Client()
    first = client.get(reverse("game-strava-authorize"))
    first_state = str(first["Location"]).split("state=", 1)[1]
    token_response = Mock()
    token_response.json.return_value = _payload()
    token_response.raise_for_status.return_value = None
    with patch("apps.accounts.services.httpx.post", return_value=token_response):
        assert (
            client.get(
                reverse("game-strava-callback"), {"state": first_state, "code": "first"}
            ).status_code
            == 302
        )
    player = Player.objects.get()
    player.mark_disconnected()
    second = client.get(reverse("game-strava-authorize"))
    second_state = str(second["Location"]).split("state=", 1)[1]
    athlete = _payload()["athlete"]
    assert isinstance(athlete, dict)
    token_response.json.return_value = {
        **_payload(),
        "athlete": {**athlete, "firstname": "Grace"},
    }
    with patch("apps.accounts.services.httpx.post", return_value=token_response):
        callback = client.get(
            reverse("game-strava-callback"), {"state": second_state, "code": "second"}
        )
    assert callback.status_code == 302
    assert callback["Location"].endswith("/game?game_auth=success")
    assert Player.objects.count() == 1
    player.refresh_from_db()
    assert player.lifecycle == Player.Lifecycle.CONNECTED
    assert player.strava_display_name == "Grace Cyclist"


@override_settings(**_settings())
def test_account_deletion_revokes_and_erases_player_and_credentials() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    PlayerCredential.objects.create(
        player=player,
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at="2033-05-18T03:33:20Z",
    )
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    with patch("apps.accounts.services.httpx.post") as revoke:
        response = client.delete(reverse("game-player-account"))
    assert response.status_code == 204
    revoke.assert_called_once()
    assert not Player.objects.exists()
    assert not get_user_model().objects.filter(pk=user.pk).exists()


@override_settings(**_settings())
def test_oauth_callback_state_cannot_cross_sessions() -> None:
    first_client = Client()
    response = first_client.get(reverse("game-strava-authorize"))
    state = str(response["Location"]).split("state=", 1)[1]
    callback = Client().get(reverse("game-strava-callback"), {"state": state, "code": "x"})
    assert callback.status_code == 302
    assert callback["Location"].endswith("/game?game_auth=error")
    assert not Player.objects.exists()


@override_settings(**_settings(), GAME_FRONTEND_URL="https://game.example.test/game")
def test_oauth_result_uses_only_the_validated_frontend_game_url() -> None:
    client = Client()
    authorize = client.get(reverse("game-strava-authorize"))
    state = str(authorize["Location"]).split("state=", 1)[1]
    response = client.get(
        reverse("game-strava-callback"),
        {"state": state, "error": "access_denied", "next": "https://evil.example/"},
    )
    assert response["Location"] == "https://game.example.test/game?game_auth=denied"


def test_configured_cors_credentials_require_explicit_allowlisted_origins() -> None:
    from django.conf import settings

    assert settings.CORS_ALLOW_CREDENTIALS is bool(settings.CORS_ALLOWED_ORIGINS)
    assert "*" not in settings.CORS_ALLOWED_ORIGINS


@override_settings(**_settings())
def test_first_login_recovers_from_username_integrity_conflict() -> None:
    get_user_model().objects.create_user(username="strava-123")
    player = save_connection(_payload())
    assert player.strava_athlete_id == 123
    assert Player.objects.count() == 1


@override_settings(**_settings())
def test_refresh_rotation_uses_the_latest_locked_credential() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    PlayerCredential.objects.create(
        player=player,
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at="2033-05-18T03:33:20Z",
    )
    first = Mock()
    first.raise_for_status.return_value = None
    first.json.return_value = {
        "access_token": "new-access",
        "refresh_token": "new-refresh",
        "expires_at": 2_000_000_000,
    }
    second = Mock()
    second.raise_for_status.return_value = None
    second.json.return_value = {
        "access_token": "latest-access",
        "refresh_token": "latest-refresh",
        "expires_at": 2_000_000_000,
    }
    with patch("apps.accounts.services.httpx.post", side_effect=[first, second]) as refresh:
        from apps.accounts.services import refresh_connection

        assert refresh_connection(player) == "success"
        assert refresh_connection(player) == "success"
    assert refresh.call_args_list[1].kwargs["data"]["refresh_token"] == "new-refresh"


@override_settings(**_settings())
def test_account_deletion_invalidates_oauth_states_in_other_sessions() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    first_client = Client()
    first_session = first_client.session
    first_session["player_id"] = player.pk
    first_session["player_session_epoch"] = player.session_epoch
    first_session.save()
    second_client = Client()
    second_session = second_client.session
    second_session["player_id"] = player.pk
    second_session["player_session_epoch"] = player.session_epoch
    second_session.save()
    authorize = first_client.get(reverse("game-strava-authorize"))
    state = str(authorize["Location"]).split("state=", 1)[1]
    with patch("apps.accounts.services.httpx.post"):
        assert second_client.delete(reverse("game-player-account")).status_code == 204
    assert not OAuthState.objects.filter(state_digest=OAuthState.digest(state)).exists()
    callback = first_client.get(reverse("game-strava-callback"), {"state": state, "code": "stale"})
    assert callback["Location"].endswith("/game?game_auth=error")


@override_settings(**_settings())
def test_game_routes_expose_only_the_exact_contract() -> None:
    client = Client()
    assert client.get("/api/v1/game/account/profile/").status_code == 404
    assert client.get(reverse("game-player-account")).status_code == 401
    client.get(reverse("game-player-session"))
    csrf_token = client.cookies["csrftoken"].value
    assert (
        client.post(reverse("game-player-account"), HTTP_X_CSRFTOKEN=csrf_token).status_code == 405
    )
    assert client.get(reverse("game-player-disconnect")).status_code == 405


@override_settings(**_settings())
def test_revocation_retry_task_removes_completed_job() -> None:
    job = RevocationJob.objects.create(
        access_token="access-secret",
        expires_at=timezone.now() + timedelta(days=1),
    )
    with patch("apps.accounts.services.httpx.post") as revoke:
        assert retry_revocations(limit=1) == {"processed": 1}
    revoke.assert_called_once()
    assert not RevocationJob.objects.filter(pk=job.pk).exists()


@override_settings(**_settings())
def test_player_mutations_require_csrf_token() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    client = Client(enforce_csrf_checks=True)
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()

    session_response = client.get(reverse("game-player-session"))
    assert session_response.status_code == 200
    token = client.cookies["csrftoken"].value
    assert client.post(reverse("game-player-logout")).status_code == 403
    assert client.post(reverse("game-player-logout"), HTTP_X_CSRFTOKEN=token).status_code == 204


@override_settings(**_settings())
def test_every_player_endpoint_is_disabled_by_default_even_with_existing_session() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    with override_settings(GAME_ENABLED=False):
        assert client.get(reverse("game-player-session")).status_code == 404
        assert client.post(reverse("game-player-logout")).status_code == 404
        assert client.post(reverse("game-player-refresh")).status_code == 404
        assert client.post(reverse("game-player-disconnect")).status_code == 404
        assert client.get(reverse("game-player-account")).status_code == 404
        assert (
            client.patch(
                reverse("game-player-account"),
                {"nickname": "Ada"},
                content_type="application/json",
            ).status_code
            == 404
        )
        assert client.delete(reverse("game-player-account")).status_code == 404


@override_settings(**_settings())
def test_revoked_refresh_invalidates_player_and_removes_credentials() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    PlayerCredential.objects.create(
        player=player,
        access_token="access-secret",
        refresh_token="refresh-secret",
        expires_at="2033-05-18T03:33:20Z",
    )
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    response = Mock(status_code=400)
    response.json.return_value = {"error": "invalid_grant"}
    response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "revoked", request=Mock(), response=response
    )
    with patch("apps.accounts.services.httpx.post", return_value=response):
        refreshed = client.post(reverse("game-player-refresh"))
    assert refreshed.status_code == 401
    player.refresh_from_db()
    assert player.lifecycle == Player.Lifecycle.PENDING_DELETION
    assert not PlayerCredential.objects.filter(player=player).exists()


@override_settings(**_settings())
def test_disconnect_invalidates_player_bound_oauth_states() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    authorize = client.get(reverse("game-strava-authorize"))
    state = str(authorize["Location"]).split("state=", 1)[1]
    assert OAuthState.objects.filter(player=player).exists()
    with patch("apps.accounts.services.httpx.post"):
        assert client.post(reverse("game-player-disconnect")).status_code == 200
    assert not OAuthState.objects.filter(state_digest=OAuthState.digest(state)).exists()


@override_settings(**_settings())
def test_expired_pending_player_is_purged_and_records_bounded_tombstone() -> None:
    user = get_user_model().objects.create_user(username="strava-123")
    player = Player.objects.create(user=user, strava_athlete_id=123)
    player.mark_disconnected()
    Player.objects.filter(pk=player.pk).update(deletion_deadline=timezone.now())
    assert purge_expired_players(limit=10) == {"purged": 1}
    assert not Player.objects.exists()
    tombstone = PlayerDeletionTombstone.objects.get()
    assert tombstone.status == PlayerDeletionTombstone.Status.COMPLETED
    assert tombstone.expires_at > timezone.now()
    assert not hasattr(tombstone, "strava_athlete_id")
    assert not RevocationJob.objects.exists()
