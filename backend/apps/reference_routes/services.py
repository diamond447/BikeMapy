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
    relations: list[dict[str, Any]],
    payload: dict[str, Any],
    selected_relation_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    if selected_relation_ids is not None:
        selected_ids = set(selected_relation_ids)
        return [relation for relation in relations if relation.get("id") in selected_ids]
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
    node_points: dict[int, dict[str, Any]] | None = None,
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
    segment_way_ids: list[int] = []
    segments: list[list[tuple[float, float]]] = []
    fixed_direction: list[bool] = []
    seen_ids: set[int] = set()
    seen_segments: set[tuple[tuple[float, float], tuple[float, float]]] = set()
    seen_full_segments: set[tuple[tuple[float, float], ...]] = set()
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
        nodes = way.get("nodes")
        if (
            not isinstance(raw_geometry, list)
            and isinstance(nodes, list)
            and node_points is not None
        ):
            raw_geometry = [node_points.get(node, {}) for node in nodes]
        if not isinstance(raw_geometry, list) or len(raw_geometry) < 2:
            diagnostics.append(
                RouteDiagnostic("missing_nodes", f"Way {way_id} has no complete geometry.")
            )
            continue
        if isinstance(nodes, list) and len(nodes) != len(raw_geometry):
            diagnostics.append(
                RouteDiagnostic("missing_nodes", f"Way {way_id} node and geometry counts differ.")
            )
        if isinstance(nodes, list) and any(node not in (node_ids or set()) for node in nodes):
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
        if len(set(concrete)) < 2 or any(
            left == right for left, right in zip(concrete, concrete[1:], strict=False)
        ):
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
        role = normalize_source_text(member.get("role"))
        tags = relation.get("tags")
        if (
            isinstance(tags, dict)
            and normalize_source_text(tags.get("signed_direction")) == "yes"
            and role not in {"forward", "backward"}
        ):
            diagnostics.append(
                RouteDiagnostic(
                    "signed_direction_missing_role",
                    f"Way {way_id} lacks a signed direction role.",
                )
            )
        canonical_segment = tuple(sorted((concrete[0], concrete[-1])))
        if canonical_segment in seen_segments:
            diagnostics.append(
                RouteDiagnostic("duplicate_segment", f"Way {way_id} duplicates another segment.")
            )
        seen_segments.add(canonical_segment)
        full_segment = tuple(concrete)
        reverse_segment = tuple(reversed(concrete))
        if full_segment in seen_full_segments or reverse_segment in seen_full_segments:
            diagnostics.append(
                RouteDiagnostic(
                    "duplicate_segment", f"Way {way_id} duplicates an internal segment."
                )
            )
        seen_full_segments.add(full_segment)
        segments.append(concrete)
        segment_way_ids.append(way_id)
        fixed_direction.append(role in {"forward", "backward"})
    if not segments:
        diagnostics.append(
            RouteDiagnostic("empty_geometry", "Route contains no usable way geometry.")
        )
        return None, diagnostics, {"way_ids": way_ids}
    tags = relation.get("tags")
    signed_direction = (
        isinstance(tags, dict) and normalize_source_text(tags.get("signed_direction")) == "yes"
    )
    if signed_direction:
        assembled = list(segments[0])
        for index, segment in enumerate(segments[1:], start=1):
            if assembled[-1] != segment[0]:
                diagnostics.append(
                    RouteDiagnostic(
                        "signed_direction_order",
                        "Signed route members are not consecutive in source order.",
                        {
                            "previous_way_id": segment_way_ids[index - 1],
                            "offending_way_id": segment_way_ids[index],
                            "expected_endpoint": list(assembled[-1]),
                            "actual_endpoint": list(segment[0]),
                        },
                    )
                )
            assembled.extend(segment[1:])
    else:
        degree: dict[tuple[float, float], int] = {}
        for segment in segments:
            degree[segment[0]] = degree.get(segment[0], 0) + 1
            degree[segment[-1]] = degree.get(segment[-1], 0) + 1
        if any(value > 2 for value in degree.values()):
            diagnostics.append(
                RouteDiagnostic("branching_geometry", "Route members form a branching graph.")
            )
        endpoints = sorted(point for point, value in degree.items() if value == 1)
        start = endpoints[0] if endpoints else min(degree)
        assembled = [start]
        unused = set(range(len(segments)))
        current = start
        while unused:
            options: list[tuple[int, bool]] = []
            for index in unused:
                segment = segments[index]
                if segment[0] == current:
                    options.append((index, False))
                if not fixed_direction[index] and segment[-1] == current:
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
    for first_index, first in enumerate(zip(assembled, assembled[1:], strict=False)):
        if first[0] == first[1]:
            diagnostics.append(
                RouteDiagnostic("zero_length", "Assembled geometry contains a zero-length segment.")
            )
        for second_index, second in enumerate(zip(assembled, assembled[1:], strict=False)):
            if second_index <= first_index + 1:
                continue
            shares_endpoint = (
                first[0] == second[0]
                or first[0] == second[1]
                or first[1] == second[0]
                or first[1] == second[1]
            )
            if shares_endpoint:
                is_closed_loop_join = (
                    first_index == 0
                    and second_index == len(assembled) - 2
                    and assembled[0] == assembled[-1]
                )
                if not is_closed_loop_join:
                    diagnostics.append(
                        RouteDiagnostic("self_intersection", "Assembled geometry self-intersects.")
                    )
                    break
                continue
            if _segments_intersect(first[0], first[1], second[0], second[1]):
                diagnostics.append(
                    RouteDiagnostic("self_intersection", "Assembled geometry self-intersects.")
                )
                break
    diagnostics.extend(_geometry_diagnostics([[lon, lat] for lon, lat in assembled]))
    normalized = [[lon, lat] for lon, lat in assembled]
    return normalized if not diagnostics else None, diagnostics, {"way_ids": way_ids}


def _segments_intersect(
    a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]
) -> bool:
    def orientation(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> bool:
        return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and min(p[1], r[1]) <= q[1] <= max(
            p[1], r[1]
        )

    first = orientation(a, b, c)
    second = orientation(a, b, d)
    third = orientation(c, d, a)
    fourth = orientation(c, d, b)
    if first == 0 and on_segment(a, c, b):
        return True
    if second == 0 and on_segment(a, d, b):
        return True
    if third == 0 and on_segment(c, a, d):
        return True
    if fourth == 0 and on_segment(c, b, d):
        return True
    return (first > 0) != (second > 0) and (third > 0) != (fourth > 0)


def _geometry_value(coordinates: list[list[float]] | None) -> Any:
    if coordinates is None:
        return None
    if _GIS_AVAILABLE:
        from django.contrib.gis.geos import GEOSGeometry

        return GEOSGeometry(
            json.dumps({"type": "LineString", "coordinates": coordinates}), srid=4326
        )
    return {"type": "LineString", "coordinates": coordinates}


def _geometry_diagnostics(coordinates: list[list[float]]) -> list[RouteDiagnostic]:
    if not _GIS_AVAILABLE:
        return []
    from django.contrib.gis.geos import GEOSGeometry

    geometry = GEOSGeometry(
        json.dumps({"type": "LineString", "coordinates": coordinates}), srid=4326
    )
    diagnostics: list[RouteDiagnostic] = []
    if not geometry.valid:
        diagnostics.append(
            RouteDiagnostic("invalid_geometry", f"GEOS rejected the line: {geometry.valid_reason}")
        )
    if not geometry.simple:
        diagnostics.append(RouteDiagnostic("self_intersection", "GEOS found a non-simple line."))
    return diagnostics


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
    stored_import_id: int | None = None,
) -> dict[str, Any]:
    """Process one bounded snapshot. Invalid records are isolated per relation."""
    if collection.source_kind != ReferenceSourceKind.OSM_NUMBERED:
        raise ValueError("Only the approved OSM numbered-route source can be imported")
    if not collection.permission_granted:
        raise PermissionError("This source collection is not approved for ingestion")
    collection.full_clean()
    raw, parsed = _response_bytes(payload)
    metadata = dict(response_metadata or {})
    checksum = hashlib.sha256(raw).hexdigest()
    manifest_hash = metadata.get("raw_sha256") or metadata.get("sha256")
    if manifest_hash is not None and str(manifest_hash) != checksum:
        raise ValueError("Source response hash does not match the manifest")
    http_status = metadata.get("http_status")
    if http_status is not None and not 200 <= int(http_status) < 300:
        raise ValueError("Source response HTTP status is not successful")
    expected_ids = metadata.get("expected_relation_ids")
    if expected_ids is not None:
        if (
            not isinstance(expected_ids, list)
            or not expected_ids
            or any(not isinstance(value, int) or isinstance(value, bool) for value in expected_ids)
        ):
            raise ValueError("Manifest must contain non-empty integer relation IDs")
        if len(set(expected_ids)) != len(expected_ids):
            raise ValueError("Manifest relation IDs must be unique")
        if metadata.get("expected_relation_count") is None:
            raise ValueError("Manifest relation count is required with selected IDs")
    retrieved = retrieved_at or timezone.now()
    source_timestamp = None
    source_base = (
        parsed.get("osm3s", {}).get("timestamp_osm_base")
        if isinstance(parsed.get("osm3s"), dict)
        else None
    )
    source_base = source_base or metadata.get("source_timestamp")
    if source_base:
        try:
            source_timestamp = datetime.fromisoformat(str(source_base).replace("Z", "+00:00"))
        except ValueError:
            metadata = {**metadata, "source_timestamp_parse_error": str(source_base)}
    manifest_timestamp = metadata.get("source_timestamp")
    if manifest_timestamp is not None:
        if (
            source_timestamp is None
            or not source_base
            or str(manifest_timestamp) != str(source_base)
        ):
            raise ValueError("OSM timestamp does not match the manifest")
    with transaction.atomic():
        if stored_import_id is not None:
            source_import = ReferenceImport.objects.select_for_update().get(pk=stored_import_id)
            if (
                source_import.collection_id != collection.pk
                or source_import.raw_response_sha256 != checksum
            ):
                raise ValueError(
                    "Stored source snapshot does not match the supplied collection or bytes"
                )
            if source_import.status != ReferenceImportStatus.DISCOVERED:
                return {
                    "status": "unchanged",
                    "import_id": source_import.pk,
                    "created": 0,
                    "invalid": 0,
                    "recomputations": 0,
                }
            created = True
        else:
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
        selected = _selected_relations(relations, parsed, expected_ids)
        node_points = {
            int(item["id"]): item
            for item in parsed.get("elements", [])
            if isinstance(item, dict)
            and item.get("type") == "node"
            and isinstance(item.get("id"), int)
        }
        node_ids = set(node_points)
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
        if expected_ids is not None and sorted(int(value) for value in expected_ids) != sorted(
            int(relation["id"]) for relation in selected
        ):
            expected = -1
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
                relation, ways, node_ids, node_points
            )
            diagnostics.extend(geometry_diagnostics)
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
            if diagnostics:
                invalid += 1
                import_diagnostics.append(
                    {
                        "source_identifier": f"osm-relation:{relation['id']}",
                        "diagnostics": [diagnostic.as_dict() for diagnostic in diagnostics],
                    }
                )
                continue
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
                    attribution_metadata={
                        "attribution_text": collection.attribution_text,
                        "attribution_url": collection.attribution_url,
                        "licence": collection.licence,
                        "licence_uri": collection.licence_uri,
                        "derivative_offer_url": collection.derivative_offer_url,
                        "rightsholder": collection.rightsholder,
                        "contact_url": collection.contact_url,
                        "source_url": collection.source_url,
                    },
                    validation_status=ReferenceValidationStatus.VALID,
                    diagnostics=[],
                    active=False,
                )
                imported += 1
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
                    for historical_version in (
                        ReferenceRouteVersion.objects.select_for_update()
                        .filter(route=route)
                        .exclude(pk=version.pk)
                    ):
                        historical_version.active = False
                        historical_version.save(update_fields=["active"])
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


def store_pending_snapshot(
    *,
    collection: ReferenceCollection,
    raw_response: bytes,
    endpoint: str,
    query_text: str,
    retrieved_at: datetime,
    response_metadata: dict[str, Any],
) -> ReferenceImport:
    """Persist a bounded upload for an operator/Celery worker to process later."""
    if (
        collection.source_kind != ReferenceSourceKind.OSM_NUMBERED
        or not collection.permission_granted
    ):
        raise PermissionError("This source collection is not approved for ingestion")
    collection.full_clean()
    raw, parsed = _response_bytes(raw_response)
    checksum = hashlib.sha256(raw).hexdigest()
    source_base = (
        parsed.get("osm3s", {}).get("timestamp_osm_base")
        if isinstance(parsed.get("osm3s"), dict)
        else None
    ) or response_metadata.get("source_timestamp")
    if not source_base:
        raise ValueError("OSM snapshot is missing its source timestamp")
    try:
        source_timestamp = datetime.fromisoformat(str(source_base).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("OSM snapshot has an invalid osm3s timestamp") from exc
    return ReferenceImport.objects.get_or_create(
        collection=collection,
        checksum=checksum,
        defaults={
            "endpoint": endpoint,
            "query_text": query_text,
            "retrieved_at": retrieved_at,
            "source_timestamp": source_timestamp,
            "response_metadata": response_metadata,
            "raw_payload": parsed,
            "raw_response": raw,
            "raw_response_sha256": checksum,
        },
    )[0]


def approve_reference_route(route: ReferenceRoute, *, reviewer: str) -> ReferenceRoute:
    """Explicit human-review/publication action; imports never call this."""
    if route.collection.source_kind != ReferenceSourceKind.OSM_NUMBERED:
        raise PermissionError("This source kind is not approved for publication")
    if not route.collection.permission_granted:
        raise PermissionError("This source collection is not approved for publication")
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
