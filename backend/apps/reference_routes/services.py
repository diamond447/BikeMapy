"""Bounded, provenance-aware import and validation for approved OSM data."""

# mypy: disable-error-code="import-untyped,misc,no-any-return,return-value,union-attr,arg-type"

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.catalogue.fields import _GIS_AVAILABLE

from .models import (
    ReferenceCollection,
    ReferenceImport,
    ReferenceImportStatus,
    ReferencePublicationStatus,
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
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_RELATIONS = 5_000
MAX_MEMBERS_PER_RELATION = 5_000
MAX_COORDINATES = 1_000_000
_REF_RE = re.compile(r"^[0-9]{1,4}$")
_INTERNATIONAL_REF_RE = re.compile(r"^(?:ev|eurovelo)[ -]?[0-9]{1,2}$")
_MARKERS = {"eurovelo", "international", "ecf"}
_CZ_BOUNDS = (12.0, 48.5, 19.0, 51.1)


@dataclass(frozen=True)
class RouteDiagnostic:
    code: str
    message: str
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details or {}}


def normalize_source_text(value: Any) -> str:
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
    if not normalize_source_text(tags.get("operator")):
        diagnostics.append(
            RouteDiagnostic(
                "missing_operator", "A numbered candidate requires an explicit operator tag."
            )
        )
    return diagnostics


def _response_bytes(payload: dict[str, Any] | bytes) -> tuple[bytes, dict[str, Any]]:
    if isinstance(payload, bytes):
        if not payload or len(payload) > MAX_RESPONSE_BYTES:
            raise ValueError("empty or oversized source response")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("source response is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("source response must be a JSON object")
        return payload, parsed
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise ValueError("source response is oversized")
    return encoded, payload


def _relation_elements(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    elements = payload.get("elements")
    if not isinstance(elements, list) or not elements:
        raise ValueError("empty or incomplete OSM response")
    if len(elements) > MAX_RELATIONS * 4:
        raise ValueError("source response contains too many elements")
    relations = [
        item
        for item in elements
        if isinstance(item, dict)
        and item.get("type") == "relation"
        and isinstance(item.get("id"), int)
    ]
    if not relations:
        raise ValueError("source response contains no relations")
    if len(relations) > MAX_RELATIONS:
        raise ValueError("source response contains too many relations")
    ways = {
        int(item["id"]): item
        for item in elements
        if isinstance(item, dict) and item.get("type") == "way" and isinstance(item.get("id"), int)
    }
    return relations, ways


def _selected_relations(
    relations: list[dict[str, Any]], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    explicit = payload.get("selected_relation_ids")
    if isinstance(explicit, list):
        selected_ids = {int(value) for value in explicit if str(value).isdigit()}
        return [relation for relation in relations if relation.get("id") in selected_ids]
    candidates = [
        relation
        for relation in relations
        if isinstance(relation.get("tags"), dict) and relation["tags"].get("route") == "bicycle"
    ]
    child_ids = {
        member.get("ref")
        for relation in candidates
        for member in relation.get("members", [])
        if member.get("type") == "relation" and isinstance(member.get("ref"), int)
    }
    return [relation for relation in candidates if relation.get("id") not in child_ids]


def _point(value: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lon, lat = float(value["lon"]), float(value["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(lon) or not math.isfinite(lat):
        return None
    return lon, lat


def _assemble_geometry(
    relation: dict[str, Any],
    ways: dict[int, dict[str, Any]],
    node_ids: set[int] | None = None,
) -> tuple[list[list[float]] | None, list[RouteDiagnostic], dict[str, Any]]:
    diagnostics: list[RouteDiagnostic] = []
    members = relation.get("members", [])
    if not isinstance(members, list) or len(members) > MAX_MEMBERS_PER_RELATION:
        return (
            None,
            [RouteDiagnostic("member_limit", "Relation has too many members.")],
            {"way_ids": []},
        )
    way_ids: list[int] = []
    segments: list[list[tuple[float, float]]] = []
    seen_ids: set[int] = set()
    seen_segments: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    coordinate_count = 0
    for member in members:
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        try:
            way_id = int(member["ref"])
        except (KeyError, TypeError, ValueError):
            diagnostics.append(
                RouteDiagnostic("invalid_member", "Route has a malformed way member.")
            )
            continue
        if way_id in seen_ids:
            diagnostics.append(
                RouteDiagnostic("duplicate_member", f"Way {way_id} is referenced more than once.")
            )
            continue
        seen_ids.add(way_id)
        way_ids.append(way_id)
        way = ways.get(way_id)
        if way is None:
            diagnostics.append(
                RouteDiagnostic(
                    "missing_way", f"Referenced way {way_id} is absent from the response."
                )
            )
            continue
        raw_geometry = way.get("geometry")
        if not isinstance(raw_geometry, list) or len(raw_geometry) < 2:
            diagnostics.append(
                RouteDiagnostic("missing_nodes", f"Way {way_id} has no complete geometry.")
            )
            continue
        nodes = way.get("nodes")
        if isinstance(nodes, list) and len(nodes) != len(raw_geometry):
            diagnostics.append(
                RouteDiagnostic("missing_nodes", f"Way {way_id} node and geometry counts differ.")
            )
        if isinstance(nodes, list) and node_ids and any(node not in node_ids for node in nodes):
            diagnostics.append(
                RouteDiagnostic(
                    "missing_nodes", f"Way {way_id} references nodes absent from the response."
                )
            )
        coordinate_count += len(raw_geometry)
        if coordinate_count > MAX_COORDINATES:
            diagnostics.append(
                RouteDiagnostic("coordinate_limit", "Snapshot exceeds the coordinate limit.")
            )
            break
        points = [_point(point) for point in raw_geometry if isinstance(point, dict)]
        if any(point is None for point in points):
            diagnostics.append(
                RouteDiagnostic(
                    "non_finite_coordinate", f"Way {way_id} has a malformed coordinate."
                )
            )
            continue
        concrete = [point for point in points if point is not None]
        if len(set(concrete)) < 2:
            diagnostics.append(RouteDiagnostic("zero_length", f"Way {way_id} has zero length."))
            continue
        for lon, lat in concrete:
            if not (
                _CZ_BOUNDS[0] <= lon <= _CZ_BOUNDS[2] and _CZ_BOUNDS[1] <= lat <= _CZ_BOUNDS[3]
            ):
                diagnostics.append(
                    RouteDiagnostic(
                        "outside_czechia",
                        f"Way {way_id} contains a coordinate outside Czechia bounds.",
                    )
                )
                break
        if normalize_source_text(member.get("role")) == "backward":
            concrete.reverse()
        canonical_segment = tuple(sorted((concrete[0], concrete[-1])))
        if canonical_segment in seen_segments:
            diagnostics.append(
                RouteDiagnostic("duplicate_segment", f"Way {way_id} duplicates another segment.")
            )
        seen_segments.add(canonical_segment)
        segments.append(concrete)
    if not segments:
        diagnostics.append(
            RouteDiagnostic("empty_geometry", "Route contains no usable way geometry.")
        )
        return None, diagnostics, {"way_ids": way_ids}
    endpoints = [(segment[0], index) for index, segment in enumerate(segments)] + [
        (segment[-1], index) for index, segment in enumerate(segments)
    ]
    start, _ = min(endpoints)
    assembled = [start]
    unused = set(range(len(segments)))
    current = start
    while unused:
        options: list[tuple[int, bool]] = []
        for index in unused:
            segment = segments[index]
            if segment[0] == current:
                options.append((index, False))
            if segment[-1] == current:
                options.append((index, True))
        if not options:
            diagnostics.append(
                RouteDiagnostic(
                    "disconnected_geometry",
                    "Route members cannot be assembled into one connected line.",
                )
            )
            break
        index, reverse = min(options)
        segment = list(reversed(segments[index])) if reverse else segments[index]
        assembled.extend(segment[1:])
        current = segment[-1]
        unused.remove(index)
    if len(assembled) < 2 or len(set(map(tuple, assembled))) < 2:
        diagnostics.append(
            RouteDiagnostic("self_invalid_geometry", "Assembled route geometry is invalid.")
        )
    return assembled if not diagnostics else None, diagnostics, {"way_ids": way_ids}


def _geometry_value(coordinates: list[list[float]] | None) -> Any:
    if coordinates is None:
        return None
    if _GIS_AVAILABLE:
        from django.contrib.gis.geos import GEOSGeometry

        return GEOSGeometry(
            json.dumps({"type": "LineString", "coordinates": coordinates}), srid=4326
        )
    return {"type": "LineString", "coordinates": coordinates}


def _canonical_geometry(value: Any) -> Any:
    if hasattr(value, "geojson"):
        return json.loads(value.geojson)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def import_osm_snapshot(
    *,
    collection: ReferenceCollection,
    payload: dict[str, Any] | bytes,
    endpoint: str = OSM_OVERPASS_ENDPOINT,
    query_text: str = OSM_DISCOVERY_QUERY,
    retrieved_at: datetime | None = None,
    response_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process one bounded snapshot. Invalid records are isolated per relation."""
    if collection.source_kind != ReferenceSourceKind.OSM_NUMBERED:
        raise ValueError("Only the approved OSM numbered-route source can be imported")
    collection.full_clean()
    raw, parsed = _response_bytes(payload)
    metadata = response_metadata or {}
    checksum = hashlib.sha256(raw).hexdigest()
    retrieved = retrieved_at or timezone.now()
    source_timestamp = None
    source_base = (
        parsed.get("osm3s", {}).get("timestamp_osm_base")
        if isinstance(parsed.get("osm3s"), dict)
        else None
    )
    if source_base:
        try:
            source_timestamp = datetime.fromisoformat(str(source_base).replace("Z", "+00:00"))
        except ValueError:
            metadata = {**metadata, "source_timestamp_parse_error": str(source_base)}
    with transaction.atomic():
        source_import, created = ReferenceImport.objects.get_or_create(
            collection=collection,
            checksum=checksum,
            defaults={
                "endpoint": endpoint,
                "query_text": query_text,
                "retrieved_at": retrieved,
                "source_timestamp": source_timestamp,
                "response_metadata": metadata,
                "raw_payload": parsed,
                "raw_response": raw,
                "raw_response_sha256": checksum,
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
            relations, ways = _relation_elements(parsed)
        except ValueError as exc:
            source_import.status = ReferenceImportStatus.FAILED
            source_import.diagnostics = [RouteDiagnostic("incomplete_snapshot", str(exc)).as_dict()]
            source_import.save(update_fields=["status", "diagnostics"])
            return {
                "status": "failed",
                "import_id": source_import.pk,
                "created": 0,
                "invalid": 0,
                "recomputations": 0,
            }
        selected = _selected_relations(relations, parsed)
        node_ids = {
            int(item["id"])
            for item in parsed.get("elements", [])
            if isinstance(item, dict)
            and item.get("type") == "node"
            and isinstance(item.get("id"), int)
        }
        if metadata.get("complete") is False or parsed.get("remark") or parsed.get("error"):
            source_import.status = ReferenceImportStatus.FAILED
            source_import.diagnostics = [
                RouteDiagnostic(
                    "incomplete_snapshot",
                    "The source marked this response as incomplete or truncated.",
                ).as_dict()
            ]
            source_import.save(update_fields=["status", "diagnostics"])
            return {
                "status": "failed",
                "import_id": source_import.pk,
                "created": 0,
                "invalid": 0,
                "recomputations": 0,
            }
        if not selected:
            source_import.status = ReferenceImportStatus.FAILED
            source_import.diagnostics = [
                RouteDiagnostic(
                    "no_selected_candidates",
                    "The snapshot contains no selected top-level numbered candidates.",
                ).as_dict()
            ]
            source_import.save(update_fields=["status", "diagnostics"])
            return {
                "status": "failed",
                "import_id": source_import.pk,
                "created": 0,
                "invalid": 0,
                "recomputations": 0,
            }
        expected = metadata.get("expected_relation_count")
        if expected is not None and int(expected) != len(selected):
            source_import.status = ReferenceImportStatus.FAILED
            source_import.diagnostics = [
                RouteDiagnostic(
                    "incomplete_snapshot",
                    "Selected relation count does not match the expected count.",
                ).as_dict()
            ]
            source_import.save(update_fields=["status", "diagnostics"])
            return {
                "status": "failed",
                "import_id": source_import.pk,
                "created": 0,
                "invalid": 0,
                "recomputations": 0,
            }
        imported = invalid = recomputations = 0
        import_diagnostics: list[dict[str, Any]] = []
        for relation in selected:
            tags = relation.get("tags") if isinstance(relation.get("tags"), dict) else {}
            diagnostics = validate_osm_candidate(tags)
            coordinates, geometry_diagnostics, member_meta = _assemble_geometry(
                relation, ways, node_ids
            )
            diagnostics.extend(geometry_diagnostics)
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
            route, _ = ReferenceRoute.objects.get_or_create(
                collection=collection,
                source_identifier=f"osm-relation:{relation['id']}",
                defaults={
                    "route_number": normalize_source_text(tags.get("ref")),
                    "title": str(tags.get("name") or tags.get("ref") or relation["id"]),
                    "operator": str(tags.get("operator") or ""),
                    "network": normalize_source_text(tags.get("network")),
                    "source_tags": tags,
                },
            )
            with transaction.atomic():
                route = ReferenceRoute.objects.select_for_update().get(pk=route.pk)
                route.route_number = normalize_source_text(tags.get("ref"))
                route.title = str(tags.get("name") or tags.get("ref") or route.source_identifier)
                route.operator = str(tags.get("operator") or "")
                route.network = normalize_source_text(tags.get("network"))
                route.source_tags = tags
                route.save(
                    update_fields=[
                        "route_number",
                        "title",
                        "operator",
                        "network",
                        "source_tags",
                        "updated_at",
                    ]
                )
                if ReferenceRouteVersion.objects.filter(
                    route=route, checksum=version_checksum
                ).exists():
                    continue
                version_number = (
                    ReferenceRouteVersion.objects.select_for_update()
                    .filter(route=route)
                    .order_by("-version_number")
                    .values_list("version_number", flat=True)
                    .first()
                    or 0
                ) + 1
                version = ReferenceRouteVersion.objects.create(
                    route=route,
                    source_import=source_import,
                    version_number=version_number,
                    source_version_identifier=(
                        f"osm:{relation['id']}:v{relation.get('version', 'unknown')}"
                        f":{relation.get('timestamp', 'unknown')}"
                        f":{relation.get('changeset', 'unknown')}"
                    ),
                    checksum=version_checksum,
                    source_geometry=source_geometry,
                    normalized_geometry=_geometry_value(coordinates),
                    provenance={
                        "source": "OpenStreetMap",
                        "relation_id": relation["id"],
                        "endpoint": endpoint,
                        "retrieved_at": retrieved.isoformat(),
                        "source_timestamp": source_base,
                        "query_text": query_text,
                        "query_sha256": hashlib.sha256(query_text.encode()).hexdigest(),
                        "member_way_ids": member_meta["way_ids"],
                    },
                    attribution=collection.attribution_text or collection.attribution,
                    validation_status=status,
                    diagnostics=[diagnostic.as_dict() for diagnostic in diagnostics],
                    active=False,
                )
                imported += 1
                if diagnostics:
                    invalid += 1
                    route.active = False
                    route.publication_status = ReferencePublicationStatus.PENDING
                    route.save(update_fields=["active", "publication_status", "updated_at"])
                    import_diagnostics.append(
                        {
                            "source_identifier": route.source_identifier,
                            "diagnostics": version.diagnostics,
                        }
                    )
                    continue
                old = route.current_version
                if old is not None and _canonical_geometry(
                    old.normalized_geometry
                ) != _canonical_geometry(version.normalized_geometry):
                    ReferenceRecomputation.objects.create(
                        route_version=version,
                        reason="reference route geometry changed",
                        scheduled_at=timezone.now(),
                    )
                    recomputations += 1
                if old is not None:
                    ReferenceRouteVersion.objects.filter(route=route).exclude(pk=version.pk).update(
                        active=False
                    )
                route.current_version = version
                route.active = False
                route.publication_status = ReferencePublicationStatus.PENDING
                route.save(
                    update_fields=["current_version", "active", "publication_status", "updated_at"]
                )
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


def approve_reference_route(route: ReferenceRoute, *, reviewer: str) -> ReferenceRoute:
    """Explicit human-review/publication action; imports never call this."""
    if (
        route.current_version_id is None
        or route.current_version.validation_status != ReferenceValidationStatus.VALID
    ):
        raise ValueError("Only a technically valid current version can be approved")
    route.publication_status = ReferencePublicationStatus.APPROVED
    route.reviewed_at = timezone.now()
    route.reviewed_by = reviewer
    route.active = True
    route.save(
        update_fields=["publication_status", "reviewed_at", "reviewed_by", "active", "updated_at"]
    )
    route.current_version.active = True
    route.current_version.save(update_fields=["active"])
    return route


def link_stage(*, stage: ReferenceRoute, parent: ReferenceRoute) -> ReferenceRoute:
    """Link an explicitly supplied stage without creating parent cycles."""
    if stage.pk == parent.pk or stage.collection_id != parent.collection_id:
        raise ValueError("A stage must have a distinct parent in the same collection")
    cursor = parent
    seen: set[Any] = set()
    while True:
        if cursor.pk in seen or cursor.pk == stage.pk:
            raise ValueError("Reference route parent cycle detected")
        seen.add(cursor.pk)
        if cursor.parent_id is None or cursor.parent is None:
            break
        cursor = cursor.parent
    stage.parent = parent
    stage.route_type = ReferenceRoute.RouteType.STAGE
    stage.save(update_fields=["parent", "route_type", "updated_at"])
    return stage


def blocked_via_czechia_import(
    *, collection: ReferenceCollection, reason: str = "Source permission is unresolved"
) -> dict[str, Any]:
    if collection.source_kind != ReferenceSourceKind.VIA_CZECHIA:
        raise ValueError("The collection is not a Via Czechia collection")
    return {
        "status": ReferenceImportStatus.BLOCKED,
        "collection": collection.slug,
        "reason": reason,
    }
