from __future__ import annotations

from unittest.mock import Mock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.models import OAuthState, Player, PlayerCredential

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
    state = str(response["Location"]).split("state=", 1)[1]
    assert OAuthState.objects.count() == 1

    token_response = Mock()
    token_response.json.return_value = _payload()
    token_response.raise_for_status.return_value = None
    with patch("apps.accounts.services.httpx.post", return_value=token_response):
        callback = client.get(reverse("game-strava-callback"), {"state": state, "code": "one-time"})
    assert callback.status_code == 200
    assert callback.json()["player"]["athlete_id"] == "123"
    assert Player.objects.get().strava_athlete_id == 123
    assert PlayerCredential.objects.get().access_token == "access-secret"

    replay = client.get(reverse("game-strava-callback"), {"state": state, "code": "one-time"})
    assert replay.status_code == 400
    assert Player.objects.count() == 1


@override_settings(**_settings())
def test_denied_callback_consumes_state_without_creating_player() -> None:
    client = Client()
    authorize = client.get(reverse("game-strava-authorize"))
    state = str(authorize["Location"]).split("state=", 1)[1]
    response = client.get(
        reverse("game-strava-callback"), {"state": state, "error": "access_denied"}
    )
    assert response.status_code == 400
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
    assert not PlayerCredential.objects.filter(player=player).exists()


@override_settings(**_settings())
def test_refresh_failure_invalidates_session_and_enters_reconnection_window() -> None:
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
    assert refreshed.status_code == 401
    player.refresh_from_db()
    assert player.lifecycle == Player.Lifecycle.PENDING_DELETION
    assert player.deletion_deadline is not None
    assert not PlayerCredential.objects.filter(player=player).exists()
    assert client.get(reverse("game-player-account")).status_code == 401


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
            == 200
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
    assert callback.status_code == 200
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
    assert callback.status_code == 400
    assert not Player.objects.exists()
