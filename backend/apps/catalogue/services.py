"""Use-case services for idempotent catalogue writes."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from .fields import _GIS_AVAILABLE
from .models import (
    ForumPost,
    LoopStatus,
    ModerationDecision,
    PayloadDeletionRequest,
    ProcessingStatus,
    Route,
    RouteLifecycle,
    RouteSource,
    RouteSourcePost,
    RouteVersion,
    SourceDenylistEntry,
    SourceStatus,
    TitleProvenance,
)


class SourceDeniedError(ValidationError):
    """Raised when an automatic import is blocked by an active denylist entry."""


@dataclass(frozen=True)
class TitleContext:
    """Thread context used to derive a stable public title."""

    thread_title: str
    start_locality: str | None = None
    end_locality: str | None = None
    loop: bool = False
    author: str | None = None
    post_date: date | datetime | None = None
    route_id: uuid.UUID | str | None = None
    disambiguate: bool = False


def _date_text(value: date | datetime) -> str:
    return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()


def generate_route_title(context: TitleContext | None = None, **kwargs: Any) -> str:
    """Generate a title using geographic, author/date, then ID fallbacks."""

    if context is None:
        context = TitleContext(**kwargs)
    elif kwargs:
        raise TypeError("Pass a TitleContext or keyword arguments, not both")
    thread = context.thread_title.strip()
    if not thread:
        raise ValidationError("A thread title is required to generate a route title.")
    start = (context.start_locality or "").strip()
    end = (context.end_locality or "").strip()
    if context.loop and start:
        title = f"{thread} — Loop from {start}"
        return _add_route_disambiguator(title, context)
    if start and end:
        title = f"{thread} — {start} → {end}"
        return _add_route_disambiguator(title, context)
    if start:
        title = f"{thread} — From {start}"
        return _add_route_disambiguator(title, context)
    if context.author and context.post_date:
        title = f"{thread} — {context.author.strip()} · {_date_text(context.post_date)}"
        return _add_route_disambiguator(title, context)
    if context.route_id is None:
        raise ValidationError("A stable route_id is required when title context is incomplete.")
    short_id = str(context.route_id).replace("-", "")[:8]
    return f"{thread} — Route {short_id}"


def _add_route_disambiguator(title: str, context: TitleContext) -> str:
    if context.disambiguate and context.route_id is not None:
        short_id = str(context.route_id).replace("-", "")[:8]
        return f"{title} — Route {short_id}"
    return title


def _prepare_geometry(value: Any) -> Any:
    if not isinstance(value, dict | list | tuple):
        return value
    payload = json.dumps(value, separators=(",", ":"))
    if not _GIS_AVAILABLE:
        return payload
    from django.contrib.gis.geos import GEOSGeometry

    geometry = GEOSGeometry(payload, srid=4326)
    if geometry.srid is None:
        geometry.srid = 4326
    return geometry


def _route_slug(route: Route) -> str:
    return f"route-{str(route.id).replace('-', '')[:12]}"


def apply_generated_title(route: Route, context: TitleContext, *, source_title: str = "") -> Route:
    """Persist thread-derived titles while retaining the original Mapy title separately."""

    if context.route_id is None:
        context = replace(context, route_id=route.id)
    route.thread_title = context.thread_title
    if source_title:
        route.original_source_title = source_title
    route.generated_title = generate_route_title(context)
    if not route.admin_title_override:
        route.display_title = route.generated_title
        if context.start_locality and (context.loop or context.end_locality):
            route.title_provenance = TitleProvenance.GEOGRAPHIC
        elif context.author and context.post_date:
            route.title_provenance = TitleProvenance.AUTHOR_DATE
        else:
            route.title_provenance = TitleProvenance.ROUTE_ID
    route.slug = route.slug or _route_slug(route)
    route.save(
        update_fields=[
            "original_source_title",
            "thread_title",
            "generated_title",
            "display_title",
            "title_provenance",
            "slug",
            "updated_at",
        ]
    )
    return route


@transaction.atomic
def register_source(
    *, route: Route, post: ForumPost, mapy_url: str, source_title: str = ""
) -> tuple[RouteSource, bool]:
    """Create or reuse a source relationship, preserving processing state."""

    route = Route.objects.select_for_update().get(pk=route.pk)
    if route.lifecycle != RouteLifecycle.PUBLISHED:
        raise ValidationError("Sources cannot be added to a quarantined or removed route.")
    if SourceDenylistEntry.objects.filter(source_url=mapy_url, active=True).exists():
        raise SourceDeniedError("This source URL is on the active denylist.")
    source, created = RouteSource.objects.get_or_create(
        mapy_url=mapy_url,
        defaults={"route": route, "source_title": source_title},
    )
    if not created and source.route_id != route.id:
        raise ValidationError("A source URL cannot be attached to two canonical routes.")
    if not created and source_title and source.source_title != source_title:
        source.source_title = source_title
        source.save(update_fields=["source_title"])
    RouteSourcePost.objects.get_or_create(source=source, post=post)
    return source, created


@transaction.atomic
def record_route_version(
    *,
    source: RouteSource,
    checksum: str,
    storage_key: str = "",
    normalized_geometry: Any = None,
    simplified_geometry: Any = None,
    distance_m: Any = None,
    ascent_m: Any = None,
    descent_m: Any = None,
    loop_status: str = LoopStatus.UNKNOWN,
    technical_status: str = ProcessingStatus.VALID,
    validation_error: str = "",
) -> tuple[RouteVersion, bool]:
    """Record a new immutable source version, or return an existing checksum."""

    if not checksum.strip():
        raise ValidationError("A route version checksum is required.")
    source_ref = RouteSource.objects.get(pk=source.pk)
    route = Route.objects.select_for_update().get(pk=source_ref.route_id)
    if route.lifecycle != RouteLifecycle.PUBLISHED:
        raise ValidationError("Versions cannot be recorded for a quarantined or removed route.")
    source = RouteSource.objects.select_for_update().get(pk=source_ref.pk)
    if SourceDenylistEntry.objects.filter(source_url=source.mapy_url, active=True).exists():
        raise SourceDeniedError("This source URL is on the active denylist.")
    existing = RouteVersion.objects.filter(source=source, checksum=checksum).first()
    if existing:
        _mark_source_success(source, technical_status=existing.technical_status, error="")
        return existing, False
    latest = (
        RouteVersion.objects.filter(source=source)
        .order_by("-version_number")
        .values_list("version_number", flat=True)
        .first()
        or 0
    )
    version = RouteVersion.objects.create(
        source=source,
        version_number=latest + 1,
        checksum=checksum,
        original_gpx_storage_key=storage_key,
        normalized_geometry=_prepare_geometry(normalized_geometry),
        simplified_geometry=_prepare_geometry(simplified_geometry),
        distance_m=distance_m,
        ascent_m=ascent_m,
        descent_m=descent_m,
        loop_status=loop_status,
        technical_status=technical_status,
        validation_error=validation_error,
    )
    _mark_source_success(source, technical_status=technical_status, error=validation_error)
    return version, True


def _mark_source_success(source: RouteSource, *, technical_status: str, error: str) -> None:
    now = timezone.now()
    source.processing_status = technical_status
    source.processed_at = now
    source.source_status = SourceStatus.AVAILABLE
    source.last_checked_at = now
    source.last_successful_check_at = now
    source.last_error = error
    source.save(
        update_fields=[
            "processing_status",
            "processed_at",
            "source_status",
            "last_checked_at",
            "last_successful_check_at",
            "last_error",
        ]
    )


@transaction.atomic
def approve_version(version: RouteVersion, *, actor: Any = None) -> Route:
    """Make a technically valid version the route's public approved version.

    Lock acquisition is deliberately ordered route, source, then version to
    match the ingestion and lifecycle services and avoid lock-order cycles.
    """

    version_ref = RouteVersion.objects.get(pk=version.pk)
    source_ref = RouteSource.objects.get(pk=version_ref.source_id)
    route = Route.objects.select_for_update().get(pk=source_ref.route_id)
    _ = RouteSource.objects.select_for_update().get(pk=source_ref.pk)
    version = RouteVersion.objects.select_for_update().get(pk=version_ref.pk)
    if version.technical_status != ProcessingStatus.VALID:
        raise ValidationError("Only a technically valid version can be published.")
    if route.lifecycle == RouteLifecycle.SOFT_DELETED:
        raise ValidationError("A soft-deleted route must be restored before publication.")
    if route.lifecycle == RouteLifecycle.QUARANTINED:
        raise ValidationError(
            "A quarantined route requires an explicit restore before publication."
        )
    now = timezone.now()
    version.approved_at = version.approved_at or now
    version.save(update_fields=["approved_at"])
    route.current_approved_version = version
    route.lifecycle = RouteLifecycle.PUBLISHED
    route.deleted_at = None
    route.quarantine_reason = ""
    route.save(
        update_fields=[
            "current_approved_version",
            "lifecycle",
            "deleted_at",
            "quarantine_reason",
            "updated_at",
        ]
    )
    ModerationDecision.objects.create(
        route=route,
        version=version,
        action=ModerationDecision.Action.PUBLISH,
        reason="Technically valid route version approved for publication.",
        actor=actor,
    )
    return route


@transaction.atomic
def review_route(route: Route, *, reason: str, actor: Any = None) -> Route:
    """Record a human suitability/technical review independently of publication."""

    if not reason.strip():
        raise ValidationError("A review reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    route.reviewed_at = timezone.now()
    route.save(update_fields=["reviewed_at", "updated_at"])
    ModerationDecision.objects.create(
        route=route,
        action=ModerationDecision.Action.REVIEW,
        reason=reason,
        actor=actor,
    )
    return route


@transaction.atomic
def quarantine_route(route: Route, *, reason: str, actor: Any = None) -> Route:
    if not reason.strip():
        raise ValidationError("A quarantine reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    route.lifecycle = RouteLifecycle.QUARANTINED
    route.quarantine_reason = reason
    route.save(update_fields=["lifecycle", "quarantine_reason", "updated_at"])
    _enqueue_payload_deletions(route, reason="Payload removed during quarantine.")
    ModerationDecision.objects.create(
        route=route, action=ModerationDecision.Action.QUARANTINE, reason=reason, actor=actor
    )
    return route


@transaction.atomic
def soft_delete_route(route: Route, *, reason: str, actor: Any = None) -> Route:
    if not reason.strip():
        raise ValidationError("A removal reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    now = timezone.now()
    route.lifecycle = RouteLifecycle.SOFT_DELETED
    route.deleted_at = now
    route.save(update_fields=["lifecycle", "deleted_at", "updated_at"])
    _enqueue_payload_deletions(route, reason="Payload removed during route removal.")
    for source in route.sources.select_for_update():
        SourceDenylistEntry.objects.update_or_create(
            source_url=source.mapy_url,
            defaults={
                "route_source": source,
                "reason": reason,
                "active": True,
                "restored_at": None,
            },
        )
    ModerationDecision.objects.create(
        route=route, action=ModerationDecision.Action.REMOVE, reason=reason, actor=actor
    )
    return route


@transaction.atomic
def restore_route(route: Route, *, actor: Any = None) -> Route:
    route = Route.objects.select_for_update().get(pk=route.pk)
    if route.lifecycle not in {RouteLifecycle.SOFT_DELETED, RouteLifecycle.QUARANTINED}:
        raise ValidationError("Only soft-deleted or quarantined routes can be restored.")
    was_soft_deleted = route.lifecycle == RouteLifecycle.SOFT_DELETED
    if not route.current_approved_version_id:
        raise ValidationError("A route requires an approved version before it can be restored.")
    route.lifecycle = (
        RouteLifecycle.PUBLISHED
        if route.current_approved_version_id
        else RouteLifecycle.QUARANTINED
    )
    route.deleted_at = None
    if route.lifecycle == RouteLifecycle.PUBLISHED:
        route.quarantine_reason = ""
    else:
        route.quarantine_reason = "Awaiting an approved route version."
    route.save(update_fields=["lifecycle", "deleted_at", "quarantine_reason", "updated_at"])
    now = timezone.now()
    if was_soft_deleted:
        SourceDenylistEntry.objects.filter(route_source__route=route, active=True).update(
            active=False, restored_at=now
        )
    ModerationDecision.objects.create(
        route=route,
        action=ModerationDecision.Action.RESTORE,
        reason="Route restored by administrator.",
        actor=actor,
    )
    return route


@transaction.atomic
def mark_source_unavailable(source: RouteSource, *, error: str = "") -> RouteSource:
    """Record a failed source check without hiding its route."""

    Route.objects.select_for_update().get(pk=source.route_id)
    source = RouteSource.objects.select_for_update().get(pk=source.pk)
    source.source_status = SourceStatus.UNAVAILABLE
    source.last_checked_at = timezone.now()
    source.last_error = error
    source.save(update_fields=["source_status", "last_checked_at", "last_error"])
    return source


@transaction.atomic
def mark_source_available(source: RouteSource, *, error: str = "") -> RouteSource:
    """Record a successful source check without changing route visibility."""

    Route.objects.select_for_update().get(pk=source.route_id)
    source = RouteSource.objects.select_for_update().get(pk=source.pk)
    now = timezone.now()
    source.source_status = SourceStatus.AVAILABLE
    source.last_checked_at = now
    source.last_successful_check_at = now
    source.last_error = error
    source.save(
        update_fields=["source_status", "last_checked_at", "last_successful_check_at", "last_error"]
    )
    return source


def _enqueue_payload_deletions(route: Route, *, reason: str) -> None:
    """Create durable deletion work without performing external I/O in a transaction."""

    versions = route.versions.select_for_update().filter(original_gpx_storage_key__gt="")
    for version in versions:
        request, created = PayloadDeletionRequest.objects.get_or_create(
            version=version,
            defaults={
                "storage_key_snapshot": version.original_gpx_storage_key,
                "removal_reason": reason,
            },
        )
        if not created and request.storage_key_snapshot != version.original_gpx_storage_key:
            raise RuntimeError("A payload deletion request has an inconsistent storage key.")
        if not created and request.status == PayloadDeletionRequest.Status.COMPLETED:
            raise RuntimeError("A completed payload deletion request still has a storage key.")


def _mark_payload_deletion_failed(request_id: int, error: str) -> PayloadDeletionRequest:
    with transaction.atomic():
        request = PayloadDeletionRequest.objects.select_for_update().get(pk=request_id)
        if request.status != PayloadDeletionRequest.Status.COMPLETED:
            request.status = PayloadDeletionRequest.Status.FAILED
            request.last_error = error
            request.save(update_fields=["status", "last_error"])
        return request


def process_payload_deletion(
    request: PayloadDeletionRequest | int,
) -> PayloadDeletionRequest:
    """Process one deletion request with retry-safe storage and DB finalization.

    The external delete happens after the attempt is durably recorded. Missing
    storage is treated as success; the request is completed only after the
    version key and removal audit metadata are committed atomically.
    """

    request_id = request.pk if isinstance(request, PayloadDeletionRequest) else request
    with transaction.atomic():
        current = PayloadDeletionRequest.objects.select_for_update().get(pk=request_id)
        if current.status == PayloadDeletionRequest.Status.COMPLETED:
            return current
        current.attempts += 1
        current.last_attempt_at = timezone.now()
        current.last_error = ""
        current.save(update_fields=["attempts", "last_attempt_at", "last_error"])
        storage_key = current.storage_key_snapshot

    try:
        if default_storage.exists(storage_key):
            default_storage.delete(storage_key)
            if default_storage.exists(storage_key):
                raise OSError(f"Storage did not remove {storage_key}")
    except Exception as exc:
        return _mark_payload_deletion_failed(request_id, str(exc))

    try:
        with transaction.atomic():
            current = PayloadDeletionRequest.objects.select_for_update().get(pk=request_id)
            if current.status == PayloadDeletionRequest.Status.COMPLETED:
                return current
            version = RouteVersion.objects.select_for_update().get(pk=current.version_id)
            if (
                version.original_gpx_storage_key
                and version.original_gpx_storage_key != current.storage_key_snapshot
            ):
                raise RuntimeError("The version storage key changed while deletion was pending.")
            if version.payload_removed_at is None:
                version.original_gpx_storage_key = ""
                version.payload_removed_at = timezone.now()
                version.payload_removal_reason = current.removal_reason
                version.save(
                    update_fields=[
                        "original_gpx_storage_key",
                        "payload_removed_at",
                        "payload_removal_reason",
                    ]
                )
            current.status = PayloadDeletionRequest.Status.COMPLETED
            current.completed_at = timezone.now()
            current.last_error = ""
            current.save(update_fields=["status", "completed_at", "last_error"])
            return current
    except Exception as exc:
        return _mark_payload_deletion_failed(request_id, str(exc))


def retry_payload_deletions(*, limit: int | None = None) -> list[PayloadDeletionRequest]:
    """Retry pending and failed payload deletions in request order."""

    requests = PayloadDeletionRequest.objects.filter(
        status__in=[PayloadDeletionRequest.Status.PENDING, PayloadDeletionRequest.Status.FAILED]
    ).order_by("requested_at", "pk")
    if limit is not None:
        requests = requests[:limit]
    return [process_payload_deletion(request) for request in requests]
