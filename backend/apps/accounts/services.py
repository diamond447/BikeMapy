"""Strava OAuth, credential, and private player-session services."""

from __future__ import annotations

import hashlib
import hmac
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

from .models import (
    OAUTH_STATE_TTL,
    OAuthState,
    Player,
    PlayerCredential,
    PlayerDeletionTombstone,
    PlayerIdentityGuard,
    RevocationJob,
)

STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_DEAUTHORIZE_URL = "https://www.strava.com/oauth/deauthorize"


class StravaOAuthError(RuntimeError):
    """Safe, non-secret provider error."""


RefreshOutcome = Literal["success", "retryable", "revoked"]
REVOCATION_RETRY_LIMIT = 8
REQUIRED_STRAVA_SCOPES = frozenset({"read", "activity:read"})
IDENTITY_GUARD_RETENTION = OAUTH_STATE_TTL + timedelta(minutes=5)


def game_is_available() -> bool:
    return bool(
        getattr(settings, "GAME_ENABLED", False)
        and getattr(settings, "STRAVA_OAUTH_CLIENT_ID", "")
        and getattr(settings, "STRAVA_OAUTH_CLIENT_SECRET", "")
        and getattr(settings, "STRAVA_TOKEN_ENCRYPTION_KEY", "")
        and getattr(settings, "STRAVA_IDENTITY_GUARD_KEY", "")
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


def granted_scopes(payload: dict[str, Any]) -> frozenset[str]:
    value = payload.get("scope")
    if isinstance(value, str):
        values = value.replace(" ", ",").split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return frozenset(str(scope).strip() for scope in values if str(scope).strip())


def validate_granted_scopes(payload: dict[str, Any]) -> None:
    if not REQUIRED_STRAVA_SCOPES.issubset(granted_scopes(payload)):
        raise StravaOAuthError("Strava authorization did not grant the required scopes")


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


def _identity_digest(athlete_id: int) -> str:
    """Return a stable, non-content key for synchronizing one athlete."""

    return hmac.new(
        str(settings.STRAVA_IDENTITY_GUARD_KEY).encode(),
        b"bikemapy:strava:identity-guard:v1:" + str(athlete_id).encode(),
        hashlib.sha256,
    ).hexdigest()


def _locked_identity_guard(athlete_id: int) -> PlayerIdentityGuard:
    """Get the per-athlete lock row, including for its first connection."""

    digest = _identity_digest(athlete_id)
    try:
        guard = PlayerIdentityGuard.objects.select_for_update().get(identity_digest=digest)
    except PlayerIdentityGuard.DoesNotExist:
        try:
            with transaction.atomic():
                guard = PlayerIdentityGuard.objects.create(identity_digest=digest)
        except IntegrityError:
            guard = PlayerIdentityGuard.objects.select_for_update().get(identity_digest=digest)
    return guard


def save_connection(payload: dict[str, Any], *, oauth_state_id: int | None = None) -> Player:
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
    validate_granted_scopes(payload)
    with transaction.atomic():
        guard = _locked_identity_guard(athlete_id)
        oauth_state = None
        if oauth_state_id is not None:
            oauth_state = OAuthState.objects.filter(
                pk=oauth_state_id,
                used_at__isnull=False,
            ).first()
            if oauth_state is None:
                raise StravaOAuthError("OAuth state is no longer valid")
            if guard.invalidated_at is not None and oauth_state.created_at <= guard.invalidated_at:
                raise StravaOAuthError("OAuth state is no longer valid")
        player = Player.objects.select_for_update().filter(strava_athlete_id=athlete_id).first()
        if player is None:
            if oauth_state is not None and oauth_state.player_id is not None:
                raise StravaOAuthError("OAuth state is no longer valid")
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
            if (
                oauth_state is not None
                and oauth_state.player_id is not None
                and (
                    oauth_state.player_id != player.pk
                    or oauth_state.player_session_epoch != player.session_epoch
                    or player.lifecycle != Player.Lifecycle.CONNECTED
                )
            ):
                raise StravaOAuthError("OAuth state is no longer valid")
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
                "scopes": sorted(granted_scopes(payload)),
            },
        )
        player.restore_connection()
        if oauth_state is not None:
            oauth_state.player = player
            oauth_state.player_session_epoch = player.session_epoch
            oauth_state.save(update_fields=("player", "player_session_epoch"))
    return player


def _display_name(athlete: dict[str, Any]) -> str:
    value = " ".join(
        str(athlete.get(key) or "").strip() for key in ("firstname", "lastname")
    ).strip()
    return value[:240]


def _refresh_is_revoked(response: httpx.Response) -> bool:
    if response.status_code in (401, 403):
        return True
    if response.status_code != 400:
        return False
    try:
        payload = response.json()
    except ValueError:
        return response.status_code in (401, 403)
    if not isinstance(payload, dict):
        return False
    error = str(payload.get("error") or payload.get("error_type") or "").lower()
    if error in {"invalid_grant", "revoked", "authorization_revoked"}:
        return True
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return False
    for item in errors:
        if not isinstance(item, dict):
            continue
        resource = str(item.get("resource") or "").lower()
        field = str(item.get("field") or "").lower()
        code = str(item.get("code") or "").lower()
        if (
            resource in {"refreshtoken", "refresh_token"}
            and field == "refresh_token"
            and code in {"invalid", "revoked", "invalid_grant"}
        ):
            return True
    return False


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


def revoke_or_schedule(access_token: str | None) -> None:
    if not access_token:
        return
    job = RevocationJob.objects.create(
        access_token=access_token,
        expires_at=timezone.now() + timedelta(days=7),
    )
    _record_revocation_result(job, success=_revoke_access_token(access_token))


def disconnect_player(
    player: Player,
    *,
    revoke: bool = True,
    session_key: str | None = None,
) -> Player:
    access_token = None
    with transaction.atomic():
        # Lock the identity before the player so callbacks and lifecycle changes
        # share one portable serialization boundary.
        guard = _locked_identity_guard(player.strava_athlete_id)
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
        guard.invalidated_at = timezone.now()
        guard.save(update_fields=("invalidated_at", "updated_at"))
        state_filter = Q(player=player)
        if session_key:
            state_filter |= Q(player__isnull=True, session_key=session_key)
        OAuthState.objects.filter(state_filter).delete()
        player.mark_disconnected()
    if job is not None:
        _record_revocation_result(job, success=_revoke_access_token(access_token))
    return player


def delete_player(player: Player, *, session_key: str | None = None) -> None:
    access_token = None
    with transaction.atomic():
        guard = _locked_identity_guard(player.strava_athlete_id)
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
        guard.invalidated_at = timezone.now()
        guard.save(update_fields=("invalidated_at", "updated_at"))
        state_filter = Q(player=player)
        if session_key:
            state_filter |= Q(player__isnull=True, session_key=session_key)
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


def cleanup_identity_guards(*, limit: int = 100) -> dict[str, int]:
    """Remove invalidation cutoffs after all related OAuth states have expired."""

    cutoff = timezone.now() - IDENTITY_GUARD_RETENTION
    guard_ids = list(
        PlayerIdentityGuard.objects.filter(
            invalidated_at__isnull=False,
            invalidated_at__lt=cutoff,
        )
        .order_by("invalidated_at", "pk")
        .values_list("pk", flat=True)[: max(1, limit)]
    )
    deleted, _ = PlayerIdentityGuard.objects.filter(pk__in=guard_ids).delete()
    return {"deleted": deleted}


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
