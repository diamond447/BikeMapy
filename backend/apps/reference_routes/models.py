"""Immutable, provenance-aware completion reference route records."""

from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from apps.catalogue.fields import RouteCoverageGeometryField, RouteGeometryField

OSM_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
OSM_DISCOVERY_QUERY = """[out:json][timeout:90];
area["ISO3166-1"="CZ"]["admin_level"="2"]->.cz;
relation["type"="route"]["route"="bicycle"]["ref"]
  ["network"~"^(lcn|rcn|ncn)$"](area.cz);
out meta;
>>;
out meta geom;"""
MAX_DISCOVERY_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_OVERPASS_FAILURE_LOG_BYTES = 512 * 1024
_completion_evidence_append = contextvars.ContextVar("completion_evidence_append", default=False)


@contextmanager
def allow_completion_evidence_append() -> Any:
    """Permit one controlled append-only evidence write from the calculator."""

    token = _completion_evidence_append.set(True)
    try:
        yield
    finally:
        _completion_evidence_append.reset(token)


def _is_exact_https_url(value: str, *, hostname: str, path: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname == hostname
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and parsed.path == path
        and not parsed.query
        and not parsed.fragment
    )


def _url_matches_offer_base(value: str, base: str) -> bool:
    parsed = urlsplit(value)
    configured = urlsplit(base)
    return (
        bool(parsed.scheme and parsed.netloc and configured.scheme and configured.netloc)
        and parsed.scheme == configured.scheme
        and parsed.netloc == configured.netloc
        and parsed.path.startswith(configured.path.rstrip("/") + "/")
        and not parsed.query
        and not parsed.fragment
    )


def _verified_discovery_content(metadata: dict[str, Any]) -> dict[str, Any] | None:
    encoded = metadata.get("discovery_artifact_content_base64")
    expected_hash = metadata.get("discovery_result_sha256")
    if not isinstance(encoded, str) or not isinstance(expected_hash, str):
        return None
    if len(encoded) > ((MAX_DISCOVERY_ARTIFACT_BYTES + 2) // 3) * 4:
        return None
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
        if (
            not content
            or len(content) > MAX_DISCOVERY_ARTIFACT_BYTES
            or hashlib.sha256(content).hexdigest() != expected_hash
        ):
            return None
        discovery = json.loads(content)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(discovery, dict)
        or not isinstance(discovery.get("elements"), list)
        or not isinstance(discovery.get("osm3s"), dict)
        or not discovery["osm3s"].get("timestamp_osm_base")
    ):
        return None
    try:
        source_timestamp = datetime.fromisoformat(
            str(discovery["osm3s"]["timestamp_osm_base"]).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    if source_timestamp.tzinfo is None or source_timestamp.utcoffset() != timedelta(0):
        return None
    return discovery


def _valid_overpass_failure_evidence(
    value: Any,
    *,
    discovery_executed_at: datetime | None = None,
    retrieved_at: datetime | None = None,
) -> bool:
    def reject() -> bool:
        return False

    if not isinstance(value, dict):
        return reject()
    if (
        value.get("endpoint") != OSM_OVERPASS_ENDPOINT
        or value.get("query_text") != OSM_DISCOVERY_QUERY
    ):
        return reject()
    if not isinstance(value.get("attempted_at"), str) or not value["attempted_at"].strip():
        return reject()
    try:
        attempted_at = datetime.fromisoformat(value["attempted_at"].replace("Z", "+00:00"))
    except ValueError:
        return reject()
    if attempted_at.tzinfo is None or attempted_at.utcoffset() != timedelta(0):
        return reject()
    now = timezone.now()
    if attempted_at > now:
        return reject()
    if discovery_executed_at is not None and attempted_at > discovery_executed_at:
        return reject()
    if retrieved_at is not None and attempted_at > retrieved_at:
        return reject()
    if value.get("network_status") not in {"http_error", "network_error"}:
        return reject()
    if value["network_status"] == "http_error" and (
        isinstance(value.get("http_status"), bool)
        or not isinstance(value.get("http_status"), int)
        or not 400 <= value["http_status"] < 600
    ):
        return reject()
    if value["network_status"] == "network_error" and value.get("http_status") is not None:
        return reject()
    if not isinstance(value.get("error"), str) or not value["error"].strip():
        return reject()
    encoded, expected_hash = value.get("log_base64"), value.get("log_sha256")
    if not isinstance(encoded, str) or not isinstance(expected_hash, str):
        return reject()
    if len(encoded) > ((MAX_OVERPASS_FAILURE_LOG_BYTES + 2) // 3) * 4:
        return reject()
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeDecodeError, ValueError):
        return reject()
    if (
        not content
        or len(content) > MAX_OVERPASS_FAILURE_LOG_BYTES
        or hashlib.sha256(content).hexdigest() != expected_hash
    ):
        return reject()
    try:
        log_payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return reject()
    expected_payload = {
        "endpoint": value["endpoint"],
        "query_text": value["query_text"],
        "attempted_at": value["attempted_at"],
        "network_status": value["network_status"],
        "http_status": value.get("http_status"),
        "error": value["error"],
    }
    valid = log_payload == expected_payload
    return bool(valid)


def has_deployable_derivative_offer(
    collection: ReferenceCollection, source_import: Any | None = None
) -> bool:
    configured = str(getattr(settings, "REFERENCE_ROUTE_DERIVATIVE_OFFER_URL", "") or "")
    if not configured or not _url_matches_offer_base(collection.derivative_offer_url, configured):
        return False
    if source_import is None:
        return True
    metadata = source_import.response_metadata or {}
    if metadata.get("import_mode") != "production" or metadata.get("production_import") is not True:
        return False
    if metadata.get("validation_sample") is True:
        return False
    if not all(
        metadata.get(field)
        for field in (
            "discovery_mechanism",
            "discovery_query_or_extract_id",
            "discovery_executed_at",
            "discovery_result_sha256",
            "discovery_artifact_url",
            "discovery_selected_relation_ids",
            "discovery_artifact_content_base64",
        )
    ):
        return False
    discovery_payload = _verified_discovery_content(metadata)
    if discovery_payload is None:
        return False
    executed_at = metadata.get("discovery_executed_at")
    if not isinstance(executed_at, str):
        return False
    try:
        parsed_executed_at = datetime.fromisoformat(executed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed_executed_at.tzinfo is None or parsed_executed_at.utcoffset() != timedelta(0):
        return False
    retrieved_at = source_import.retrieved_at
    if (
        not timezone.is_aware(retrieved_at)
        or retrieved_at.utcoffset() != timedelta(0)
        or retrieved_at > timezone.now()
        or parsed_executed_at > retrieved_at
    ):
        return False
    expected_ids = metadata.get("expected_relation_ids")
    discovery_ids = metadata.get("discovery_selected_relation_ids")
    if (
        not isinstance(expected_ids, list)
        or not isinstance(discovery_ids, list)
        or any(not isinstance(value, int) or isinstance(value, bool) for value in expected_ids)
        or any(not isinstance(value, int) or isinstance(value, bool) for value in discovery_ids)
        or sorted(expected_ids) != sorted(discovery_ids)
    ):
        return False
    artifact = metadata.get("discovery_artifact_url")
    artifact_parts = urlsplit(str(artifact))
    configured_parts = urlsplit(configured)
    source_parts = urlsplit(str(source_import.endpoint))
    if (
        artifact_parts.scheme != "https"
        or not artifact_parts.netloc
        or artifact_parts.query
        or artifact_parts.fragment
        or artifact_parts.scheme != configured_parts.scheme
        or artifact_parts.netloc != configured_parts.netloc
        or not artifact_parts.path.startswith(configured_parts.path.rstrip("/") + "/")
        or not any(
            marker in artifact_parts.path.split("/") for marker in ("discovery", "artifacts")
        )
        or not artifact_parts.path.casefold().endswith(".json")
        or (
            artifact_parts.scheme == source_parts.scheme
            and artifact_parts.netloc == source_parts.netloc
            and artifact_parts.path == source_parts.path
        )
    ):
        return False
    mechanism = metadata.get("discovery_mechanism")
    query_id = str(metadata.get("discovery_query_or_extract_id", ""))
    if mechanism == "overpass" and query_id != (
        f"overpass:{hashlib.sha256(OSM_DISCOVERY_QUERY.encode()).hexdigest()}"
    ):
        return False
    if mechanism == "regional_extract" and (
        not query_id.startswith("regional-extract:")
        or metadata.get("overpass_unavailable") is not True
        or not _valid_overpass_failure_evidence(
            metadata.get("overpass_failure_evidence"),
            discovery_executed_at=parsed_executed_at,
            retrieved_at=source_import.retrieved_at,
        )
    ):
        return False
    elements = discovery_payload.get("elements")
    if not isinstance(elements, list):
        return False
    discovered_records = [
        item for item in elements if isinstance(item, dict) and item.get("type") == "relation"
    ]
    discovered_ids = [item.get("id") for item in discovered_records]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in discovered_ids):
        return False
    if len(discovered_ids) != len(set(discovered_ids)):
        return False
    if not set(expected_ids).issubset(discovered_ids):
        return False
    raw_elements = (
        source_import.raw_payload.get("elements", [])
        if isinstance(source_import.raw_payload, dict)
        else []
    )
    source_records = [
        item for item in raw_elements if isinstance(item, dict) and item.get("type") == "relation"
    ]
    source_ids = [item.get("id") for item in source_records]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in source_ids):
        return False
    if len(source_ids) != len(set(source_ids)):
        return False
    source_relations = {item["id"]: item for item in source_records}
    for route_id in expected_ids:
        discovered_relation = next(
            (item for item in discovered_records if item.get("id") == route_id), None
        )
        if (
            not isinstance(discovered_relation, dict)
            or route_id not in source_relations
            or discovered_relation.get("tags") != source_relations[route_id].get("tags")
            or any(
                discovered_relation.get(field) in (None, "")
                for field in ("version", "timestamp", "changeset")
            )
            or any(
                discovered_relation.get(field) != source_relations[route_id].get(field)
                for field in ("version", "timestamp", "changeset")
            )
        ):
            return False
    offer = getattr(source_import, "alteration_offer", None)
    return bool(offer and offer.is_complete_for(source_import, configured))


def has_publishable_reference_source(
    collection: ReferenceCollection, source_import: Any | None = None
) -> bool:
    """Apply the source-specific publication gate shared by route/version saves."""

    if collection.source_kind == ReferenceSourceKind.OSM_NUMBERED:
        return bool(
            collection.permission_granted
            and has_deployable_derivative_offer(collection, source_import)
        )
    if collection.source_kind == ReferenceSourceKind.VIA_CZECHIA:
        return bool(
            getattr(settings, "REFERENCE_ROUTE_VIA_CZECHIA_ENABLED", False)
            and collection.permission_granted
            and collection.active
            and source_import is not None
            and source_import.response_metadata.get("source_available") is True
        )
    return False


class ReferenceSourceKind(models.TextChoices):
    OSM_NUMBERED = "osm_numbered", "OpenStreetMap numbered cycling routes"
    VIA_CZECHIA = "via_czechia", "Via Czechia"


class ReferenceImportStatus(models.TextChoices):
    DISCOVERED = "discovered", "Discovered"
    VALID = "valid", "Valid"
    INVALID = "invalid", "Invalid"
    BLOCKED = "blocked", "Blocked"
    FAILED = "failed", "Failed"


class ImmutableReferenceImportQuerySet(models.QuerySet["ReferenceImport"]):
    def update(self, **kwargs: object) -> int:
        if set(kwargs) - {"status", "diagnostics"}:
            raise ValidationError("Immutable source snapshot fields cannot be updated.")
        return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Source snapshots cannot be deleted.", set(self))


class ReferenceValidationStatus(models.TextChoices):
    PENDING_REVIEW = "pending_review", "Pending human review"
    VALID = "valid", "Valid"
    INVALID = "invalid", "Invalid"


class ReferencePublicationStatus(models.TextChoices):
    PENDING = "pending", "Pending publication review"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class ReferenceCollection(models.Model):
    """A bounded catalogue with its own source identity and attribution."""

    slug = models.SlugField(max_length=120, unique=True)
    name = models.CharField(max_length=255)
    source_kind = models.CharField(max_length=32, choices=ReferenceSourceKind.choices)
    source_url = models.URLField(max_length=1000)
    attribution = models.TextField()
    licence = models.CharField(max_length=255)
    attribution_text = models.TextField(blank=True)
    attribution_url = models.URLField(max_length=1000, blank=True)
    licence_uri = models.URLField(max_length=1000, blank=True)
    derivative_offer_url = models.URLField(max_length=1000, blank=True)
    rightsholder = models.CharField(max_length=255, blank=True)
    contact_url = models.URLField(max_length=1000, blank=True)
    permission_granted = models.BooleanField(default=False)
    active = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "pk"]
        indexes = [models.Index(fields=["source_kind", "active"], name="ref_coll_kind_active_idx")]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        required = (
            "attribution_text",
            "attribution_url",
            "licence_uri",
            "derivative_offer_url",
            "rightsholder",
            "contact_url",
        )
        missing = [field for field in required if not getattr(self, field)]
        if self.source_kind == ReferenceSourceKind.OSM_NUMBERED and missing:
            raise ValidationError(
                {field: "ODbL provenance metadata is required." for field in missing}
            )
        if self.source_kind == ReferenceSourceKind.OSM_NUMBERED:
            attribution = f"{self.attribution_text} {self.attribution}"
            if (
                "openstreetmap" not in attribution.casefold()
                or "contributor" not in attribution.casefold()
            ):
                raise ValidationError(
                    {"attribution_text": "OSM contributor attribution is required."}
                )
            if not _is_exact_https_url(self.source_url, hostname="www.openstreetmap.org", path=""):
                raise ValidationError({"source_url": "Use the exact OSM source URL."})
            if not _is_exact_https_url(
                self.attribution_url, hostname="www.openstreetmap.org", path="/copyright"
            ):
                raise ValidationError({"attribution_url": "Use the OSM copyright URL."})
            if not _is_exact_https_url(
                self.licence_uri,
                hostname="opendatacommons.org",
                path="/licenses/odbl/1-0/",
            ):
                raise ValidationError({"licence_uri": "Use the ODbL licence URI."})
            if not _is_exact_https_url(
                self.derivative_offer_url,
                hostname="github.com",
                path="/diamond447/BikeMapy/blob/main/docs/osm-alterations.md",
            ):
                raise ValidationError(
                    {"derivative_offer_url": "Use the tracked OSM alteration offer document."}
                )
            if not _is_exact_https_url(
                self.contact_url, hostname="www.openstreetmap.org", path="/fixthemap"
            ):
                raise ValidationError({"contact_url": "Use the OSM contact URL."})


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
    raw_response = models.BinaryField(default=bytes)
    raw_response_sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(
        max_length=16,
        choices=ReferenceImportStatus.choices,
        default=ReferenceImportStatus.DISCOVERED,
    )
    diagnostics = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    objects = ImmutableReferenceImportQuerySet.as_manager()

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

    def save(self, *args: object, **kwargs: object) -> None:  # noqa: DJ012
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            immutable = (
                "collection_id",
                "checksum",
                "endpoint",
                "query_text",
                "retrieved_at",
                "source_timestamp",
                "response_metadata",
                "raw_payload",
                "raw_response",
                "raw_response_sha256",
                "created_at",
            )
            changed = [field for field in immutable if getattr(old, field) != getattr(self, field)]
            if changed:
                raise ValidationError(
                    {"import": f"Source snapshots are immutable: {', '.join(changed)}."}
                )
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Source snapshots cannot be deleted.", {self})


class ImmutableReferenceAlterationOfferQuerySet(models.QuerySet["ReferenceAlterationOffer"]):
    def update(self, **kwargs: object) -> int:
        raise ValidationError("Published alteration offers are immutable.")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Published alteration offers cannot be deleted.", set(self))


class ReferenceAlterationOffer(models.Model):
    """Immutable public evidence for reconstructing one exact source snapshot."""

    source_import = models.OneToOneField(
        ReferenceImport, on_delete=models.PROTECT, related_name="alteration_offer"
    )
    manifest_url = models.URLField(max_length=1000)
    artifact_url = models.URLField(max_length=1000)
    method_url = models.URLField(max_length=1000)
    published_at = models.DateTimeField()
    offered_snapshot_hash = models.CharField(max_length=64)
    operator_evidence = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    objects = ImmutableReferenceAlterationOfferQuerySet.as_manager()

    def __str__(self) -> str:
        return f"alteration-offer:{self.source_import_id}"

    def is_complete_for(self, source_import: ReferenceImport, allowed_base: str) -> bool:
        return (
            self.source_import_id == source_import.pk
            and self.offered_snapshot_hash == source_import.raw_response_sha256
            and all(
                _url_matches_offer_base(value, allowed_base)
                for value in (self.manifest_url, self.artifact_url, self.method_url)
            )
            and timezone.is_aware(self.published_at)
            and self.published_at.utcoffset() == timedelta(0)
            and bool(self.operator_evidence.strip())
        )

    def clean(self) -> None:
        super().clean()
        allowed_base = str(getattr(settings, "REFERENCE_ROUTE_DERIVATIVE_OFFER_URL", "") or "")
        if (
            not allowed_base
            or self.source_import.response_metadata.get("import_mode") != "production"
            or not self.is_complete_for(self.source_import, allowed_base)
        ):
            raise ValidationError("Alteration offer evidence is incomplete or hash-mismatched.")

    def save(self, *args: object, **kwargs: object) -> None:  # noqa: DJ012
        if self.pk:
            raise ValidationError("Published alteration offers are immutable.")
        self.full_clean()
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Published alteration offers cannot be deleted.", {self})


class ProtectedReferenceRouteQuerySet(models.QuerySet["ReferenceRoute"]):
    def update(self, **kwargs: object) -> int:
        raise ValidationError("Reference routes must be changed through source-aware services.")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Reference routes cannot be deleted.", set(self))


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
    publication_status = models.CharField(
        max_length=16,
        choices=ReferencePublicationStatus.choices,
        default=ReferencePublicationStatus.PENDING,
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    reviewed_by = models.CharField(max_length=255, blank=True)
    current_version = models.ForeignKey(
        "ReferenceRouteVersion",
        on_delete=models.SET_NULL,
        related_name="current_for_routes",
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    objects = ProtectedReferenceRouteQuerySet.as_manager()

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

    def save(self, *args: object, **kwargs: object) -> None:  # noqa: DJ012
        if self.active or self.publication_status == ReferencePublicationStatus.APPROVED:
            collection = ReferenceCollection.objects.get(pk=self.collection_id)
            source_import = None
            if self.current_version_id:
                current_version = self.current_version
                if current_version is not None:
                    source_import = current_version.source_import
            if not has_publishable_reference_source(collection, source_import):
                raise ValidationError(
                    "A blocked reference source or incomplete alteration offer "
                    "cannot be published or activated."
                )
        super().save(*args, **kwargs)  # type: ignore[arg-type]


class ImmutableReferenceVersionQuerySet(models.QuerySet["ReferenceRouteVersion"]):
    def update(self, **kwargs: object) -> int:
        raise ValidationError("Reference versions must be changed through source-aware services.")

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
    normalized_geometry = RouteGeometryField(srid=4326, spatial_index=True, blank=True, null=True)
    provenance = models.JSONField(default=dict)
    attribution_metadata = models.JSONField(default=dict)
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
        if self.active:
            collection = ReferenceCollection.objects.get(pk=self.route.collection_id)
            if not has_publishable_reference_source(collection, self.source_import):
                raise ValidationError(
                    "A blocked reference source or incomplete alteration offer "
                    "cannot activate a version."
                )
        if self.validation_status == ReferenceValidationStatus.VALID or self.active:
            required_attribution = (
                "attribution_text",
                "attribution_url",
                "licence",
                "licence_uri",
                "derivative_offer_url",
                "rightsholder",
                "contact_url",
                "source_url",
            )
            missing_attribution = [
                field for field in required_attribution if not self.attribution_metadata.get(field)
            ]
            if missing_attribution:
                raise ValidationError(
                    {"attribution_metadata": "Structured attribution metadata is incomplete."}
                )
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
                "attribution_metadata",
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


class CompletionSubject(models.TextChoices):
    PLAYER = "player", "Player"
    COMPETITION = "competition", "Competition"


class CompletionStatus(models.TextChoices):
    FRESH = "fresh", "Fresh"
    PENDING = "pending", "Pending"
    FAILED = "failed", "Failed"


class RouteCompletion(models.Model):
    """A version-pinned, spatially unioned completion projection."""

    route_version = models.ForeignKey(
        ReferenceRouteVersion, on_delete=models.PROTECT, related_name="completion_results"
    )
    subject_type = models.CharField(max_length=16, choices=CompletionSubject.choices)
    player = models.ForeignKey(
        "accounts.Player",
        on_delete=models.CASCADE,
        related_name="route_completions",
        blank=True,
        null=True,
    )
    competition = models.ForeignKey(
        "accounts.Competition",
        on_delete=models.CASCADE,
        related_name="route_completions",
        blank=True,
        null=True,
    )
    status = models.CharField(
        max_length=16, choices=CompletionStatus.choices, default=CompletionStatus.PENDING
    )
    tolerance_meters = models.DecimalField(max_digits=8, decimal_places=3, default=50)
    total_length_meters = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    covered_length_meters = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    completion_percent = models.DecimalField(max_digits=7, decimal_places=3, default=0)
    calculated_at = models.DateTimeField(blank=True, null=True)
    requested_at = models.DateTimeField(default=timezone.now)
    algorithm_version = models.CharField(max_length=32, default="corridor-v1")
    route_checksum = models.CharField(max_length=64, blank=True)
    membership_revision = models.PositiveBigIntegerField(blank=True, null=True)
    evidence_digest = models.CharField(max_length=64, blank=True)
    evidence_generation = models.UUIDField(default=uuid.uuid4, editable=False)
    error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("route_version", "player"),
                condition=Q(player__isnull=False),
                name="completion_route_player_unique",
            ),
            models.UniqueConstraint(
                fields=("route_version", "competition"),
                condition=Q(competition__isnull=False),
                name="completion_route_competition_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        subject_type=CompletionSubject.PLAYER,
                        player__isnull=False,
                        competition__isnull=True,
                    )
                    | Q(
                        subject_type=CompletionSubject.COMPETITION,
                        player__isnull=True,
                        competition__isnull=False,
                    )
                ),
                name="completion_subject_matches_type",
            ),
        ]
        indexes = [
            models.Index(fields=("status", "requested_at"), name="completion_status_req_idx"),
            models.Index(
                fields=("route_version", "subject_type"), name="completion_version_subject_idx"
            ),
        ]

    def __str__(self) -> str:
        subject = self.player_id or self.competition_id
        return f"completion:{self.route_version_id}:{self.subject_type}:{subject}"


class ImmutableCompletionEvidenceQuerySet(models.QuerySet["RouteCompletionEvidence"]):
    def update(self, **kwargs: object) -> int:
        raise ValidationError("Completion evidence is immutable and append-only.")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Completion evidence is immutable and append-only.", set(self))

    def bulk_update(self, objs: Any, fields: Any, batch_size: int | None = None) -> int:
        raise ValidationError("Completion evidence is immutable and append-only.")

    def bulk_create(
        self,
        objs: Iterable[RouteCompletionEvidence],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Iterable[str] | None = None,
        unique_fields: Iterable[str] | None = None,
    ) -> list[RouteCompletionEvidence]:
        del objs, batch_size, ignore_conflicts, update_conflicts, update_fields, unique_fields
        raise ValidationError("Use the controlled evidence append service.")


class RouteCompletionEvidence(models.Model):
    """Traceable contribution of one imported activity to one projection."""

    completion = models.ForeignKey(
        RouteCompletion, on_delete=models.CASCADE, related_name="evidence"
    )
    activity = models.ForeignKey(
        "accounts.ImportedActivity",
        on_delete=models.SET_NULL,
        related_name="route_completion_evidence",
        blank=True,
        null=True,
    )
    provider_activity_id = models.CharField(max_length=80)
    activity_player_id = models.PositiveBigIntegerField()
    evidence_generation = models.UUIDField(default=uuid.uuid4, editable=False)
    covered_length_meters = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    covered_geometry = RouteCoverageGeometryField(blank=True, null=True)
    activity_geometry_hash = models.CharField(max_length=64, blank=True)
    membership_revision = models.PositiveBigIntegerField(blank=True, null=True)
    evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    objects = ImmutableCompletionEvidenceQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("completion", "evidence_generation", "provider_activity_id"),
                name="completion_evidence_activity_unique",
            )
        ]

    def __str__(self) -> str:
        return f"{self.completion_id}:{self.provider_activity_id}"

    def save(self, *args: object, **kwargs: object) -> None:  # noqa: DJ012
        if self.pk or not _completion_evidence_append.get():
            raise ValidationError("Completion evidence is immutable and append-only.")
        super().save(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise ProtectedError("Completion evidence is immutable and append-only.", {self})


class RouteCompletionMonthly(models.Model):
    """Unique newly covered distance for a subject and calendar month."""

    route_version = models.ForeignKey(
        ReferenceRouteVersion, on_delete=models.PROTECT, related_name="monthly_completions"
    )
    subject_type = models.CharField(max_length=16, choices=CompletionSubject.choices)
    player = models.ForeignKey(
        "accounts.Player",
        on_delete=models.CASCADE,
        related_name="monthly_route_completions",
        blank=True,
        null=True,
    )
    competition = models.ForeignKey(
        "accounts.Competition",
        on_delete=models.CASCADE,
        related_name="monthly_route_completions",
        blank=True,
        null=True,
    )
    month = models.DateField()
    covered_length_meters = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    covered_geometry = RouteGeometryField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("route_version", "player", "month"),
                condition=Q(player__isnull=False),
                name="completion_month_player_unique",
            ),
            models.UniqueConstraint(
                fields=("route_version", "competition", "month"),
                condition=Q(competition__isnull=False),
                name="completion_month_competition_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"monthly:{self.route_version_id}:{self.month}:{self.subject_type}"


class RouteCompletionJob(models.Model):
    """Idempotent durable queue row for player/competition calculations."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    idempotency_key = models.CharField(max_length=255, unique=True)
    route_version = models.ForeignKey(
        ReferenceRouteVersion, on_delete=models.PROTECT, related_name="completion_jobs"
    )
    subject_type = models.CharField(max_length=16, choices=CompletionSubject.choices)
    player = models.ForeignKey("accounts.Player", on_delete=models.CASCADE, blank=True, null=True)
    competition = models.ForeignKey(
        "accounts.Competition", on_delete=models.CASCADE, blank=True, null=True
    )
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_token = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(blank=True, null=True)
    dispatch_token = models.CharField(max_length=64, blank=True)
    dispatch_lease_until = models.DateTimeField(blank=True, null=True)
    dispatched_at = models.DateTimeField(blank=True, null=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=("status", "next_attempt_at"), name="completion_job_due_idx")
        ]

    def __str__(self) -> str:
        return f"completion-job:{self.idempotency_key}"
