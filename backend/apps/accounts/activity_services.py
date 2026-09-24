"""Privacy-aware Strava activity synchronization and webhook services."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .models import (
    Competition,
    ImportedActivity,
    Player,
    PlayerCredential,
    StravaSyncJob,
    StravaSyncState,
    StravaWebhookEvent,
)

STRAVA_API_URL = "https://www.strava.com/api/v3"
ELIGIBLE_ACTIVITY_TYPES = frozenset({"Ride", "MountainBikeRide", "GravelRide", "EBikeRide"})
SYNC_RETRY_SECONDS = (30, 120, 600, 1800, 3600)
MAX_POLYLINE_LENGTH = 250_000
MAX_POLYLINE_POINTS = 100_000
WEBHOOK_UPDATE_FIELDS = frozenset({"title", "type", "private"})
MAX_RETRY_AFTER_SECONDS = 86_400


class StravaActivityError(RuntimeError):
    """Safe provider error with retry metadata and no credential content."""

    def __init__(self, message: str, *, retryable: bool = True, retry_after: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _retry_after_cap() -> int:
    try:
        configured = float(getattr(settings, "STRAVA_SYNC_MAX_RETRY_AFTER", 3600))
    except (TypeError, ValueError, OverflowError):
        configured = 3600
    if not math.isfinite(configured) or configured <= 0:
        configured = 3600
    return min(MAX_RETRY_AFTER_SECONDS, max(1, math.ceil(configured)))


def _bounded_retry_seconds(value: Any) -> int | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(_retry_after_cap(), math.ceil(seconds))


def _retry_after_seconds(response: httpx.Response) -> int | None:
    """Parse provider retry hints without allowing unbounded retry schedules."""

    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            numeric = float(retry_after.strip())
        except (TypeError, ValueError, OverflowError):
            numeric = math.nan
        if math.isfinite(numeric) and numeric >= 0:
            return _bounded_retry_seconds(numeric)
        try:
            retry_at = parsedate_to_datetime(retry_after)
        except (TypeError, ValueError, OverflowError):
            retry_at = None
        if retry_at is not None:
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = (retry_at - timezone.now()).total_seconds()
            bounded = _bounded_retry_seconds(delay)
            if bounded is not None:
                return bounded

    quota_reset = response.headers.get("X-RateLimit-Reset")
    if quota_reset:
        try:
            reset_at = float(quota_reset.strip())
        except (TypeError, ValueError, OverflowError):
            reset_at = math.nan
        if math.isfinite(reset_at) and reset_at >= 0:
            delay = reset_at - timezone.now().timestamp()
            bounded = _bounded_retry_seconds(delay)
            if bounded is not None:
                return bounded
    return None


def _iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _decode_polyline(encoded: str) -> list[list[float]]:
    """Decode Strava's encoded polyline without filling omitted privacy points."""

    if not encoded or len(encoded) > MAX_POLYLINE_LENGTH:
        raise ValueError("Polyline is empty or too large.")
    coordinates: list[list[float]] = []
    index = lat = lon = 0

    def read_value() -> int:
        nonlocal index
        result = shift = 0
        while index < len(encoded):
            byte = ord(encoded[index]) - 63
            index += 1
            if byte < 0 or byte > 63:
                raise ValueError("Polyline contains an invalid character.")
            result |= (byte & 0x1F) << shift
            if byte < 0x20:
                value = ~(result >> 1) if result & 1 else result >> 1
                return value
            shift += 5
            if shift > 30:
                raise ValueError("Polyline value is too large.")
        raise ValueError("Polyline is truncated.")

    while index < len(encoded):
        lat += read_value()
        lon += read_value()
        if len(coordinates) >= MAX_POLYLINE_POINTS:
            raise ValueError("Polyline contains too many points.")
        coordinates.append([lon / 1e5, lat / 1e5])
    return coordinates


def _activity_geometry(payload: dict[str, Any]) -> dict[str, Any] | None:
    map_data = payload.get("map")
    if not isinstance(map_data, dict):
        return None
    encoded = map_data.get("polyline") or map_data.get("summary_polyline")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        coordinates = _decode_polyline(encoded)
    except ValueError:
        return None
    if len(coordinates) < 2 or any(
        not math.isfinite(point[0])
        or not math.isfinite(point[1])
        or not -180 <= point[0] <= 180
        or not -90 <= point[1] <= 90
        for point in coordinates
    ):
        return None
    if all(point == coordinates[0] for point in coordinates[1:]):
        return None
    return {"type": "LineString", "coordinates": coordinates}


def _geometry_for_database(value: dict[str, Any]) -> Any:
    if connection.vendor != "postgresql":
        return value
    from django.contrib.gis.geos import GEOSGeometry

    return GEOSGeometry(json.dumps(value), srid=4326)


def geometry_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "geojson"):
        try:
            parsed = json.loads(str(value.geojson))
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


def activity_is_eligible(payload: dict[str, Any]) -> bool:
    activity_type = str(payload.get("sport_type") or payload.get("type") or "")
    if activity_type not in ELIGIBLE_ACTIVITY_TYPES:
        return False
    if bool(payload.get("virtual") or payload.get("trainer")):
        return False
    visibility = str(payload.get("visibility") or "").strip().lower()
    if bool(payload.get("private")) or visibility in {"only_you", "only_me", "private"}:
        return False
    return _activity_geometry(payload) is not None


def _activity_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("id")
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return str(parsed) if parsed > 0 else None


def _athlete_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def activity_belongs_to_player(
    player: Player, payload: dict[str, Any], *, require_athlete: bool = False
) -> bool:
    athlete = payload.get("athlete")
    value = athlete.get("id") if isinstance(athlete, dict) else payload.get("athlete_id")
    if value is None:
        return not require_athlete
    return _athlete_id(value) == player.strava_athlete_id


def _provider_updated(payload: dict[str, Any]) -> datetime | None:
    return _iso_datetime(payload.get("updated_at")) or _iso_datetime(payload.get("start_date"))


def _calendar_date(payload: dict[str, Any], started_at: datetime) -> date:
    local_start = _iso_datetime(payload.get("start_date_local"))
    return (local_start or timezone.localtime(started_at)).date()


def _event_datetime(payload: dict[str, Any]) -> datetime | None:
    value = payload.get("event_time")
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


WEBHOOK_ASPECT_TYPES = frozenset({"create", "update", "delete"})
WEBHOOK_FIELDS = frozenset(
    {"subscription_id", "object_id", "aspect_type", "object_type", "owner_id", "event_time"}
)


def validate_webhook_payload(payload: Any) -> dict[str, Any] | None:
    """Validate the exact event shape documented by Strava's webhooks API."""

    if not isinstance(payload, dict) or not set(payload).issubset(WEBHOOK_FIELDS | {"updates"}):
        return None
    if not WEBHOOK_FIELDS.issubset(payload):
        return None
    if payload.get("object_type") != "activity":
        return None
    if payload.get("aspect_type") not in WEBHOOK_ASPECT_TYPES:
        return None
    updates = payload.get("updates")
    if "updates" in payload:
        if payload["aspect_type"] != "update" or not isinstance(updates, dict):
            return None
        if not set(updates).issubset(WEBHOOK_UPDATE_FIELDS):
            return None
        for key, value in updates.items():
            if key == "private":
                if not isinstance(value, str) or value not in {"true", "false"}:
                    return None
            elif not isinstance(value, str):
                return None
    values: dict[str, int] = {}
    for field in ("subscription_id", "object_id", "owner_id", "event_time"):
        value = payload.get(field)
        if isinstance(value, bool):
            return None
        try:
            parsed = int(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed <= 0:
            return None
        values[field] = parsed
    try:
        configured = int(getattr(settings, "STRAVA_WEBHOOK_SUBSCRIPTION_ID", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        configured = 0
    if configured <= 0 or values["subscription_id"] != configured:
        return None
    normalized = {**payload, **values}
    if isinstance(updates, dict) and "private" in updates:
        normalized["updates"] = {**updates, "private": updates["private"] == "true"}
    return normalized


def _schedule_player_recomputations(player_id: int) -> None:
    from apps.reference_routes.completion_services import schedule_player_completions

    from .competition_services import schedule_recomputation

    competition_ids = list(
        Competition.objects.filter(memberships__player_id=player_id)
        .order_by("pk")
        .values_list("pk", flat=True)
        .distinct()
    )
    # PostgreSQL/PostGIS does not support FOR UPDATE on a DISTINCT query.
    # Lock each competition separately in stable order instead.
    for competition_id in competition_ids:
        competition = Competition.objects.select_for_update().get(pk=competition_id)
        schedule_recomputation(competition)
    player = Player.objects.get(pk=player_id)
    schedule_player_completions(player, reason="activity-change")


def remove_activity(player: Player, provider_activity_id: str, *, reason: str) -> bool:
    with transaction.atomic():
        activity = (
            ImportedActivity.objects.select_for_update()
            .filter(player=player, provider_activity_id=str(provider_activity_id))
            .first()
        )
        if activity is None:
            return False
        activity.delete()
        _schedule_player_recomputations(player.pk)
    return True


def import_activity(player: Player, payload: dict[str, Any]) -> str:
    """Create/update one eligible activity, or remove it after a privacy downgrade."""

    if not activity_belongs_to_player(player, payload):
        return "ignored"
    provider_id = _activity_id(payload)
    if provider_id is None:
        return "ignored"
    if not activity_is_eligible(payload):
        remove_activity(player, provider_id, reason="ineligible")
        return "removed"
    geometry = _activity_geometry(payload)
    started_at = _iso_datetime(payload.get("start_date"))
    if geometry is None or started_at is None:
        remove_activity(player, provider_id, reason="missing_geometry")
        return "removed"
    geometry_hash = hashlib.sha256(
        json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    provider_updated_at = _provider_updated(payload)
    defaults = {
        "title": "",  # Strava titles are intentionally never retained or exposed.
        "started_at": started_at,
        "calendar_date": _calendar_date(payload, started_at),
        "activity_type": str(payload.get("sport_type") or payload.get("type") or "")[:48],
        "visibility": str(payload.get("visibility") or "")[:32],
        "geometry": _geometry_for_database(geometry),
        "geometry_hash": geometry_hash,
        "provider_updated_at": provider_updated_at,
        "removed_at": None,
        "removal_reason": "",
        "payload": {},
    }
    with transaction.atomic():
        activity = (
            ImportedActivity.objects.select_for_update()
            .filter(player=player, provider_activity_id=provider_id)
            .first()
        )
        if (
            activity is not None
            and activity.provider_updated_at
            and (
                defaults["provider_updated_at"] is not None
                and activity.provider_updated_at > defaults["provider_updated_at"]
            )
        ):
            return "stale"
        is_new = activity is None
        if activity is None:
            try:
                # The unique constraint is the final serialization boundary:
                # two webhook deliveries can race before either sees a row.
                activity = ImportedActivity.objects.create(
                    player=player, provider_activity_id=provider_id, **defaults
                )
            except IntegrityError:
                activity = ImportedActivity.objects.select_for_update().get(
                    player=player, provider_activity_id=provider_id
                )
                is_new = False
        if not is_new:
            if provider_updated_at is None and activity.provider_updated_at is not None:
                defaults["provider_updated_at"] = activity.provider_updated_at
            changed = (
                activity.geometry_hash != geometry_hash
                or activity.calendar_date != defaults["calendar_date"]
            )
            for field, value in defaults.items():
                setattr(activity, field, value)
            activity.save(update_fields=tuple(defaults) + ("updated_at",))
        if is_new or changed:
            _schedule_player_recomputations(player.pk)
    return "imported" if is_new else "updated"


def _credential(player_id: int) -> str:
    try:
        credential = PlayerCredential.objects.get(player_id=player_id)
    except PlayerCredential.DoesNotExist as exc:
        raise StravaActivityError("Strava credentials are unavailable.", retryable=False) from exc
    if not credential.access_token:
        raise StravaActivityError("Strava credentials are unavailable.", retryable=False)
    return str(credential.access_token)


def _strava_get(
    player_id: int,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    _retried_after_refresh: bool = False,
) -> Any:
    try:
        response = httpx.get(
            f"{STRAVA_API_URL}{path}",
            headers={"Authorization": f"Bearer {_credential(player_id)}"},
            params=params or {},
            timeout=float(getattr(settings, "STRAVA_API_TIMEOUT", 10)),
        )
    except httpx.HTTPError as exc:
        raise StravaActivityError("Strava is temporarily unavailable.") from exc
    if response.status_code == 429:
        delay = _retry_after_seconds(response)
        raise StravaActivityError("Strava rate limit reached.", retry_after=delay)
    if response.status_code in {401, 403}:
        if not _retried_after_refresh:
            from .services import refresh_connection

            player = Player.objects.get(pk=player_id)
            if refresh_connection(player) == "success":
                return _strava_get(
                    player_id,
                    path,
                    params=params,
                    _retried_after_refresh=True,
                )
        raise StravaActivityError("Strava authorization is no longer valid.", retryable=False)
    try:
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise StravaActivityError("Strava returned an invalid response.") from exc
    if not isinstance(value, (dict, list)):
        raise StravaActivityError("Strava returned an invalid response.")
    return value


def fetch_activity(player_id: int, provider_activity_id: str) -> dict[str, Any]:
    value = _strava_get(player_id, f"/activities/{provider_activity_id}")
    if not isinstance(value, dict):
        raise StravaActivityError("Strava returned an invalid activity.")
    return value


def fetch_activity_page(
    player_id: int, *, page: int, after: datetime | None = None
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "page": page,
        "per_page": min(100, max(1, int(getattr(settings, "STRAVA_SYNC_PAGE_SIZE", 100)))),
    }
    if after is not None:
        params["after"] = int(after.timestamp())
    value = _strava_get(player_id, "/athlete/activities", params=params)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def queue_sync(player: Player, *, kind: str, full_history: bool = False) -> StravaSyncJob:
    now = timezone.now()
    history_start = None if full_history else now - timedelta(days=365)
    with transaction.atomic():
        state, _ = StravaSyncState.objects.select_for_update().get_or_create(
            player=player,
            defaults={"history_start": history_start},
        )
        suffix = "full" if full_history else str((history_start or now).date())
        key = f"{player.pk}:{kind}:{suffix}"
        job, _ = StravaSyncJob.objects.select_for_update().get_or_create(
            idempotency_key=key,
            defaults={"player": player, "kind": kind, "next_attempt_at": now},
        )
        # Do not reset a live worker when a duplicate OAuth callback or an
        # impatient settings click queues the same mode again. The worker
        # owns the cursor until its lease expires.
        if job.status == StravaSyncJob.Status.RUNNING and (
            job.lease_until is None or job.lease_until > now
        ):
            return job
        state.status = StravaSyncState.Status.QUEUED
        state.mode = kind
        state.history_start = history_start
        state.cursor_page = 1
        state.imported_count = 0
        state.rejected_count = 0
        state.processed_count = 0
        state.attempts = 0
        state.last_provider_updated_at = None
        state.last_error = ""
        state.next_attempt_at = now
        state.started_at = None
        state.completed_at = None
        state.save(
            update_fields=(
                "status",
                "mode",
                "history_start",
                "cursor_page",
                "imported_count",
                "rejected_count",
                "processed_count",
                "attempts",
                "last_provider_updated_at",
                "last_error",
                "next_attempt_at",
                "started_at",
                "completed_at",
                "updated_at",
            )
        )
        job.status = StravaSyncJob.Status.PENDING
        job.page = 1
        job.next_attempt_at = now
        job.last_error = ""
        job.dispatch_token = ""
        job.dispatch_lease_until = None
        job.save(
            update_fields=(
                "status",
                "page",
                "next_attempt_at",
                "last_error",
                "dispatch_token",
                "dispatch_lease_until",
                "updated_at",
            )
        )
    return job


def pause_sync(player: Player) -> None:
    StravaSyncState.objects.filter(player=player).update(
        status=StravaSyncState.Status.PAUSED,
        updated_at=timezone.now(),
    )
    StravaSyncJob.objects.filter(
        player=player,
        status__in=(
            StravaSyncJob.Status.PENDING,
            StravaSyncJob.Status.FAILED,
        ),
    ).update(
        status=StravaSyncJob.Status.COMPLETED,
        dispatch_token="",
        dispatch_lease_until=None,
        updated_at=timezone.now(),
    )


def queue_webhook_event(payload: dict[str, Any], event_key: str) -> StravaWebhookEvent:
    object_id = int(payload.get("object_id") or 0)
    owner_id = int(payload.get("owner_id") or 0)
    with transaction.atomic():
        event, created = StravaWebhookEvent.objects.get_or_create(
            event_key=event_key,
            defaults={
                "subscription_id": payload.get("subscription_id"),
                "object_id": object_id,
                "owner_athlete_id": owner_id,
                "aspect_type": str(payload.get("aspect_type") or "")[:24],
                "object_type": str(payload.get("object_type") or "activity")[:24],
                "payload": payload,
            },
        )
        if created:
            player = Player.objects.filter(
                strava_athlete_id=owner_id, lifecycle=Player.Lifecycle.CONNECTED
            ).first()
            if player is not None:
                StravaSyncJob.objects.create(
                    player=player,
                    kind=StravaSyncJob.Kind.WEBHOOK,
                    webhook_event=event,
                    idempotency_key=f"webhook:{event_key}",
                )
    return event


def webhook_event_key(payload: dict[str, Any], raw_body: bytes) -> str:
    del raw_body
    stable = "|".join(
        str(payload.get(key) or "")
        for key in ("subscription_id", "object_id", "owner_id", "aspect_type", "event_time")
    )
    return hashlib.sha256(stable.encode()).hexdigest()


def _lock_sync_state_then_job(
    job_id: int,
    *,
    state_id: int | None = None,
    player_id: int | None = None,
) -> tuple[StravaSyncState | None, StravaSyncJob]:
    """Lock the sync pair in the one order shared by every lifecycle path."""

    locked_state = None
    if state_id is not None:
        locked_state = StravaSyncState.objects.select_for_update().get(pk=state_id)
    elif player_id is not None:
        locked_state = (
            StravaSyncState.objects.select_for_update().filter(player_id=player_id).first()
        )
    locked_job = (
        StravaSyncJob.objects.select_for_update()
        .select_related("player")
        .select_for_update(of=("self",))
        .get(pk=job_id)
    )
    return locked_state, locked_job


def _finish_job(
    job: StravaSyncJob,
    *,
    state: StravaSyncState | None,
    more: bool = False,
    lease_token: str = "",
) -> str:
    now = timezone.now()
    with transaction.atomic():
        locked_state = (
            StravaSyncState.objects.select_for_update().get(pk=state.pk)
            if state is not None
            else None
        )
        locked_job = StravaSyncJob.objects.select_for_update().get(pk=job.pk)
        if lease_token and (
            locked_job.status != StravaSyncJob.Status.RUNNING
            or locked_job.lease_token != lease_token
        ):
            return "in_progress"
        locked_job.status = StravaSyncJob.Status.PENDING if more else StravaSyncJob.Status.COMPLETED
        locked_job.lease_token = ""
        locked_job.lease_until = None
        locked_job.dispatch_token = ""
        locked_job.dispatch_lease_until = None
        locked_job.last_error = ""
        locked_job.save(
            update_fields=(
                "status",
                "lease_token",
                "lease_until",
                "dispatch_token",
                "dispatch_lease_until",
                "last_error",
                "updated_at",
            )
        )
        if locked_state is not None:
            locked_state.status = (
                StravaSyncState.Status.QUEUED if more else StravaSyncState.Status.IDLE
            )
            locked_state.next_attempt_at = now
            locked_state.completed_at = None if more else now
            locked_state.lease_token = ""
            locked_state.lease_until = None
            locked_state.save(
                update_fields=(
                    "status",
                    "next_attempt_at",
                    "completed_at",
                    "lease_token",
                    "lease_until",
                    "updated_at",
                )
            )
    return "more" if more else "completed"


def _pause_job(job: StravaSyncJob, state: StravaSyncState | None, *, lease_token: str = "") -> str:
    with transaction.atomic():
        locked_state = (
            StravaSyncState.objects.select_for_update().get(pk=state.pk)
            if state is not None
            else None
        )
        locked_job = StravaSyncJob.objects.select_for_update().get(pk=job.pk)
        if lease_token and (
            locked_job.status != StravaSyncJob.Status.RUNNING
            or locked_job.lease_token != lease_token
        ):
            return "in_progress"
        locked_job.status = StravaSyncJob.Status.COMPLETED
        locked_job.lease_token = ""
        locked_job.lease_until = None
        locked_job.dispatch_token = ""
        locked_job.dispatch_lease_until = None
        locked_job.save(
            update_fields=(
                "status",
                "lease_token",
                "lease_until",
                "dispatch_token",
                "dispatch_lease_until",
                "updated_at",
            )
        )
        if locked_state is not None:
            locked_state.status = StravaSyncState.Status.PAUSED
            locked_state.lease_token = ""
            locked_state.lease_until = None
            locked_state.save(update_fields=("status", "lease_token", "lease_until", "updated_at"))
    return "paused"


def _fail_job(
    job: StravaSyncJob,
    state: StravaSyncState | None,
    error: StravaActivityError,
    *,
    lease_token: str = "",
) -> str:
    retry_index = min(max(job.attempts - 1, 0), len(SYNC_RETRY_SECONDS) - 1)
    delay = _bounded_retry_seconds(error.retry_after)
    if delay is None:
        delay = SYNC_RETRY_SECONDS[retry_index]
    with transaction.atomic():
        locked_state = (
            StravaSyncState.objects.select_for_update().get(pk=state.pk)
            if state is not None
            else None
        )
        locked_job = StravaSyncJob.objects.select_for_update().get(pk=job.pk)
        if lease_token and (
            locked_job.status != StravaSyncJob.Status.RUNNING
            or locked_job.lease_token != lease_token
        ):
            return "in_progress"
        locked_job.status = StravaSyncJob.Status.FAILED
        locked_job.last_error = str(error)[:240]
        locked_job.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        locked_job.lease_token = ""
        locked_job.lease_until = None
        locked_job.dispatch_token = ""
        locked_job.dispatch_lease_until = None
        locked_job.save(
            update_fields=(
                "status",
                "last_error",
                "next_attempt_at",
                "lease_token",
                "lease_until",
                "dispatch_token",
                "dispatch_lease_until",
                "updated_at",
            )
        )
        if locked_state is not None:
            locked_state.status = StravaSyncState.Status.FAILED
            locked_state.last_error = str(error)[:240]
            locked_state.next_attempt_at = locked_job.next_attempt_at
            locked_state.lease_token = ""
            locked_state.lease_until = None
            locked_state.save(
                update_fields=(
                    "status",
                    "last_error",
                    "next_attempt_at",
                    "lease_token",
                    "lease_until",
                    "updated_at",
                )
            )
    return "failed"


def _owned_apply_transaction[T](
    job_id: int,
    state_id: int | None,
    lease_token: str,
    apply: Callable[[StravaSyncJob, StravaSyncState | None], T],
) -> T | str:
    """Run domain writes only while the worker still owns its durable lease."""

    now = timezone.now()
    with transaction.atomic():
        try:
            locked_state, locked_job = _lock_sync_state_then_job(job_id, state_id=state_id)
        except StravaSyncState.DoesNotExist:
            return "in_progress"
        if (
            locked_job.status != StravaSyncJob.Status.RUNNING
            or locked_job.lease_token != lease_token
            or locked_job.lease_until is None
            or locked_job.lease_until <= now
        ):
            return "in_progress"
        if state_id is not None and locked_state is not None:
            if (
                locked_state.status != StravaSyncState.Status.RUNNING
                or locked_state.lease_token != lease_token
                or locked_state.lease_until is None
                or locked_state.lease_until <= now
            ):
                return "in_progress"

        lease_until = now + timedelta(
            seconds=max(1, int(getattr(settings, "STRAVA_SYNC_LEASE_SECONDS", 600)))
        )
        locked_job.lease_until = lease_until
        locked_job.save(update_fields=("lease_until", "updated_at"))
        if locked_state is not None:
            locked_state.lease_until = lease_until
            locked_state.save(update_fields=("lease_until", "updated_at"))
        result = apply(locked_job, locked_state)
        # A large page or recomputation can outlive the heartbeat. In that
        # case none of the callback's writes may become visible to a future
        # replacement worker.
        if timezone.now() >= lease_until:
            transaction.set_rollback(True)
            return "in_progress"
        return result


def process_sync_job(job_id: int) -> str:
    """Process a bounded page under a durable lease; safe for duplicate delivery."""

    now = timezone.now()
    lease_token = hashlib.sha256(f"{job_id}:{now.timestamp()}".encode()).hexdigest()
    with transaction.atomic():
        player_id = StravaSyncJob.objects.values_list("player_id", flat=True).get(pk=job_id)
        state, job = _lock_sync_state_then_job(job_id, player_id=player_id)
        if job.status == StravaSyncJob.Status.COMPLETED:
            return "completed"
        if job.status == StravaSyncJob.Status.RUNNING and job.lease_until and job.lease_until > now:
            return "in_progress"
        if job.next_attempt_at > now:
            return "retry_scheduled"
        if job.player.lifecycle != Player.Lifecycle.CONNECTED:
            return _pause_job(job, state)
        job.status = StravaSyncJob.Status.RUNNING
        job.attempts += 1
        job.lease_token = lease_token
        job.lease_until = now + timedelta(
            seconds=getattr(settings, "STRAVA_SYNC_LEASE_SECONDS", 600)
        )
        job.dispatch_token = ""
        job.dispatch_lease_until = None
        job.save(
            update_fields=(
                "status",
                "attempts",
                "lease_token",
                "lease_until",
                "dispatch_token",
                "dispatch_lease_until",
                "updated_at",
            )
        )
        if state is not None:
            state.status = StravaSyncState.Status.RUNNING
            state.started_at = state.started_at or now
            state.lease_token = lease_token
            state.lease_until = job.lease_until
            state.save(
                update_fields=("status", "started_at", "lease_token", "lease_until", "updated_at")
            )

    try:
        if job.kind == StravaSyncJob.Kind.WEBHOOK:
            if not Player.objects.filter(
                pk=job.player_id, lifecycle=Player.Lifecycle.CONNECTED
            ).exists():
                return _pause_job(job, state, lease_token=lease_token)
            event = job.webhook_event
            if event is None:
                return _finish_job(job, state=state, lease_token=lease_token)
            payload = None
            if event.aspect_type != "delete":
                # Provider I/O must not hold database locks. The authoritative
                # payload is checked again inside the guarded transaction.
                payload = fetch_activity(job.player_id, str(event.object_id))

            def apply_webhook(
                locked_job: StravaSyncJob, _locked_state: StravaSyncState | None
            ) -> str:
                locked_event = StravaWebhookEvent.objects.select_for_update().get(pk=event.pk)
                if locked_event.owner_athlete_id != locked_job.player.strava_athlete_id:
                    locked_event.last_error = "Webhook owner did not match the connected athlete."
                elif locked_event.aspect_type == "delete":
                    activity = ImportedActivity.objects.filter(
                        player=locked_job.player, provider_activity_id=str(locked_event.object_id)
                    ).first()
                    event_at = _event_datetime(locked_event.payload)
                    if (
                        activity is None
                        or event_at is None
                        or activity.provider_updated_at is None
                        or event_at >= activity.provider_updated_at
                    ):
                        remove_activity(
                            locked_job.player, str(locked_event.object_id), reason="deleted"
                        )
                    locked_event.last_error = ""
                elif not isinstance(payload, dict) or not activity_belongs_to_player(
                    locked_job.player, payload, require_athlete=True
                ):
                    locked_event.last_error = "Activity owner did not match the connected athlete."
                else:
                    import_activity(locked_job.player, payload)
                    locked_event.last_error = ""
                locked_event.processed_at = timezone.now()
                locked_event.save(update_fields=("processed_at", "last_error"))
                return "completed"

            result = _owned_apply_transaction(
                job.pk, state.pk if state is not None else None, lease_token, apply_webhook
            )
            if result == "in_progress":
                return result
            return _finish_job(job, state=state, lease_token=lease_token)

        pages_per_run = max(1, int(getattr(settings, "STRAVA_SYNC_PAGES_PER_RUN", 5)))
        page = job.page
        after = state.history_start if state is not None else timezone.now() - timedelta(days=365)
        has_more = False
        for _ in range(pages_per_run):
            if not Player.objects.filter(
                pk=job.player_id, lifecycle=Player.Lifecycle.CONNECTED
            ).exists():
                return _pause_job(job, state, lease_token=lease_token)
            activities = fetch_activity_page(job.player_id, page=page, after=after)
            page_is_full = len(activities) >= int(getattr(settings, "STRAVA_SYNC_PAGE_SIZE", 100))
            next_page = page + 1 if page_is_full else page

            def apply_page(
                locked_job: StravaSyncJob,
                locked_state: StravaSyncState | None,
                *,
                page_activities: list[dict[str, Any]] = activities,
                page_next: int = next_page,
            ) -> str:
                for payload in page_activities:
                    outcome = import_activity(locked_job.player, payload)
                    if locked_state is not None:
                        locked_state.processed_count += 1
                        if outcome in {"imported", "updated"}:
                            locked_state.imported_count += 1
                        elif outcome in {"removed", "ignored"}:
                            locked_state.rejected_count += 1
                        provider_updated_at = _provider_updated(payload)
                        if provider_updated_at is not None and (
                            locked_state.last_provider_updated_at is None
                            or provider_updated_at > locked_state.last_provider_updated_at
                        ):
                            locked_state.last_provider_updated_at = provider_updated_at
                if locked_state is not None:
                    locked_state.cursor_page = page_next
                    locked_state.save(
                        update_fields=(
                            "cursor_page",
                            "processed_count",
                            "imported_count",
                            "rejected_count",
                            "last_provider_updated_at",
                            "updated_at",
                        )
                    )
                locked_job.page = page_next
                locked_job.save(update_fields=("page", "updated_at"))
                return "applied"

            result = _owned_apply_transaction(
                job.pk, state.pk if state is not None else None, lease_token, apply_page
            )
            if result == "in_progress":
                return result
            if len(activities) < int(getattr(settings, "STRAVA_SYNC_PAGE_SIZE", 100)):
                break
            page += 1
            has_more = True
        return _finish_job(job, state=state, more=has_more, lease_token=lease_token)
    except StravaActivityError as error:
        return _fail_job(job, state, error, lease_token=lease_token)
    except Exception:
        return _fail_job(
            job,
            state,
            StravaActivityError("Activity sync failed."),
            lease_token=lease_token,
        )
