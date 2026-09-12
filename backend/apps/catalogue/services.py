"""Use-case services for idempotent catalogue writes."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .fields import _GIS_AVAILABLE
from .models import (
    Category,
    ForumPost,
    LoopStatus,
    ModerationDecision,
    PayloadDeletionRequest,
    ProcessingStatus,
    Route,
    RouteCategory,
    RouteLifecycle,
    RouteSource,
    RouteSourceMerge,
    RouteSourcePost,
    RouteVersion,
    SimilarityRelationship,
    SourceDenylistEntry,
    SourceStatus,
    TitleProvenance,
)

logger = logging.getLogger(__name__)


def _route_state(route: Route) -> dict[str, Any]:
    """Serialize moderation-relevant state for an append-only audit record."""

    return {
        "route_id": str(route.pk),
        "lifecycle": route.lifecycle,
        "deleted_at": route.deleted_at.isoformat() if route.deleted_at else None,
        "quarantine_reason": route.quarantine_reason,
        "current_approved_version_id": (
            str(route.current_approved_version_id) if route.current_approved_version_id else None
        ),
        "reviewed_at": route.reviewed_at.isoformat() if route.reviewed_at else None,
        "display_title": route.display_title,
        "generated_title": route.generated_title,
        "title_provenance": route.title_provenance,
        "admin_title_override": route.admin_title_override,
        "sources": [
            {
                "id": source.pk,
                "url": source.mapy_url,
                "status": source.source_status,
            }
            for source in route.sources.all().order_by("pk")
        ],
        "merged_sources": [
            {
                "id": link.pk,
                "source_id": link.source_id,
                "active": link.active,
            }
            for link in route.merged_source_links.all().order_by("pk")
        ],
        "denylist": [
            {
                "id": entry.pk,
                "source_url": entry.source_url,
                "active": entry.active,
                "restored_at": entry.restored_at.isoformat() if entry.restored_at else None,
            }
            for entry in SourceDenylistEntry.objects.filter(route_source__route=route).order_by(
                "pk"
            )
        ],
        "payload_deletions": [
            {
                "id": request.pk,
                "version_id": request.version_id,
                "storage_key_snapshot": request.storage_key_snapshot,
                "status": request.status,
            }
            for request in PayloadDeletionRequest.objects.filter(
                version__source__route=route
            ).order_by("pk")
        ],
    }


def _source_state(source: RouteSource) -> dict[str, Any]:
    return {
        "source_id": source.pk,
        "source_url": source.mapy_url,
        "source_status": source.source_status,
        "last_checked_at": source.last_checked_at.isoformat() if source.last_checked_at else None,
        "last_error": source.last_error,
    }


def _relationship_state(relationship: SimilarityRelationship | None) -> dict[str, Any]:
    if relationship is None:
        return {"exists": False}
    return {
        "exists": True,
        "id": relationship.pk,
        "relationship_type": relationship.relationship_type,
        "reason": relationship.decision_reason,
        "decided_at": relationship.decided_at.isoformat() if relationship.decided_at else None,
        "similarity_score": str(relationship.similarity_score)
        if relationship.similarity_score is not None
        else None,
        "evidence": relationship.evidence,
    }


def _audit_metadata(
    before: dict[str, Any], after: dict[str, Any], **metadata: Any
) -> dict[str, Any]:
    return {"before": before, "after": after, **metadata}


def _refresh_spatial_products(route: Route, version: RouteVersion | None = None) -> None:
    """Update derived browse products inside the lifecycle transaction."""

    # Import lazily: spatial imports the catalogue models and must remain an
    # optional quality-suite dependency when GDAL is unavailable.
    from .spatial import generate_browse_geometries, refresh_route_heatmap

    selected = version or route.current_approved_version
    if selected is not None:
        generate_browse_geometries(selected)
    refresh_route_heatmap(route)


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
def update_route_metadata(
    route: Route,
    *,
    reason: str,
    actor: Any = None,
    admin_title_override: str | None = None,
) -> Route:
    """Update owner-curated metadata without allowing lifecycle bypasses."""

    if not reason.strip():
        raise ValidationError("A metadata update reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    before = _route_state(route)
    route.admin_title_override = (admin_title_override or "").strip()
    if len(route.admin_title_override) > 500:
        raise ValidationError("A curated title cannot exceed 500 characters.")
    if route.admin_title_override:
        route.display_title = route.admin_title_override
        route.title_provenance = TitleProvenance.ADMIN_OVERRIDE
    else:
        route.display_title = route.generated_title
        prior_provenance = None
        for prior_decision in ModerationDecision.objects.filter(
            route=route, action=ModerationDecision.Action.ROUTE_METADATA
        ).order_by("-created_at", "-pk"):
            candidate = (prior_decision.metadata or {}).get("before", {}).get("title_provenance")
            if (
                candidate in {value for value, _ in TitleProvenance.choices}
                and candidate != TitleProvenance.ADMIN_OVERRIDE
            ):
                prior_provenance = candidate
                break
        route.title_provenance = (
            prior_provenance
            if prior_provenance in {value for value, _ in TitleProvenance.choices}
            and prior_provenance != TitleProvenance.ADMIN_OVERRIDE
            else TitleProvenance.ROUTE_ID
        )
    route.full_clean()
    route.save(
        update_fields=["admin_title_override", "display_title", "title_provenance", "updated_at"]
    )
    ModerationDecision.objects.create(
        route=route,
        action=ModerationDecision.Action.ROUTE_METADATA,
        reason=reason,
        metadata=_audit_metadata(before, _route_state(route)),
        actor=actor,
    )
    return route


@transaction.atomic
def create_category(
    *, name: str, slug: str, description: str = "", reason: str, actor: Any = None
) -> Category:
    name, slug, description = name.strip(), slug.strip(), description.strip()
    if not reason.strip():
        raise ValidationError("A category reason is required.")
    if not name or not slug:
        raise ValidationError("A category name and slug are required.")
    category = Category(name=name, slug=slug, description=description)
    try:
        with transaction.atomic():
            category.full_clean()
            category.save()
    except IntegrityError as exc:
        raise ValidationError("A category with this slug already exists.") from exc
    ModerationDecision.objects.create(
        route=None,
        action=ModerationDecision.Action.CATEGORY_CREATE,
        reason=reason,
        metadata={
            "before": {"category_id": None, "exists": False},
            "after": {
                "category_id": category.pk,
                "name": name,
                "slug": slug,
                "description": description,
            },
        },
        actor=actor,
    )
    return category


@transaction.atomic
def update_category(
    category: Category,
    *,
    name: str,
    slug: str,
    description: str = "",
    reason: str,
    actor: Any = None,
) -> Category:
    name, slug, description = name.strip(), slug.strip(), description.strip()
    if not reason.strip():
        raise ValidationError("A category reason is required.")
    if not name or not slug:
        raise ValidationError("A category name and slug are required.")
    category = Category.objects.select_for_update().get(pk=category.pk)
    before = {
        "category_id": category.pk,
        "name": category.name,
        "slug": category.slug,
        "description": category.description,
    }
    category.name, category.slug, category.description = name, slug, description
    try:
        with transaction.atomic():
            category.full_clean()
            category.save(update_fields=["name", "slug", "description"])
    except IntegrityError as exc:
        raise ValidationError("A category with this slug already exists.") from exc
    ModerationDecision.objects.create(
        route=None,
        action=ModerationDecision.Action.CATEGORY_UPDATE,
        reason=reason,
        metadata=_audit_metadata(
            before, {**before, "name": name, "slug": slug, "description": description}
        ),
        actor=actor,
    )
    return category


@transaction.atomic
def delete_category(category: Category, *, reason: str, actor: Any = None) -> None:
    if not reason.strip():
        raise ValidationError("A category reason is required.")
    category = Category.objects.select_for_update().get(pk=category.pk)
    if RouteCategory.objects.filter(category=category).exists():
        raise ValidationError("Remove category assignments before deleting the category.")
    before = {
        "category_id": category.pk,
        "name": category.name,
        "slug": category.slug,
        "description": category.description,
    }
    category.delete()
    ModerationDecision.objects.create(
        route=None,
        action=ModerationDecision.Action.CATEGORY_DELETE,
        reason=reason,
        metadata=_audit_metadata(before, {"category_id": before["category_id"], "deleted": True}),
        actor=actor,
    )


@transaction.atomic
def assign_route_category(
    route: Route, category: Category, *, reason: str, actor: Any = None
) -> RouteCategory:
    if not reason.strip():
        raise ValidationError("A category assignment reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    category = Category.objects.get(pk=category.pk)
    link, created = RouteCategory.objects.get_or_create(
        route=route, category=category, defaults={"assigned_by": actor}
    )
    if not created:
        raise ValidationError("This category is already assigned to the route.")
    ModerationDecision.objects.create(
        route=route,
        action=ModerationDecision.Action.CATEGORY_ASSIGN,
        reason=reason,
        metadata=_audit_metadata(
            {"category_id": category.pk, "assigned": False},
            {"category_id": category.pk, "assigned": True, "link_id": link.pk},
        ),
        actor=actor,
    )
    return link


@transaction.atomic
def remove_route_category(
    route: Route, category: Category, *, reason: str, actor: Any = None
) -> None:
    if not reason.strip():
        raise ValidationError("A category removal reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    link = RouteCategory.objects.select_for_update().filter(route=route, category=category).first()
    if link is None:
        raise ValidationError("This category is not assigned to the route.")
    before = {"category_id": category.pk, "assigned": True, "link_id": link.pk}
    link.delete()
    ModerationDecision.objects.create(
        route=route,
        action=ModerationDecision.Action.CATEGORY_UNASSIGN,
        reason=reason,
        metadata=_audit_metadata(
            before,
            {"category_id": category.pk, "assigned": False},
        ),
        actor=actor,
    )


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
    elevation_profile: list[dict[str, float]] | None = None,
    loop_status: str = LoopStatus.UNKNOWN,
    technical_status: str = ProcessingStatus.VALID,
    validation_error: str = "",
) -> tuple[RouteVersion, bool]:
    """Record a new immutable source version, or return an existing checksum."""

    if not checksum.strip():
        raise ValidationError("A route version checksum is required.")
    source_ref = RouteSource.objects.get(pk=source.pk)
    route = Route.objects.select_for_update().get(pk=source_ref.route_id)
    if route.lifecycle == RouteLifecycle.SOFT_DELETED:
        raise ValidationError("Versions cannot be recorded for a removed route.")
    if route.lifecycle == RouteLifecycle.QUARANTINED and storage_key:
        raise ValidationError("Quarantined route versions cannot retain original payloads.")
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
        elevation_profile=elevation_profile or [],
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
def approve_version(
    version: RouteVersion,
    *,
    actor: Any = None,
    reason: str = "Technically valid route version approved for publication.",
) -> Route:
    """Make a technically valid version the route's public approved version.

    Lock acquisition is deliberately ordered route, source, then version to
    match the ingestion and lifecycle services and avoid lock-order cycles.
    """

    version_ref = RouteVersion.objects.get(pk=version.pk)
    if not reason.strip():
        raise ValidationError("A version selection reason is required.")
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
    before = _route_state(route)
    previous_version_id = route.current_approved_version_id
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
        action=(
            ModerationDecision.Action.SELECT_VERSION
            if previous_version_id and previous_version_id != version.pk
            else ModerationDecision.Action.PUBLISH
        ),
        reason=reason,
        metadata=_audit_metadata(
            before,
            _route_state(route),
            previous_approved_version_id=(
                str(previous_version_id) if previous_version_id else None
            ),
        ),
        actor=actor,
    )
    _refresh_spatial_products(route, version)
    return route


# Explicit names used by moderation callers when switching between historical
# immutable versions.  The same transactional guard as initial publication is
# intentionally reused.
select_approved_version = approve_version
select_route_version = approve_version


@transaction.atomic
def review_route(
    route: Route,
    *,
    reason: str,
    actor: Any = None,
    technical_validity: bool = False,
    source_context: bool = False,
    content_suitability: bool = False,
) -> Route:
    """Record review evidence independently of publication.

    The public badge requires all three explicit checks. A timestamp alone is
    intentionally not enough, preserving the distinction between review and a
    safety, passability, or legal-access claim.
    """

    if not reason.strip():
        raise ValidationError("A review reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    before = _route_state(route)
    route.reviewed_at = timezone.now()
    route.save(update_fields=["reviewed_at", "updated_at"])
    ModerationDecision.objects.create(
        route=route,
        action=ModerationDecision.Action.REVIEW,
        reason=reason,
        metadata={
            **_audit_metadata(before, _route_state(route)),
            "technical_validity": technical_validity,
            "source_context": source_context,
            "content_suitability": content_suitability,
        },
        actor=actor,
    )
    return route


@transaction.atomic
def quarantine_route(
    route: Route,
    *,
    reason: str,
    actor: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Route:
    if not reason.strip():
        raise ValidationError("A quarantine reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    before = _route_state(route)
    route.lifecycle = RouteLifecycle.QUARANTINED
    route.quarantine_reason = reason
    route.save(update_fields=["lifecycle", "quarantine_reason", "updated_at"])
    request_ids = _enqueue_payload_deletions(route, reason="Payload removed during quarantine.")
    _schedule_payload_deletions(request_ids)
    ModerationDecision.objects.create(
        route=route,
        action=ModerationDecision.Action.QUARANTINE,
        reason=reason,
        metadata=_audit_metadata(before, _route_state(route), **(metadata or {})),
        actor=actor,
    )
    _refresh_spatial_products(route)
    return route


@transaction.atomic
def soft_delete_route(route: Route, *, reason: str, actor: Any = None) -> Route:
    if not reason.strip():
        raise ValidationError("A removal reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    before = _route_state(route)
    now = timezone.now()
    route.lifecycle = RouteLifecycle.SOFT_DELETED
    route.deleted_at = now
    route.save(update_fields=["lifecycle", "deleted_at", "updated_at"])
    request_ids = _enqueue_payload_deletions(route, reason="Payload removed during route removal.")
    _schedule_payload_deletions(request_ids)
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
        route=route,
        action=ModerationDecision.Action.REMOVE,
        reason=reason,
        metadata=_audit_metadata(before, _route_state(route)),
        actor=actor,
    )
    _refresh_spatial_products(route)
    return route


@transaction.atomic
def restore_route(
    route: Route,
    *,
    reason: str = "Route restored by administrator.",
    actor: Any = None,
) -> Route:
    if not reason.strip():
        raise ValidationError("A restoration reason is required.")
    route = Route.objects.select_for_update().get(pk=route.pk)
    if route.lifecycle not in {RouteLifecycle.SOFT_DELETED, RouteLifecycle.QUARANTINED}:
        raise ValidationError("Only soft-deleted or quarantined routes can be restored.")
    before = _route_state(route)
    was_soft_deleted = route.lifecycle == RouteLifecycle.SOFT_DELETED
    if not route.current_approved_version_id:
        # Automatic duplicate quarantine happens before publication, so an
        # explicit restore must be able to approve its valid retained version
        # without a second non-transactional import round trip.
        candidate = (
            RouteVersion.objects.select_for_update()
            .filter(source__route=route, technical_status=ProcessingStatus.VALID)
            .order_by("-created_at", "-pk")
            .first()
        )
        if candidate is None:
            raise ValidationError("A route requires an approved version before it can be restored.")
        candidate.approved_at = candidate.approved_at or timezone.now()
        candidate.save(update_fields=["approved_at"])
        route.current_approved_version = candidate
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
    route.save(
        update_fields=[
            "current_approved_version",
            "lifecycle",
            "deleted_at",
            "quarantine_reason",
            "updated_at",
        ]
    )
    now = timezone.now()
    if was_soft_deleted:
        SourceDenylistEntry.objects.filter(route_source__route=route, active=True).update(
            active=False, restored_at=now
        )
    ModerationDecision.objects.create(
        route=route,
        action=ModerationDecision.Action.RESTORE,
        reason=reason,
        metadata=_audit_metadata(before, _route_state(route), denylist_restored=was_soft_deleted),
        actor=actor,
    )
    _refresh_spatial_products(route)
    return route


@transaction.atomic
def restore_denylist_entry(
    entry: SourceDenylistEntry, *, reason: str, actor: Any = None
) -> SourceDenylistEntry:
    """Explicitly release a source URL so automatic imports may resume."""

    if not reason.strip():
        raise ValidationError("A denylist restoration reason is required.")
    entry_ref = SourceDenylistEntry.objects.values("pk", "route_source_id").get(pk=entry.pk)
    route_source_id = entry_ref["route_source_id"]
    if not route_source_id:
        raise ValidationError("Only denylist entries associated with a route can be restored.")
    route_id = RouteSource.objects.values_list("route_id", flat=True).get(pk=route_source_id)
    # Lifecycle lock order is route, then entry. Resolve foreign keys before
    # acquiring either lock so restoration cannot deadlock with route actions.
    route = Route.objects.select_for_update().get(pk=route_id)
    entry = SourceDenylistEntry.objects.select_for_update().get(pk=entry.pk)
    if not entry.active:
        raise ValidationError("This denylist entry has already been restored.")
    if entry.route_source_id != route_source_id:
        raise ValidationError("Only denylist entries associated with a route can be restored.")
    if not RouteSource.objects.filter(pk=entry.route_source_id, route_id=route.pk).exists():
        raise ValidationError("The denylist entry source no longer belongs to its route.")
    before = {
        "active": entry.active,
        "source_url": entry.source_url,
        "restored_at": None,
    }
    now = timezone.now()
    entry.active = False
    entry.restored_at = now
    entry.save(update_fields=["active", "restored_at"])
    ModerationDecision.objects.create(
        route=route,
        action=ModerationDecision.Action.DENYLIST_RESTORE,
        reason=reason,
        metadata=_audit_metadata(
            before,
            {"active": False, "source_url": entry.source_url, "restored_at": now.isoformat()},
        ),
        actor=actor,
    )
    return entry


@transaction.atomic
def keep_both_routes(
    route_a: Route,
    route_b: Route,
    *,
    reason: str,
    actor: Any = None,
    evidence: dict[str, Any] | None = None,
) -> SimilarityRelationship:
    """Record an explicit keep-both decision for a similar route pair."""

    if not reason.strip():
        raise ValidationError("A keep-both reason is required.")
    if route_a.pk == route_b.pk:
        raise ValidationError("A route cannot be kept alongside itself.")
    first, second = sorted([route_a.pk, route_b.pk], key=str)
    first_route = Route.objects.select_for_update().get(pk=first)
    second_route = Route.objects.select_for_update().get(pk=second)
    before_states = {
        str(selected.pk): _route_state(selected) for selected in (first_route, second_route)
    }
    relationship_before = _relationship_state(
        SimilarityRelationship.objects.filter(route_a_id=first, route_b_id=second).first()
    )
    for selected in (first_route, second_route):
        if selected.lifecycle == RouteLifecycle.QUARANTINED:
            restore_route(selected, actor=actor)
    alias_ids = list(
        RouteSourceMerge.objects.filter(active=True)
        .filter(
            Q(canonical_route_id=first, source__route_id=second)
            | Q(canonical_route_id=second, source__route_id=first)
        )
        .values_list("pk", flat=True)
    )
    if alias_ids:
        RouteSourceMerge.objects.filter(pk__in=alias_ids).update(
            active=False,
            deactivated_at=timezone.now(),
            deactivated_reason=reason,
        )
        from .spatial import schedule_viewport_filter_cache_invalidation

        schedule_viewport_filter_cache_invalidation()
    relationship, _ = SimilarityRelationship.objects.get_or_create(
        route_a_id=first,
        route_b_id=second,
        defaults={
            "relationship_type": SimilarityRelationship.RelationshipType.VARIANT,
            "evidence": evidence or {},
        },
    )
    relationship.relationship_type = SimilarityRelationship.RelationshipType.VARIANT
    relationship.decision_reason = reason
    relationship.decided_at = timezone.now()
    if evidence:
        relationship.evidence = evidence
    relationship.save(
        update_fields=["relationship_type", "decision_reason", "decided_at", "evidence"]
    )
    for route in (first, second):
        relationship_after = _relationship_state(relationship)
        ModerationDecision.objects.create(
            route_id=route,
            action=ModerationDecision.Action.KEEP_BOTH,
            reason=reason,
            metadata={
                **_audit_metadata(
                    before_states[str(route)],
                    _route_state(Route.objects.get(pk=route)),
                ),
                "relationship_before": relationship_before,
                "relationship_after": relationship_after,
                "related_route_id": str(second if route == first else first),
                **(evidence or {}),
            },
            actor=actor,
        )
    return relationship


@transaction.atomic
def merge_route_sources(
    canonical_route: Route,
    duplicate_route: Route,
    *,
    reason: str,
    actor: Any = None,
) -> list[RouteSourceMerge]:
    """Merge provenance sources under a canonical route without rewriting history."""

    if not reason.strip():
        raise ValidationError("A source-merge reason is required.")
    if canonical_route.pk == duplicate_route.pk:
        raise ValidationError("A route cannot merge sources with itself.")
    first, second = sorted([canonical_route.pk, duplicate_route.pk], key=str)
    Route.objects.select_for_update().get(pk=first)
    Route.objects.select_for_update().get(pk=second)
    before_states = {
        str(route_id): _route_state(Route.objects.get(pk=route_id)) for route_id in (first, second)
    }
    relationship_before = _relationship_state(
        SimilarityRelationship.objects.filter(route_a_id=first, route_b_id=second).first()
    )
    links: list[RouteSourceMerge] = []
    for source in RouteSource.objects.filter(route_id=duplicate_route.pk).order_by("pk"):
        existing = RouteSourceMerge.objects.filter(
            canonical_route_id=canonical_route.pk, source_id=source.pk
        ).first()
        if existing is not None:
            if (
                RouteSourceMerge.objects.filter(source_id=source.pk, active=True)
                .exclude(pk=existing.pk)
                .exists()
            ):
                raise ValidationError("A source is already merged into another canonical route.")
            existing.active = True
            existing.deactivated_at = None
            existing.deactivated_reason = ""
            existing.reason = reason
            existing.actor = actor
            existing.save(
                update_fields=[
                    "active",
                    "deactivated_at",
                    "deactivated_reason",
                    "reason",
                    "actor",
                ]
            )
            links.append(existing)
            continue
        if RouteSourceMerge.objects.filter(source_id=source.pk, active=True).exists():
            raise ValidationError("A source is already merged into another canonical route.")
        links.append(
            RouteSourceMerge.objects.create(
                canonical_route_id=canonical_route.pk,
                source_id=source.pk,
                reason=reason,
                actor=actor,
            )
        )
    duplicate_route = Route.objects.get(pk=duplicate_route.pk)
    if duplicate_route.lifecycle == RouteLifecycle.PUBLISHED:
        quarantine_route(
            duplicate_route,
            reason="Route identity merged into another canonical route.",
            actor=actor,
            metadata={"canonical_route_id": str(canonical_route.pk)},
        )
    relationship, _ = SimilarityRelationship.objects.get_or_create(
        route_a_id=first,
        route_b_id=second,
        defaults={"relationship_type": SimilarityRelationship.RelationshipType.SUSPECTED_DUPLICATE},
    )
    relationship.relationship_type = SimilarityRelationship.RelationshipType.SUSPECTED_DUPLICATE
    relationship.decision_reason = reason
    relationship.decided_at = timezone.now()
    relationship.save(update_fields=["relationship_type", "decision_reason", "decided_at"])
    metadata = {
        "canonical_route_id": str(canonical_route.pk),
        "merged_source_ids": [link.source_id for link in links],
        "relationship_before": relationship_before,
        "relationship_after": _relationship_state(relationship),
    }
    ModerationDecision.objects.create(
        route=canonical_route,
        action=ModerationDecision.Action.MERGE_SOURCES,
        reason=reason,
        metadata=_audit_metadata(
            before_states[str(canonical_route.pk)],
            _route_state(Route.objects.get(pk=canonical_route.pk)),
            **metadata,
        ),
        actor=actor,
    )
    ModerationDecision.objects.create(
        route=duplicate_route,
        action=ModerationDecision.Action.MERGE_SOURCES,
        reason=reason,
        metadata=_audit_metadata(
            before_states[str(duplicate_route.pk)],
            _route_state(Route.objects.get(pk=duplicate_route.pk)),
            **metadata,
        ),
        actor=actor,
    )
    return links


@transaction.atomic
def quarantine_suspected_duplicate(
    route: Route,
    *,
    reason: str,
    relationship: SimilarityRelationship | None = None,
    actor: Any = None,
) -> Route:
    """Quarantine a suspected duplicate and retain its similarity evidence."""

    if relationship is not None:
        if route.pk not in {relationship.route_a_id, relationship.route_b_id}:
            raise ValidationError("The similarity relationship does not contain this route.")
        metadata = {
            "relationship_id": relationship.pk,
            "similarity_evidence": relationship.evidence,
            "similarity_score": str(relationship.similarity_score)
            if relationship.similarity_score is not None
            else None,
        }
    else:
        metadata = {}
    return quarantine_route(route, reason=reason, actor=actor, metadata=metadata)


# Short aliases used by task dispatchers and moderation adapters.
merge_sources = merge_route_sources
keep_both = keep_both_routes
quarantine_duplicate = quarantine_suspected_duplicate


@transaction.atomic
def mark_source_unavailable(
    source: RouteSource, *, error: str = "", actor: Any = None, reason: str | None = None
) -> RouteSource:
    """Record a failed source check without hiding its route."""

    if actor is not None and not (reason or "").strip():
        raise ValidationError("A source-unavailable reason is required.")
    Route.objects.select_for_update().get(pk=source.route_id)
    source = RouteSource.objects.select_for_update().get(pk=source.pk)
    before = _source_state(source)
    source.source_status = SourceStatus.UNAVAILABLE
    source.last_checked_at = timezone.now()
    source.last_error = error
    source.save(update_fields=["source_status", "last_checked_at", "last_error"])
    ModerationDecision.objects.create(
        route_id=source.route_id,
        action=ModerationDecision.Action.SOURCE_UNAVAILABLE,
        reason=(reason or error or "Source check failed.").strip(),
        metadata=_audit_metadata(before, _source_state(source)),
        actor=actor,
    )
    return source


@transaction.atomic
def mark_source_available(
    source: RouteSource,
    *,
    error: str = "",
    actor: Any = None,
    reason: str | None = None,
) -> RouteSource:
    """Record a successful source check without changing route visibility."""

    if actor is not None and not (reason or "").strip():
        raise ValidationError("A source-available reason is required.")
    Route.objects.select_for_update().get(pk=source.route_id)
    source = RouteSource.objects.select_for_update().get(pk=source.pk)
    before = _source_state(source)
    now = timezone.now()
    source.source_status = SourceStatus.AVAILABLE
    source.last_checked_at = now
    source.last_successful_check_at = now
    source.last_error = error
    source.save(
        update_fields=["source_status", "last_checked_at", "last_successful_check_at", "last_error"]
    )
    ModerationDecision.objects.create(
        route_id=source.route_id,
        action=ModerationDecision.Action.SOURCE_AVAILABLE,
        reason=(reason or error or "Source check succeeded.").strip(),
        metadata=_audit_metadata(before, _source_state(source)),
        actor=actor,
    )
    return source


def _enqueue_payload_deletions(route: Route, *, reason: str) -> list[int]:
    """Create durable deletion work without performing external I/O in a transaction."""

    request_ids: list[int] = []
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
        request_ids.append(request.pk)
    return request_ids


def _schedule_payload_deletions(request_ids: list[int]) -> None:
    """Dispatch deletion work only after the lifecycle transaction commits."""

    if not request_ids:
        return

    def dispatch() -> None:
        from apps.ingestion.tasks import process_payload_deletion_task

        for request_id in request_ids:
            try:
                process_payload_deletion_task.delay(request_id)
            except Exception:
                logger.warning("Could not dispatch payload deletion %s", request_id, exc_info=True)

    transaction.on_commit(dispatch)


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
