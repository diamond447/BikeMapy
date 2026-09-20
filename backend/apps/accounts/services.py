"""Strava OAuth, credential, and private player-session services."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from .models import Player, PlayerCredential

STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_DEAUTHORIZE_URL = "https://www.strava.com/oauth/deauthorize"


class StravaOAuthError(RuntimeError):
    """Safe, non-secret provider error."""


def game_is_available() -> bool:
    return bool(
        getattr(settings, "GAME_ENABLED", False)
        and getattr(settings, "STRAVA_OAUTH_CLIENT_ID", "")
        and getattr(settings, "STRAVA_OAUTH_CLIENT_SECRET", "")
        and getattr(settings, "STRAVA_TOKEN_ENCRYPTION_KEY", "")
    )


def redirect_uri() -> str:
    return getattr(
        settings,
        "STRAVA_OAUTH_REDIRECT_URI",
        "http://localhost:8000/api/v1/game/auth/strava/callback/",
    )


def authorization_url(*, state: str) -> str:
    params = {
        "client_id": settings.STRAVA_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "read,activity:read",
        "state": state,
    }
    return f"{STRAVA_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict[str, Any]:
    try:
        response = httpx.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": settings.STRAVA_OAUTH_CLIENT_ID,
                "client_secret": settings.STRAVA_OAUTH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise StravaOAuthError("Strava authorization could not be completed") from exc
    if (
        not isinstance(payload, dict)
        or not payload.get("access_token")
        or not payload.get("refresh_token")
    ):
        raise StravaOAuthError("Strava authorization returned an incomplete response")
    athlete = payload.get("athlete")
    if not isinstance(athlete, dict) or not athlete.get("id"):
        raise StravaOAuthError("Strava authorization did not return an athlete identity")
    return payload


def _expires_at(payload: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(payload["expires_at"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise StravaOAuthError("Strava authorization returned an invalid expiry") from exc


def save_connection(payload: dict[str, Any]) -> Player:
    athlete = payload["athlete"]
    athlete_id = int(athlete["id"])
    with transaction.atomic():
        player = Player.objects.select_for_update().filter(strava_athlete_id=athlete_id).first()
        if player is None:
            user = get_user_model().objects.create_user(
                username=f"strava-{athlete_id}",
                password=None,
            )
            user.set_unusable_password()
            user.save(update_fields=("password",))
            player = Player.objects.create(
                user=user,
                strava_athlete_id=athlete_id,
                strava_display_name=_display_name(athlete),
                strava_profile_image_url=str(athlete.get("profile") or ""),
            )
        elif player.lifecycle == Player.Lifecycle.DELETED:
            raise StravaOAuthError("This Strava identity is no longer available")
        else:
            player.strava_display_name = _display_name(athlete)
            player.strava_profile_image_url = str(athlete.get("profile") or "")
            player.save(
                update_fields=("strava_display_name", "strava_profile_image_url", "updated_at")
            )
        PlayerCredential.objects.update_or_create(
            player=player,
            defaults={
                "access_token": str(payload["access_token"]),
                "refresh_token": str(payload["refresh_token"]),
                "expires_at": _expires_at(payload),
                "scopes": (
                    payload.get("scope", []) if isinstance(payload.get("scope", []), list) else []
                ),
            },
        )
        player.restore_connection()
    return player


def _display_name(athlete: dict[str, Any]) -> str:
    value = " ".join(str(athlete.get(key, "")).strip() for key in ("firstname", "lastname")).strip()
    return value[:240]


def refresh_connection(player: Player) -> bool:
    try:
        credential = player.credential
        response = httpx.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": settings.STRAVA_OAUTH_CLIENT_ID,
                "client_secret": settings.STRAVA_OAUTH_CLIENT_SECRET,
                "refresh_token": credential.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or not payload.get("access_token")
            or not payload.get("refresh_token")
        ):
            raise StravaOAuthError("Strava refresh returned an incomplete response")
        PlayerCredential.objects.update_or_create(
            player=player,
            defaults={
                "access_token": str(payload["access_token"]),
                "refresh_token": str(payload["refresh_token"]),
                "expires_at": _expires_at(payload),
                "scopes": payload.get("scope", credential.scopes),
            },
        )
        return True
    except (PlayerCredential.DoesNotExist, httpx.HTTPError, ValueError, StravaOAuthError):
        disconnect_player(player, revoke=False)
        return False


def _revoke_access_token(access_token: str | None) -> None:
    if not access_token:
        return
    try:
        httpx.post(STRAVA_DEAUTHORIZE_URL, params={"access_token": access_token}, timeout=10)
    except httpx.HTTPError:
        pass


def disconnect_player(player: Player, *, revoke: bool = True) -> Player:
    access_token = None
    try:
        access_token = player.credential.access_token
    except PlayerCredential.DoesNotExist:
        pass
    if revoke:
        _revoke_access_token(access_token)
    with transaction.atomic():
        PlayerCredential.objects.filter(player=player).delete()
        player = Player.objects.select_for_update().get(pk=player.pk)
        player.mark_disconnected()
    return player


def delete_player(player: Player) -> None:
    access_token = None
    try:
        access_token = player.credential.access_token
    except PlayerCredential.DoesNotExist:
        pass
    _revoke_access_token(access_token)
    with transaction.atomic():
        PlayerCredential.objects.filter(player=player).delete()
        player.lifecycle = Player.Lifecycle.DELETED
        player.invalidate_sessions()
        player.user.delete()
