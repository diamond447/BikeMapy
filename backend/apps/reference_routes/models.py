"""Immutable, provenance-aware completion reference route records."""

from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.catalogue.fields import RouteGeometryField


class ReferenceSourceKind(models.TextChoices):
    OSM_NUMBERED = "osm_numbered", "OpenStreetMap numbered cycling routes"
    VIA_CZECHIA = "via_czechia", "Via Czechia"


class ReferenceImportStatus(models.TextChoices):
    DISCOVERED = "discovered", "Discovered"
    VALID = "valid", "Valid"
    INVALID = "invalid", "Invalid"
    BLOCKED = "blocked", "Blocked"
    FAILED = "failed", "Failed"


class ReferenceValidationStatus(models.TextChoices):
    VALID = "valid", "Valid"
    INVALID = "invalid", "Invalid"


class ReferenceCollection(models.Model):
    """A bounded catalogue with its own source identity and attribution."""

    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=255)
    source_kind = models.CharField(max_length=32, choices=ReferenceSourceKind.choices)
    source_url = models.URLField(max_length=1000)
    attribution = models.TextField()
    licence = models.CharField(max_length=255)
    active = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]
        indexes = [models.Index(fields=["source_kind", "active"], name="ref_coll_kind_active_idx")]

    def __str__(self) -> str:
        return self.name


class ReferenceImport(models.Model):
    """One complete, hashed source snapshot; raw source data is never rewritten."""

    collection = models.ForeignKey(
        ReferenceCollection, on_delete=models.PROTECT, related_name="imports"
    )
    checksum = models.CharField(max_length=64)
    endpoint = models.URLField(max_length=1000)
    query_text = models.TextField(blank=True)
    retrieved_at = models.DateTimeField(default=timezone.now)
    source_timestamp = models.DateTimeField(blank=True, null=True)
    response_metadata = models.JSONField(default=dict, blank=True)
    raw_payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16,
        choices=ReferenceImportStatus.choices,
        default=ReferenceImportStatus.DISCOVERED,
    )
    diagnostics = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-retrieved_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "checksum"], name="ref_import_collection_checksum_unique"
            )
        ]
        indexes = [
            models.Index(
                fields=["collection", "-retrieved_at"], name="ref_import_collection_date_idx"
            ),
            models.Index(fields=["status"], name="ref_import_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.collection.slug}:{self.checksum[:12]}"


class ReferenceRoute(models.Model):
    """Stable route identity; a number is scoped to collection and source ID."""

    class RouteType(models.TextChoices):
        ROUTE = "route", "Route"
        STAGE = "stage", "Stage"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    collection = models.ForeignKey(
        ReferenceCollection, on_delete=models.PROTECT, related_name="routes"
    )
    source_identifier = models.CharField(max_length=255)
    route_number = models.CharField(max_length=32, blank=True)
    title = models.CharField(max_length=500)
    operator = models.CharField(max_length=255, blank=True)
    network = models.CharField(max_length=32, blank=True)
    route_type = models.CharField(max_length=16, choices=RouteType.choices, default=RouteType.ROUTE)
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="stages", blank=True, null=True
    )
    source_tags = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=False)
    current_version = models.ForeignKey(
        "ReferenceRouteVersion",
        on_delete=models.SET_NULL,
        related_name="current_for_routes",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["route_number", "title", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["collection", "source_identifier"],
                name="ref_route_collection_source_unique",
            ),
            models.CheckConstraint(
                condition=Q(route_type="stage", parent__isnull=False) | Q(route_type="route"),
                name="ref_stage_has_parent",
            ),
        ]
        indexes = [
            models.Index(
                fields=["collection", "active", "route_number"], name="ref_route_browse_idx"
            ),
            models.Index(fields=["parent", "active"], name="ref_route_parent_idx"),
        ]

    def __str__(self) -> str:
        return self.title or self.source_identifier


class ImmutableReferenceVersionQuerySet(models.QuerySet["ReferenceRouteVersion"]):
    def delete(self) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Reference route versions cannot be deleted.", set(self))


class ReferenceRouteVersion(models.Model):
    """An immutable source representation and normalized geometry version."""

    route = models.ForeignKey(ReferenceRoute, on_delete=models.PROTECT, related_name="versions")
    source_import = models.ForeignKey(
        ReferenceImport, on_delete=models.PROTECT, related_name="route_versions"
    )
    version_number = models.PositiveIntegerField()
    source_version_identifier = models.CharField(max_length=255, blank=True)
    checksum = models.CharField(max_length=64)
    source_geometry = models.JSONField(default=dict)
    normalized_geometry = RouteGeometryField(srid=4326, spatial_index=True)
    provenance = models.JSONField(default=dict)
    attribution = models.TextField()
    validation_status = models.CharField(max_length=16, choices=ReferenceValidationStatus.choices)
    diagnostics = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    objects = ImmutableReferenceVersionQuerySet.as_manager()

    class Meta:
        ordering = ["route_id", "version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["route", "version_number"], name="ref_version_route_number_unique"
            ),
            models.UniqueConstraint(
                fields=["route", "checksum"], name="ref_version_route_checksum_unique"
            ),
            models.CheckConstraint(
                condition=Q(version_number__gt=0), name="ref_version_positive_number"
            ),
        ]
        indexes = [
            models.Index(fields=["route", "active"], name="ref_version_route_active_idx"),
            models.Index(fields=["validation_status"], name="ref_version_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.route} v{self.version_number}"

    def save(self, *args: object, **kwargs: object) -> None:  # noqa: DJ012
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            immutable = (
                "route_id",
                "source_import_id",
                "version_number",
                "source_version_identifier",
                "checksum",
                "source_geometry",
                "normalized_geometry",
                "provenance",
                "attribution",
                "validation_status",
                "diagnostics",
                "created_at",
            )
            changed = [field for field in immutable if getattr(old, field) != getattr(self, field)]
            if changed:
                raise ValidationError(
                    {"version": f"Historical reference version is immutable: {', '.join(changed)}."}
                )
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Reference route versions cannot be deleted.", {self})


class ReferenceRecomputation(models.Model):
    """Durable hook consumed by the later completion recomputation feature."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    route_version = models.ForeignKey(
        ReferenceRouteVersion, on_delete=models.PROTECT, related_name="recomputations"
    )
    reason = models.CharField(max_length=255)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(default=timezone.now)
    scheduled_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["status", "created_at"], name="ref_recompute_queue_idx")]

    def __str__(self) -> str:
        return f"{self.route_version} ({self.status})"
