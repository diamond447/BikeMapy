"""Persistent catalogue entities and their provenance relationships."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from .fields import RouteGeometryField, RoutePolygonField


class ProcessingStatus(models.TextChoices):
    DISCOVERED = "discovered", "Discovered"
    PROCESSING = "processing", "Processing"
    VALID = "valid", "Valid"
    INVALID = "invalid", "Invalid"
    FAILED = "failed", "Failed"
    BLOCKED = "blocked", "Blocked"


class SourceStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    UNAVAILABLE = "unavailable", "Unavailable"
    UNKNOWN = "unknown", "Unknown"


class RouteLifecycle(models.TextChoices):
    PUBLISHED = "published", "Published"
    QUARANTINED = "quarantined", "Quarantined"
    SOFT_DELETED = "soft_deleted", "Soft deleted"


class LoopStatus(models.TextChoices):
    LOOP = "loop", "Loop"
    POINT_TO_POINT = "point_to_point", "Point to point"
    UNKNOWN = "unknown", "Unknown"


class TitleProvenance(models.TextChoices):
    GEOGRAPHIC = "geographic", "Thread and geographic context"
    AUTHOR_DATE = "author_date", "Thread, author, and post date"
    ROUTE_ID = "route_id", "Thread and stable route ID"
    ADMIN_OVERRIDE = "admin_override", "Administrator override"


def _default_route_slug() -> str:
    return f"route-{uuid.uuid4().hex[:12]}"


class ForumAuthor(models.Model):
    """A BikeForum author; the username is retained as source attribution."""

    username = models.CharField(max_length=150)
    external_id = models.CharField(max_length=255, blank=True)
    profile_url = models.URLField(blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["username", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["external_id"],
                condition=Q(external_id__gt=""),
                name="catalogue_author_external_id_unique",
            )
        ]

    def __str__(self) -> str:
        return self.username


class ForumThread(models.Model):
    """A source discussion from which one or more routes may be discovered."""

    external_id = models.CharField(max_length=255, blank=True)
    url = models.URLField(unique=True)
    title = models.CharField(max_length=500)
    locality = models.CharField(max_length=255, blank=True)
    discovered_at = models.DateTimeField(default=timezone.now)
    last_crawled_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["title", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["external_id"],
                condition=Q(external_id__gt=""),
                name="catalogue_thread_external_id_unique",
            )
        ]

    def __str__(self) -> str:
        return self.title


class ForumPost(models.Model):
    """A stable, linkable forum post and its author/date provenance."""

    thread = models.ForeignKey(ForumThread, on_delete=models.PROTECT, related_name="posts")
    author = models.ForeignKey(
        ForumAuthor, on_delete=models.PROTECT, related_name="posts", blank=True, null=True
    )
    external_id = models.CharField(max_length=255, blank=True)
    url = models.URLField(unique=True)
    posted_at = models.DateTimeField(blank=True, null=True)
    content_hash = models.CharField(max_length=128, blank=True)
    discovered_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["posted_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["thread", "external_id"],
                condition=Q(external_id__gt=""),
                name="catalogue_post_thread_external_id_unique",
            )
        ]

    def __str__(self) -> str:
        return self.url


class Route(models.Model):
    """Canonical route identity, independent from any one source/version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=180, unique=True, default=_default_route_slug)
    lifecycle = models.CharField(
        max_length=20, choices=RouteLifecycle.choices, default=RouteLifecycle.PUBLISHED
    )
    # These four fields are deliberately separate: generated text is never
    # confused with source content or an administrator's override.
    original_source_title = models.CharField(max_length=500, blank=True)
    thread_title = models.CharField(max_length=500, blank=True)
    generated_title = models.CharField(max_length=500, blank=True)
    display_title = models.CharField(max_length=500, blank=True)
    admin_title_override = models.CharField(max_length=500, blank=True)
    title_provenance = models.CharField(
        max_length=20, choices=TitleProvenance.choices, default=TitleProvenance.ROUTE_ID
    )
    current_approved_version = models.ForeignKey(
        "catalogue.RouteVersion",
        on_delete=models.SET_NULL,
        related_name="approved_for_routes",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(blank=True, null=True)
    quarantine_reason = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["display_title", "id"]
        indexes = [
            models.Index(fields=["lifecycle", "updated_at"], name="catalogue_route_lifecycle_idx"),
            models.Index(fields=["slug"], name="catalogue_route_slug_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(lifecycle=RouteLifecycle.SOFT_DELETED, deleted_at__isnull=False)
                | ~Q(lifecycle=RouteLifecycle.SOFT_DELETED),
                name="catalogue_deleted_route_has_timestamp",
            ),
            models.CheckConstraint(
                condition=~Q(lifecycle=RouteLifecycle.QUARANTINED) | Q(quarantine_reason__gt=""),
                name="catalogue_quarantined_route_has_reason",
            ),
        ]

    def __str__(self) -> str:
        return self.effective_title or str(self.id)

    @property
    def is_public(self) -> bool:
        return (
            self.lifecycle == RouteLifecycle.PUBLISHED
            and self.current_approved_version_id is not None
        )

    @property
    def effective_title(self) -> str:
        return self.admin_title_override or self.display_title or self.generated_title

    def clean(self) -> None:
        super().clean()
        if self.current_approved_version_id:
            version = RouteVersion.objects.filter(pk=self.current_approved_version_id).first()
            if version is None or version.source.route_id != self.pk:
                raise ValidationError(
                    {"current_approved_version": "The approved version must belong to this route."}
                )
            if version.technical_status != ProcessingStatus.VALID:
                raise ValidationError(
                    {"current_approved_version": "The approved version must be technically valid."}
                )

    def save(self, *args: object, **kwargs: object) -> None:  # noqa: DJ012
        if self.current_approved_version_id:
            version = RouteVersion.objects.filter(pk=self.current_approved_version_id).first()
            if version is None or version.source.route_id != self.pk:
                raise ValidationError(
                    {"current_approved_version": "The approved version must belong to this route."}
                )
            if version.technical_status != ProcessingStatus.VALID:
                raise ValidationError(
                    {"current_approved_version": "The approved version must be technically valid."}
                )
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    @property
    def versions(self) -> models.QuerySet[RouteVersion]:
        return RouteVersion.objects.filter(source__route=self)

    @property
    def provenance_sources(self) -> models.QuerySet[RouteSource]:
        """All direct and explicitly merged sources exposed by this route."""

        return (
            RouteSource.objects.filter(
                Q(route=self)
                | Q(canonical_route_links__canonical_route=self, canonical_route_links__active=True)
            )
            .distinct()
            .order_by("pk")
        )

    all_sources = provenance_sources


class ImmutableSourceQuerySet(models.QuerySet["RouteSource"]):
    def update(self, **kwargs: object) -> int:
        if "route" in kwargs or "route_id" in kwargs or "mapy_url" in kwargs:
            raise ValidationError("A route source URL and route relationship are immutable.")
        return super().update(**kwargs)


class ImmutableVersionQuerySet(models.QuerySet["RouteVersion"]):
    def delete(self) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Historical route versions cannot be deleted.", set(self))


class RouteSource(models.Model):
    """One immutable Mapy URL with many forum-post provenance relationships."""

    route = models.ForeignKey(Route, on_delete=models.PROTECT, related_name="sources")
    mapy_url = models.URLField(unique=True, max_length=1000)
    posts = models.ManyToManyField(  # type: ignore[var-annotated]
        ForumPost, through="RouteSourcePost", related_name="route_sources"
    )
    source_title = models.CharField(max_length=500, blank=True)
    processing_status = models.CharField(
        max_length=20, choices=ProcessingStatus.choices, default=ProcessingStatus.DISCOVERED
    )
    source_status = models.CharField(
        max_length=20, choices=SourceStatus.choices, default=SourceStatus.UNKNOWN
    )
    discovered_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(blank=True, null=True)
    last_checked_at = models.DateTimeField(blank=True, null=True)
    last_successful_check_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True)
    objects = ImmutableSourceQuerySet.as_manager()

    class Meta:
        ordering = ["-discovered_at", "pk"]
        indexes = [
            models.Index(fields=["processing_status"], name="catalogue_source_process_idx"),
            models.Index(fields=["source_status"], name="catalogue_source_status_idx"),
        ]

    def __str__(self) -> str:
        return self.mapy_url

    def save(self, *args: object, **kwargs: object) -> None:  # noqa: DJ012
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if old.route_id != self.route_id or old.mapy_url != self.mapy_url:
                raise ValidationError("A route source URL and route relationship are immutable.")
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class RouteSourcePost(models.Model):
    """Forum provenance junction; one source can be cited by many posts."""

    source = models.ForeignKey(RouteSource, on_delete=models.CASCADE, related_name="post_links")
    post = models.ForeignKey(ForumPost, on_delete=models.PROTECT, related_name="source_links")
    discovered_at = models.DateTimeField(default=timezone.now)
    is_primary = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "post"], name="catalogue_source_post_unique")
        ]

    def __str__(self) -> str:
        return f"{self.source} from {self.post}"


class RouteVersion(models.Model):
    """Immutable GPX result of processing a source URL.

    Only approval and payload-removal audit metadata are lifecycle updates;
    source identity, validation, and route content never change.
    """

    source = models.ForeignKey(RouteSource, on_delete=models.PROTECT, related_name="versions")
    version_number = models.PositiveIntegerField()
    checksum = models.CharField(max_length=128)
    original_gpx_storage_key = models.CharField(max_length=1000, blank=True)
    payload_removed_at = models.DateTimeField(blank=True, null=True)
    payload_removal_reason = models.TextField(blank=True)
    normalized_geometry = RouteGeometryField(srid=4326, spatial_index=True, blank=True, null=True)
    simplified_geometry = RouteGeometryField(srid=4326, spatial_index=True, blank=True, null=True)
    distance_m = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    ascent_m = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    descent_m = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    loop_status = models.CharField(
        max_length=20, choices=LoopStatus.choices, default=LoopStatus.UNKNOWN
    )
    technical_status = models.CharField(
        max_length=20, choices=ProcessingStatus.choices, default=ProcessingStatus.PROCESSING
    )
    validation_error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    approved_at = models.DateTimeField(blank=True, null=True)
    objects = ImmutableVersionQuerySet.as_manager()

    class Meta:
        ordering = ["source_id", "version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "version_number"], name="catalogue_version_source_number_unique"
            ),
            models.UniqueConstraint(
                fields=["source", "checksum"], name="catalogue_version_source_checksum_unique"
            ),
            models.CheckConstraint(
                condition=Q(version_number__gt=0), name="catalogue_version_positive_number"
            ),
            models.CheckConstraint(
                condition=Q(payload_removed_at__isnull=True) | Q(original_gpx_storage_key=""),
                name="catalogue_removed_payload_has_no_storage_ref",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source.mapy_url} v{self.version_number}"

    @property
    def route(self) -> Route:
        return self.source.route

    @property
    def route_id(self) -> uuid.UUID:
        return self.source.route_id

    # Compatibility alias used by storage/import adapters.
    @property
    def gpx_storage_key(self) -> str:
        return self.original_gpx_storage_key

    @gpx_storage_key.setter
    def gpx_storage_key(self, value: str) -> None:
        self.original_gpx_storage_key = value

    def clean(self) -> None:
        super().clean()
        if self.payload_removed_at and self.original_gpx_storage_key:
            raise ValidationError(
                {
                    "original_gpx_storage_key": (
                        "Removed payloads must not retain a storage reference."
                    )
                }
            )

    def save(self, *args: object, **kwargs: object) -> None:  # noqa: DJ012
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "source_id",
                "version_number",
                "checksum",
                "normalized_geometry",
                "simplified_geometry",
                "distance_m",
                "ascent_m",
                "descent_m",
                "loop_status",
                "technical_status",
                "validation_error",
                "created_at",
            )
            changed = [
                field for field in immutable_fields if getattr(old, field) != getattr(self, field)
            ]
            if changed:
                raise ValidationError(
                    {"version": f"Historical version content is immutable: {', '.join(changed)}."}
                )
            if old.original_gpx_storage_key != self.original_gpx_storage_key:
                if not (
                    old.original_gpx_storage_key
                    and not self.original_gpx_storage_key
                    and self.payload_removed_at
                ):
                    raise ValidationError(
                        {
                            "original_gpx_storage_key": (
                                "Storage references are immutable except removal."
                            )
                        }
                    )
            if old.payload_removed_at:
                if (
                    self.payload_removed_at != old.payload_removed_at
                    or self.payload_removal_reason != old.payload_removal_reason
                    or self.original_gpx_storage_key != old.original_gpx_storage_key
                ):
                    raise ValidationError("Payload removal audit metadata is immutable.")
            elif self.payload_removed_at:
                if not old.original_gpx_storage_key or not self.payload_removal_reason:
                    raise ValidationError(
                        "Payload removal requires an existing payload and an audit reason."
                    )
            if old.approved_at is not None and self.approved_at != old.approved_at:
                raise ValidationError("Approval timestamps are immutable once recorded.")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Historical route versions cannot be deleted.", {self})


class RouteBrowseGeometry(models.Model):
    """A zoom-specific, lossy geometry used by map browsing.

    Browse geometries are derived data.  The immutable route version remains
    the source of truth and can always be regenerated if tolerances change.
    """

    version = models.ForeignKey(
        RouteVersion, on_delete=models.CASCADE, related_name="browse_geometries"
    )
    zoom = models.PositiveSmallIntegerField()
    geometry = RouteGeometryField(srid=4326, spatial_index=True)
    tolerance_m = models.DecimalField(max_digits=10, decimal_places=3)
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["version_id", "zoom"]
        constraints = [
            models.UniqueConstraint(
                fields=["version", "zoom"], name="catalogue_browse_geometry_version_zoom_unique"
            ),
            models.CheckConstraint(
                condition=Q(zoom__gte=0), name="catalogue_browse_geometry_zoom_positive"
            ),
        ]
        indexes = [models.Index(fields=["zoom", "version"], name="cat_browse_geom_zoom_idx")]

    def __str__(self) -> str:
        return f"{self.version} @ z{self.zoom}"


class RouteHeatmapCell(models.Model):
    """A persisted grid cell and its current distinct public-route count."""

    zoom = models.PositiveSmallIntegerField()
    x = models.PositiveIntegerField()
    y = models.PositiveIntegerField()
    boundary = RoutePolygonField(srid=4326, spatial_index=True)
    route_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["zoom", "y", "x"]
        constraints = [
            models.UniqueConstraint(
                fields=["zoom", "x", "y"], name="catalogue_heatmap_cell_coordinates_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["zoom", "route_count"], name="cat_heatmap_cell_count_idx"),
        ]

    def __str__(self) -> str:
        return f"z{self.zoom}/{self.x}/{self.y} ({self.route_count})"


class RouteHeatmapMembership(models.Model):
    """Materialized route/cell crossing relationship used for incremental updates."""

    cell = models.ForeignKey(RouteHeatmapCell, on_delete=models.CASCADE, related_name="memberships")
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="heatmap_memberships")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cell", "route"], name="catalogue_heatmap_membership_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["route", "cell"], name="cat_heatmap_member_route_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.route_id} in cell {self.cell_id}"


class PayloadDeletionRequest(models.Model):
    """Durable work item for removing an original payload from storage.

    The storage key is a snapshot: a retry must never delete a newer payload
    that happens to be written under a different key.  Completion is recorded
    only after the version metadata has been atomically finalized.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        FAILED = "failed", "Failed"
        COMPLETED = "completed", "Completed"

    version = models.OneToOneField(
        RouteVersion,
        on_delete=models.PROTECT,
        related_name="payload_deletion_request",
    )
    storage_key_snapshot = models.CharField(max_length=1000)
    removal_reason = models.TextField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    last_attempt_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["requested_at", "pk"]
        indexes = [models.Index(fields=["status", "requested_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(status="completed", completed_at__isnull=False)
                | ~Q(status="completed"),
                name="catalogue_payload_request_completed_timestamp",
            )
        ]

    def __str__(self) -> str:
        return f"{self.status}: {self.storage_key_snapshot}"


class Category(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class RouteCategory(models.Model):
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="category_links")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="route_links")
    assigned_at = models.DateTimeField(default=timezone.now)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="route_category_assignments",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["route", "category"], name="catalogue_route_category_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.route} / {self.category}"


class SimilarityRelationship(models.Model):
    """Evidence-backed relation between two routes and its moderation state."""

    class RelationshipType(models.TextChoices):
        SUSPECTED_DUPLICATE = "suspected_duplicate", "Suspected duplicate"
        VARIANT = "variant", "Variant"

    route_a = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="similarity_a")
    route_b = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="similarity_b")
    relationship_type = models.CharField(max_length=30, choices=RelationshipType.choices)
    similarity_score = models.DecimalField(max_digits=5, decimal_places=4, blank=True, null=True)
    evidence = models.JSONField(default=dict, blank=True)
    decision_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    decided_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(route_a=models.F("route_b")), name="catalogue_similarity_no_self"
            ),
            models.CheckConstraint(
                condition=Q(similarity_score__isnull=True)
                | (Q(similarity_score__gte=0) & Q(similarity_score__lte=1)),
                name="catalogue_similarity_score_range",
            ),
            models.UniqueConstraint(
                fields=["route_a", "route_b"], name="catalogue_similarity_pair_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.route_a} ↔ {self.route_b}"


class RouteSourceMerge(models.Model):
    """A durable provenance alias created when two route identities are merged.

    ``RouteSource.route`` remains immutable: it is the route identity under
    which the URL was first discovered.  This explicit alias lets a
    moderation action expose that source under the surviving canonical route
    without rewriting historical provenance or violating source identity
    guards.
    """

    canonical_route = models.ForeignKey(
        Route, on_delete=models.PROTECT, related_name="merged_source_links"
    )
    source = models.ForeignKey(
        RouteSource, on_delete=models.PROTECT, related_name="canonical_route_links"
    )
    reason = models.TextField()
    active = models.BooleanField(default=True)
    deactivated_at = models.DateTimeField(blank=True, null=True)
    deactivated_reason = models.TextField(blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="catalogue_source_merges",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["canonical_route", "source"], name="catalogue_source_merge_unique"
            ),
            models.UniqueConstraint(
                fields=["source"],
                condition=Q(active=True),
                name="catalogue_active_source_merge_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source} → {self.canonical_route}"

    def clean(self) -> None:
        super().clean()
        if self.active and self.deactivated_at is not None:
            raise ValidationError("An active source merge cannot have a deactivation timestamp.")
        if not self.active and self.deactivated_at is None:
            raise ValidationError("An inactive source merge requires a deactivation timestamp.")
        if self.canonical_route_id and self.source_id:
            source_route_id = (
                RouteSource.objects.filter(pk=self.source_id)
                .values_list("route_id", flat=True)
                .first()
            )
            if source_route_id == self.canonical_route_id:
                raise ValidationError("A route cannot merge one of its own sources.")


class SourceDenylistEntry(models.Model):
    """Persistent source block preventing automatic re-import after takedown."""

    source_url = models.URLField(unique=True, max_length=1000)
    route_source = models.ForeignKey(
        RouteSource,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="denylist_entries",
    )
    reason = models.CharField(max_length=500)
    active = models.BooleanField(default=True)
    denied_at = models.DateTimeField(default=timezone.now)
    restored_at = models.DateTimeField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-denied_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(active=True) | Q(restored_at__isnull=False),
                name="catalogue_restored_denylist_entry_has_timestamp",
            )
        ]

    def __str__(self) -> str:
        return self.source_url


class ModerationDecision(models.Model):
    class Action(models.TextChoices):
        REVIEW = "review", "Reviewed"
        PUBLISH = "publish", "Published automatically"
        KEEP_BOTH = "keep_both", "Keep both"
        MERGE_SOURCES = "merge_sources", "Merge sources"
        QUARANTINE = "quarantine", "Quarantine"
        RESTORE = "restore", "Restore"
        REMOVE = "remove", "Remove"

    route = models.ForeignKey(Route, on_delete=models.PROTECT, related_name="moderation_decisions")
    version = models.ForeignKey(
        RouteVersion,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name="moderation_decisions",
    )
    action = models.CharField(max_length=30, choices=Action.choices)
    reason = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="catalogue_moderation_decisions",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.action}: {self.route}"


# Explicit aliases make the BikeForum terminology available to callers while
# keeping concise model names for Django admin and query code.
BikeForumAuthor = ForumAuthor
BikeForumThread = ForumThread
BikeForumPost = ForumPost
RouteProvenance = RouteSource
DenylistEntry = SourceDenylistEntry
