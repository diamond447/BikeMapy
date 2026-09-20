"""Conservative, deterministic import and validation for approved route data."""

# mypy: disable-error-code="no-redef,return-value,no-any-return,union-attr,arg-type"

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from .models import (
    ReferenceCollection,
    ReferenceImport,
    ReferenceImportStatus,
    ReferenceRecomputation,
    ReferenceRoute,
    ReferenceRouteVersion,
    ReferenceSourceKind,
    ReferenceValidationStatus,
)

OSM_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
OSM_DISCOVERY_QUERY = """[out:json][timeout:90];
area["ISO3166-1"="CZ"]["admin_level"="2"]->.cz;
relation["type"="route"]["route"="bicycle"]["ref"]
  ["network"~"^(lcn|rcn|ncn)$"](area.cz);
out meta;
>>;
out meta geom;"""
_REF_RE = re.compile(r"^[0-9]{1,4}$")
_INTERNATIONAL_REF_RE = re.compile(r"^(?:ev|eurovelo)[ -]?[0-9]{1,2}$")
_MARKERS = {"eurovelo", "international", "ecf"}


@dataclass(frozen=True)
class RouteDiagnostic:
    code: str
    message: str
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details or {}}


def normalize_source_text(value: Any) -> str:
    """Normalize Unicode text before allow/deny checks."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = " ".join(text.split())
    return "".join("-" if unicodedata.category(char) == "Pd" else char for char in text)


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", value) if token}


def _has_international_marker(value: str) -> bool:
    return any(
        token in _MARKERS or re.fullmatch(r"eurovelo[0-9]{1,2}", token) for token in _tokens(value)
    )


def validate_osm_candidate(tags: dict[str, Any]) -> list[RouteDiagnostic]:
    network = normalize_source_text(tags.get("network"))
    ref = normalize_source_text(tags.get("ref"))
    fields = [
        normalize_source_text(tags.get(name)) for name in ("network", "ref", "name", "operator")
    ]
    diagnostics: list[RouteDiagnostic] = []
    if network not in {"lcn", "rcn", "ncn"}:
        diagnostics.append(
            RouteDiagnostic("unsupported_network", f"Unsupported network: {network or '<empty>'}")
        )
    if _INTERNATIONAL_REF_RE.fullmatch(ref):
        diagnostics.append(RouteDiagnostic("international_ref", f"International reference: {ref}"))
    if not _REF_RE.fullmatch(ref):
        diagnostics.append(
            RouteDiagnostic("invalid_reference", "Reference must be one to four digits.")
        )
    if any(_has_international_marker(field) for field in fields):
        diagnostics.append(
            RouteDiagnostic("international_marker", "International marker found in route tags.")
        )
    return diagnostics


def _relation_elements(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise ValueError("OSM response must contain an elements array")
    relations = [
        element
        for element in elements
        if element.get("type") == "relation" and isinstance(element.get("id"), int)
    ]
    ways = {
        int(element["id"]): element
        for element in elements
        if element.get("type") == "way" and isinstance(element.get("id"), int)
    }
    return relations, ways


def _relation_geometry(
    relation: dict[str, Any], ways: dict[int, dict[str, Any]]
) -> tuple[dict[str, Any], list[RouteDiagnostic], dict[str, Any]]:
    diagnostics: list[RouteDiagnostic] = []
    lines: list[list[list[float]]] = []
    member_ids: list[int] = []
    for member in relation.get("members", []):
        if member.get("type") != "way":
            continue
        try:
            way_id = int(member["ref"])
        except (KeyError, TypeError, ValueError):
            diagnostics.append(
                RouteDiagnostic("invalid_member", "Route has a malformed way member.")
            )
            continue
        member_ids.append(way_id)
        geometry = ways.get(way_id, {}).get("geometry")
        if not isinstance(geometry, list) or len(geometry) < 2:
            diagnostics.append(
                RouteDiagnostic("missing_geometry", f"Way {way_id} has no usable geometry.")
            )
            continue
        line = [
            [float(point["lon"]), float(point["lat"])]
            for point in geometry
            if "lon" in point and "lat" in point
        ]
        if len(line) < 2:
            diagnostics.append(
                RouteDiagnostic("invalid_geometry", f"Way {way_id} has fewer than two coordinates.")
            )
        else:
            lines.append(line)
    if not lines:
        diagnostics.append(
            RouteDiagnostic("empty_geometry", "Route contains no usable way geometry.")
        )
        return {"type": "MultiLineString", "coordinates": []}, diagnostics, {"way_ids": member_ids}
    for previous, current in zip(lines, lines[1:], strict=False):
        if previous[-1] not in (current[0], current[-1]):
            diagnostics.append(
                RouteDiagnostic(
                    "disconnected_geometry", "Consecutive route members are disconnected."
                )
            )
            break
    geometry: dict[str, Any] = (
        {"type": "LineString", "coordinates": lines[0]}
        if len(lines) == 1
        else {"type": "MultiLineString", "coordinates": lines}
    )
    return geometry, diagnostics, {"way_ids": member_ids}


def _canonical_checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _geometry_equal(left: Any, right: Any) -> bool:
    """Compare JSON/text fallback values consistently on SQLite and PostGIS."""

    def decode(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    return decode(left) == decode(right)


def _source_version(relation: dict[str, Any]) -> str:
    return "osm:{id}:v{version}:{timestamp}:{changeset}".format(
        id=relation.get("id", "unknown"),
        version=relation.get("version", "unknown"),
        timestamp=relation.get("timestamp", "unknown"),
        changeset=relation.get("changeset", "unknown"),
    )


def import_osm_snapshot(
    *,
    collection: ReferenceCollection,
    payload: dict[str, Any],
    endpoint: str = OSM_OVERPASS_ENDPOINT,
    query_text: str = OSM_DISCOVERY_QUERY,
    retrieved_at: datetime | None = None,
    response_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Import one bounded Overpass snapshot and return an auditable summary."""

    if collection.source_kind != ReferenceSourceKind.OSM_NUMBERED:
        raise ValueError("Only the approved OSM numbered-route source can be imported")
    checksum = _canonical_checksum(payload)
    retrieved = retrieved_at or timezone.now()
    with transaction.atomic():
        source_import, created = ReferenceImport.objects.get_or_create(
            collection=collection,
            checksum=checksum,
            defaults={
                "endpoint": endpoint,
                "query_text": query_text,
                "retrieved_at": retrieved,
                "response_metadata": response_metadata or {},
                "raw_payload": payload,
            },
        )
        if not created:
            return {
                "status": "unchanged",
                "import_id": source_import.pk,
                "created": 0,
                "invalid": 0,
                "recomputations": 0,
            }
        try:
            relations, ways = _relation_elements(payload)
        except ValueError as exc:
            source_import.status = ReferenceImportStatus.FAILED
            source_import.diagnostics = [RouteDiagnostic("malformed_payload", str(exc)).as_dict()]
            source_import.save(update_fields=["status", "diagnostics"])
            return {
                "status": "failed",
                "import_id": source_import.pk,
                "created": 0,
                "invalid": 0,
                "recomputations": 0,
            }
        relation_by_id = {element["id"]: element for element in relations}
        route_objects: dict[int, ReferenceRoute] = {}
        for relation in relations:
            tags = relation.get("tags") if isinstance(relation.get("tags"), dict) else {}
            source_id = f"osm-relation:{relation['id']}"
            route, _ = ReferenceRoute.objects.get_or_create(
                collection=collection,
                source_identifier=source_id,
                defaults={
                    "route_number": normalize_source_text(tags.get("ref")),
                    "title": str(tags.get("name") or tags.get("ref") or source_id),
                    "operator": str(tags.get("operator") or ""),
                    "network": normalize_source_text(tags.get("network")),
                    "source_tags": tags,
                },
            )
            route_objects[relation["id"]] = route
        for relation in relations:
            route = route_objects[relation["id"]]
            parent_id = next(
                (
                    member.get("ref")
                    for member in relation.get("members", [])
                    if member.get("type") == "relation" and member.get("ref") in relation_by_id
                ),
                None,
            )
            if parent_id in route_objects and route.parent_id != route_objects[parent_id].pk:
                route.parent = route_objects[parent_id]
                route.route_type = ReferenceRoute.RouteType.STAGE
                route.save(update_fields=["parent", "route_type", "updated_at"])
        imported = invalid = recomputations = 0
        import_diagnostics: list[dict[str, Any]] = []
        for relation in relations:
            route = route_objects[relation["id"]]
            tags = relation.get("tags") if isinstance(relation.get("tags"), dict) else {}
            diagnostics = validate_osm_candidate(tags)
            normalized, geometry_diagnostics, member_meta = _relation_geometry(relation, ways)
            diagnostics.extend(geometry_diagnostics)
            diagnostic_dicts = [diagnostic.as_dict() for diagnostic in diagnostics]
            status = (
                ReferenceValidationStatus.VALID
                if not diagnostics
                else ReferenceValidationStatus.INVALID
            )
            source_geometry = {
                "relation": relation,
                "members": member_meta,
                "ways": {
                    str(way_id): ways[way_id] for way_id in member_meta["way_ids"] if way_id in ways
                },
            }
            version_checksum = hashlib.sha256(
                json.dumps(source_geometry, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if ReferenceRouteVersion.objects.filter(
                route=route, checksum=version_checksum
            ).exists():
                continue
            version_number = (
                ReferenceRouteVersion.objects.filter(route=route)
                .order_by("-version_number")
                .values_list("version_number", flat=True)
                .first()
                or 0
            ) + 1
            version = ReferenceRouteVersion.objects.create(
                route=route,
                source_import=source_import,
                version_number=version_number,
                source_version_identifier=_source_version(relation),
                checksum=version_checksum,
                source_geometry=source_geometry,
                normalized_geometry=normalized,
                provenance={
                    "source": "OpenStreetMap",
                    "relation_id": relation["id"],
                    "endpoint": endpoint,
                    "retrieved_at": retrieved.isoformat(),
                    "query_sha256": hashlib.sha256(query_text.encode()).hexdigest(),
                    "member_way_ids": member_meta["way_ids"],
                },
                attribution=collection.attribution,
                validation_status=status,
                diagnostics=diagnostic_dicts,
                active=status == ReferenceValidationStatus.VALID,
            )
            imported += 1
            if diagnostics:
                invalid += 1
                import_diagnostics.append(
                    {"source_identifier": route.source_identifier, "diagnostics": diagnostic_dicts}
                )
                continue
            old = route.current_version
            if old is not None:
                ReferenceRouteVersion.objects.filter(route=route).exclude(pk=version.pk).update(
                    active=False
                )
            route.current_version = version
            route.active = True
            route.save(update_fields=["current_version", "active", "updated_at"])
            if old is not None and not _geometry_equal(
                old.normalized_geometry, version.normalized_geometry
            ):
                ReferenceRecomputation.objects.create(
                    route_version=version,
                    reason="reference route geometry changed",
                    scheduled_at=timezone.now(),
                )
                recomputations += 1
        source_import.status = (
            ReferenceImportStatus.INVALID if invalid else ReferenceImportStatus.VALID
        )
        source_import.diagnostics = import_diagnostics
        source_import.save(update_fields=["status", "diagnostics"])
        return {
            "status": source_import.status,
            "import_id": source_import.pk,
            "created": imported,
            "invalid": invalid,
            "recomputations": recomputations,
        }


def blocked_via_czechia_import(
    *, collection: ReferenceCollection, reason: str = "Source permission is unresolved"
) -> dict[str, Any]:
    """Record the block without making a request to the proprietary source."""

    if collection.source_kind != ReferenceSourceKind.VIA_CZECHIA:
        raise ValueError("The collection is not a Via Czechia collection")
    return {
        "status": ReferenceImportStatus.BLOCKED,
        "collection": collection.slug,
        "reason": reason,
    }
