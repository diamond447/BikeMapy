"""Strava OAuth, credential, and private player-session services."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import OAuthState, Player, PlayerCredential, PlayerDeletionTombstone, RevocationJob

STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_DEAUTHORIZE_URL = "https://www.strava.com/oauth/deauthorize"


class StravaOAuthError(RuntimeError):
    """Safe, non-secret provider error."""


RefreshOutcome = Literal["success", "retryable", "revoked"]
REVOCATION_RETRY_LIMIT = 8


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


def game_return_url(result: Literal["success", "denied", "error"]) -> str:
    """Return only the configured game URL with a fixed OAuth result value."""

    target = str(getattr(settings, "GAME_FRONTEND_URL", "http://localhost:5173/game"))
    parsed = urlsplit(target)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/game"
    ):
        raise StravaOAuthError("Configured game return URL is invalid")
    return (
        urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        + "?"
        + urlencode({"game_auth": result})
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
    if not isinstance(athlete, dict) or _athlete_id(athlete.get("id")) is None:
        raise StravaOAuthError("Strava authorization did not return an athlete identity")
    return payload


def _expires_at(payload: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(payload["expires_at"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise StravaOAuthError("Strava authorization returned an invalid expiry") from exc


def _athlete_id(value: Any) -> int | None:
    try:
        athlete_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return athlete_id if athlete_id > 0 else None


def save_connection(payload: dict[str, Any]) -> Player:
    athlete = payload.get("athlete")
    if not isinstance(athlete, dict):
        raise StravaOAuthError("Strava authorization did not return an athlete identity")
    athlete_id = _athlete_id(athlete.get("id"))
    if athlete_id is None:
        raise StravaOAuthError("Strava authorization returned an invalid athlete identity")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    if not access_token or not refresh_token:
        raise StravaOAuthError("Strava authorization returned incomplete credentials")
    with transaction.atomic():
        player = Player.objects.select_for_update().filter(strava_athlete_id=athlete_id).first()
        if player is None:
            for attempt in range(3):
                username = (
                    f"strava-{athlete_id}"
                    if attempt == 0
                    else f"strava-{athlete_id}-{secrets.token_hex(8)}"
                )
                try:
                    with transaction.atomic():
                        user = get_user_model().objects.create_user(
                            username=username,
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
                    break
                except IntegrityError:
                    player = (
                        Player.objects.select_for_update()
                        .filter(strava_athlete_id=athlete_id)
                        .first()
                    )
                    if player is not None:
                        break
            else:
                raise StravaOAuthError("Strava player account could not be created")
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
                "access_token": str(access_token),
                "refresh_token": str(refresh_token),
                "expires_at": _expires_at(payload),
                "scopes": (
                    payload.get("scope", []) if isinstance(payload.get("scope", []), list) else []
                ),
            },
        )
        player.restore_connection()
    return player


def _display_name(athlete: dict[str, Any]) -> str:
    value = " ".join(
        str(athlete.get(key) or "").strip() for key in ("firstname", "lastname")
    ).strip()
    return value[:240]


def _refresh_is_revoked(response: httpx.Response) -> bool:
    if response.status_code not in (400, 401, 403):
        return False
    try:
        payload = response.json()
    except ValueError:
        return response.status_code in (401, 403)
    if not isinstance(payload, dict):
        return response.status_code in (401, 403)
    error = str(payload.get("error") or payload.get("error_type") or "").lower()
    return error in {"invalid_grant", "revoked", "authorization_revoked"}


def refresh_connection(player: Player) -> RefreshOutcome:
    try:
        with transaction.atomic():
            locked_player = Player.objects.select_for_update().get(pk=player.pk)
            credential = PlayerCredential.objects.select_for_update().get(player=locked_player)
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
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if _refresh_is_revoked(exc.response):
                    transaction.set_rollback(True)
                else:
                    return "retryable"
            except httpx.HTTPError:
                return "retryable"
            else:
                try:
                    payload = response.json()
                except ValueError:
                    return "retryable"
                if (
                    not isinstance(payload, dict)
                    or not payload.get("access_token")
                    or not payload.get("refresh_token")
                ):
                    return "retryable"
                credential.access_token = str(payload["access_token"])
                credential.refresh_token = str(payload["refresh_token"])
                credential.expires_at = _expires_at(payload)
                scopes = payload.get("scope")
                credential.scopes = scopes if isinstance(scopes, list) else credential.scopes
                credential.save()
                return "success"
    except PlayerCredential.DoesNotExist:
        disconnect_player(player, revoke=False)
        return "revoked"
    except ValidationError:
        disconnect_player(player, revoke=False)
        return "revoked"
    except (httpx.HTTPError, StravaOAuthError, ValueError):
        return "retryable"
    disconnect_player(player, revoke=False)
    return "revoked"


def _revoke_access_token(access_token: str | None) -> bool:
    if not access_token:
        return True
    try:
        response = httpx.post(
            STRAVA_DEAUTHORIZE_URL,
            data={"access_token": access_token},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def _record_revocation_result(job: RevocationJob, *, success: bool) -> None:
    if success:
        job.delete()
        return
    now = timezone.now()
    job.attempts += 1
    job.status = (
        RevocationJob.Status.FAILED
        if job.attempts >= REVOCATION_RETRY_LIMIT
        else RevocationJob.Status.PENDING
    )
    job.last_error = "provider unavailable"
    job.next_attempt_at = now + timedelta(minutes=min(60, 2 ** min(job.attempts, 6)))
    job.save(update_fields=("attempts", "status", "last_error", "next_attempt_at"))


def disconnect_player(
    player: Player,
    *,
    revoke: bool = True,
    session_key: str | None = None,
) -> Player:
    access_token = None
    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player.pk)
        try:
            access_token = (
                PlayerCredential.objects.select_for_update().get(player=player).access_token
            )
        except PlayerCredential.DoesNotExist:
            pass
        job = None
        if revoke and access_token:
            job = RevocationJob.objects.create(
                access_token=access_token,
                expires_at=timezone.now() + timedelta(days=7),
            )
        PlayerCredential.objects.filter(player=player).delete()
        state_filter = Q(player=player)
        if session_key:
            state_filter |= Q(session_key=session_key)
        OAuthState.objects.filter(state_filter).delete()
        player.mark_disconnected()
    if job is not None:
        _record_revocation_result(job, success=_revoke_access_token(access_token))
    return player


def delete_player(player: Player, *, session_key: str | None = None) -> None:
    access_token = None
    with transaction.atomic():
        player = Player.objects.select_for_update().get(pk=player.pk)
        try:
            access_token = (
                PlayerCredential.objects.select_for_update().get(player=player).access_token
            )
        except PlayerCredential.DoesNotExist:
            pass
        job = None
        if access_token:
            job = RevocationJob.objects.create(
                access_token=access_token,
                expires_at=timezone.now() + timedelta(days=7),
            )
        PlayerCredential.objects.filter(player=player).delete()
        state_filter = Q(player=player)
        if session_key:
            state_filter |= Q(session_key=session_key)
        OAuthState.objects.filter(state_filter).delete()
        PlayerDeletionTombstone.objects.create(expires_at=timezone.now() + timedelta(days=90))
        player.lifecycle = Player.Lifecycle.DELETED
        player.invalidate_sessions()
        player.user.delete()
    if job is not None:
        _record_revocation_result(job, success=_revoke_access_token(access_token))


def purge_expired_players(*, limit: int = 100) -> dict[str, int]:
    purged = 0
    for candidate in Player.objects.filter(
        lifecycle=Player.Lifecycle.PENDING_DELETION,
        deletion_deadline__lte=timezone.now(),
    ).order_by("pk")[: max(1, limit)]:
        try:
            delete_player(candidate)
        except Player.DoesNotExist:
            continue
        purged += 1
    PlayerDeletionTombstone.objects.filter(expires_at__lte=timezone.now()).delete()
    return {"purged": purged}


def retry_revocations(*, limit: int = 100) -> dict[str, int]:
    processed = 0
    for job in RevocationJob.objects.filter(
        status=RevocationJob.Status.PENDING,
        next_attempt_at__lte=timezone.now(),
        expires_at__gt=timezone.now(),
    ).order_by("pk")[: max(1, limit)]:
        processed += 1
        _record_revocation_result(job, success=_revoke_access_token(job.access_token))
    RevocationJob.objects.filter(expires_at__lte=timezone.now()).delete()
    return {"processed": processed}
