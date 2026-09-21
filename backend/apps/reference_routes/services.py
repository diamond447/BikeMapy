"""Bounded, provenance-aware import and validation for approved OSM data."""

# mypy: disable-error-code="import-untyped,misc,no-any-return,return-value,union-attr,arg-type"

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.catalogue.fields import _GIS_AVAILABLE

from .models import (
    OSM_DISCOVERY_QUERY,
    OSM_OVERPASS_ENDPOINT,
    ReferenceAlterationOffer,
    ReferenceCollection,
    ReferenceImport,
    ReferenceImportStatus,
    ReferencePublicationStatus,
    ReferenceRecomputation,
    ReferenceRoute,
    ReferenceRouteVersion,
    ReferenceSourceKind,
    ReferenceValidationStatus,
    has_publishable_reference_source,
)

MAX_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_DISCOVERY_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_OVERPASS_FAILURE_LOG_BYTES = 512 * 1024
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
    relation_ids = [relation["id"] for relation in relations]
    if len(relation_ids) != len(set(relation_ids)):
        raise ValueError("source response contains duplicate relation IDs")
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


def _parse_utc_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be timezone-aware UTC")
    return parsed.astimezone(UTC)


def _validate_retrieved_at(value: datetime) -> datetime:
    if not timezone.is_aware(value) or value.utcoffset() != timedelta(0):
        raise ValueError("retrieved_at must be timezone-aware UTC")
    retrieved = value.astimezone(UTC)
    if retrieved > timezone.now().astimezone(UTC):
        raise ValueError("retrieved_at cannot be in the future")
    return retrieved


def _validate_http_evidence(metadata: dict[str, Any]) -> None:
    headers = metadata.get("http_headers")
    absence = metadata.get("http_header_absence")
    if headers is None and absence is None:
        return
    if not isinstance(headers, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not value.strip()
        for key, value in headers.items()
    ):
        raise ValueError("Manifest HTTP headers must contain non-empty string values")
    if not isinstance(absence, list) or any(
        not isinstance(value, str) or not value.strip() for value in absence
    ):
        raise ValueError("Manifest HTTP header absence must be a list of names")
    normalized = {key.casefold(): value.strip() for key, value in headers.items()}
    absent = {value.casefold() for value in absence}
    if not normalized.get("content-type"):
        raise ValueError("Manifest HTTP evidence requires a non-empty content-type header")
    for conditional in ("etag", "last-modified"):
        if conditional not in normalized and conditional not in absent:
            raise ValueError(
                f"Manifest HTTP evidence must record whether {conditional} was provided"
            )
        if conditional in normalized and conditional in absent:
            raise ValueError(f"Manifest HTTP evidence marks {conditional} both present and absent")


def _decode_verified_content(
    value: Any, *, field: str, expected_hash: Any, max_bytes: int
) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is required")
    max_encoded_length = ((max_bytes + 2) // 3) * 4
    if len(value) > max_encoded_length:
        raise ValueError(f"{field} exceeds the retained byte limit")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise ValueError(f"{field} hash is invalid")
    try:
        content = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"{field} is not valid base64") from exc
    if not content or len(content) > max_bytes:
        raise ValueError(f"{field} exceeds the retained byte limit")
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError(f"{field} hash does not match retained bytes")
    return content


def _parse_verified_discovery_bytes(metadata: dict[str, Any]) -> dict[str, Any]:
    content = _decode_verified_content(
        metadata.get("discovery_artifact_content_base64"),
        field="Production discovery artifact content",
        expected_hash=metadata.get("discovery_result_sha256"),
        max_bytes=MAX_DISCOVERY_ARTIFACT_BYTES,
    )
    try:
        discovery = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Production discovery artifact is not valid JSON") from exc
    if not isinstance(discovery, dict) or not isinstance(discovery.get("elements"), list):
        raise ValueError("Production discovery artifact is not an OSM response")
    osm3s = discovery.get("osm3s")
    if not isinstance(osm3s, dict) or not osm3s.get("timestamp_osm_base"):
        raise ValueError("Production discovery artifact lacks OSM timestamp evidence")
    return discovery


def _validate_overpass_failure_evidence(
    value: Any,
    *,
    discovery_executed_at: datetime | None = None,
    retrieved_at: datetime | None = None,
) -> None:
    if not isinstance(value, dict):
        raise ValueError("Regional discovery requires retained Overpass failure evidence")
    if (
        value.get("endpoint") != OSM_OVERPASS_ENDPOINT
        or value.get("query_text") != OSM_DISCOVERY_QUERY
    ):
        raise ValueError("Overpass failure evidence is not bound to the approved request")
    attempted_at = _parse_utc_timestamp(value.get("attempted_at"), field="overpass attempted_at")
    now = timezone.now().astimezone(UTC)
    if attempted_at > now:
        raise ValueError("Overpass failure attempt cannot be in the future")
    if discovery_executed_at is not None and attempted_at > discovery_executed_at:
        raise ValueError("Overpass failure attempt must precede discovery execution")
    if retrieved_at is not None and attempted_at > retrieved_at:
        raise ValueError("Overpass failure attempt must precede import retrieval")
    network_status = value.get("network_status")
    if network_status not in {"http_error", "network_error"}:
        raise ValueError("Overpass failure evidence status is invalid")
    if network_status == "http_error" and (
        isinstance(value.get("http_status"), bool)
        or not isinstance(value.get("http_status"), int)
        or not 400 <= value["http_status"] < 600
    ):
        raise ValueError("Overpass HTTP failure status is invalid")
    if network_status == "network_error" and value.get("http_status") is not None:
        raise ValueError("Network failure evidence cannot contain an HTTP status")
    if not isinstance(value.get("error"), str) or not value["error"].strip():
        raise ValueError("Overpass failure error text is required")
    log_bytes = _decode_verified_content(
        value.get("log_base64"),
        field="Overpass failure log",
        expected_hash=value.get("log_sha256"),
        max_bytes=MAX_OVERPASS_FAILURE_LOG_BYTES,
    )
    try:
        log_payload = json.loads(log_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Overpass failure log is not valid JSON") from exc
    expected_payload = {
        "endpoint": value["endpoint"],
        "query_text": value["query_text"],
        "attempted_at": value["attempted_at"],
        "network_status": network_status,
        "http_status": value.get("http_status"),
        "error": value["error"],
    }
    if log_payload != expected_payload:
        raise ValueError("Overpass failure log does not match its manifest fields")


def _validate_discovery_evidence(
    *,
    parsed: dict[str, Any],
    metadata: dict[str, Any],
    endpoint: str,
    expected_ids: list[int],
    retrieved_at: datetime,
) -> None:
    """Require a retained discovery result that is distinct from the snapshot.

    A caller must provide the independently retained Overpass result, including
    the selected relation records.  A hash of a caller-authored list of IDs is
    deliberately insufficient evidence of discovery.
    """
    mechanism = metadata["discovery_mechanism"]
    executed_at = _parse_utc_timestamp(
        metadata["discovery_executed_at"], field="discovery_executed_at"
    )
    discovery_ids = metadata["discovery_selected_relation_ids"]
    if (
        not isinstance(discovery_ids, list)
        or any(not isinstance(value, int) or isinstance(value, bool) for value in discovery_ids)
        or sorted(discovery_ids) != sorted(expected_ids)
    ):
        raise ValueError("Production discovery IDs must exactly match imported IDs")
    artifact = metadata["discovery_artifact_url"]
    artifact_parts = urlsplit(artifact)
    endpoint_parts = urlsplit(endpoint)
    configured_parts = urlsplit(
        str(getattr(settings, "REFERENCE_ROUTE_DERIVATIVE_OFFER_URL", "") or "")
    )
    if (
        artifact_parts.scheme != "https"
        or not artifact_parts.netloc
        or artifact_parts.query
        or artifact_parts.fragment
        or not configured_parts.scheme
        or artifact_parts.scheme != configured_parts.scheme
        or artifact_parts.netloc != configured_parts.netloc
        or not artifact_parts.path.startswith(configured_parts.path.rstrip("/") + "/")
        or not any(
            marker in artifact_parts.path.split("/") for marker in ("discovery", "artifacts")
        )
        or not artifact_parts.path.casefold().endswith(".json")
        or (
            artifact_parts.scheme == endpoint_parts.scheme
            and artifact_parts.netloc == endpoint_parts.netloc
            and artifact_parts.path == endpoint_parts.path
        )
    ):
        raise ValueError("Production discovery artifact must be a separate trusted artifact")
    discovery_payload = _parse_verified_discovery_bytes(metadata)
    if mechanism == "overpass":
        if metadata["discovery_query_or_extract_id"] != (
            f"overpass:{hashlib.sha256(OSM_DISCOVERY_QUERY.encode()).hexdigest()}"
        ):
            raise ValueError("Production Overpass discovery must use the approved query")
    elif metadata.get("overpass_unavailable") is not True or not str(
        metadata["discovery_query_or_extract_id"]
    ).startswith("regional-extract:"):
        raise ValueError("Regional discovery requires recorded Overpass-first fallback evidence")
    if executed_at > timezone.now().astimezone(UTC):
        raise ValueError("Production discovery execution time is in the future")
    if executed_at > retrieved_at:
        raise ValueError("Discovery execution must precede import retrieval")
    if mechanism == "regional_extract":
        _validate_overpass_failure_evidence(
            metadata.get("overpass_failure_evidence"),
            discovery_executed_at=executed_at,
            retrieved_at=retrieved_at,
        )
    payload_elements = discovery_payload.get("elements")
    if not isinstance(payload_elements, list):
        raise ValueError("Production discovery result must retain relation records")
    discovery_relation_records = [
        item
        for item in payload_elements
        if isinstance(item, dict)
        and item.get("type") == "relation"
        and isinstance(item.get("id"), int)
    ]
    discovery_record_ids = [item["id"] for item in discovery_relation_records]
    if len(discovery_record_ids) != len(set(discovery_record_ids)):
        raise ValueError("Production discovery artifact contains duplicate relation IDs")
    discovered = {item["id"]: item for item in discovery_relation_records}
    source_relation_records = [
        item
        for item in parsed.get("elements", [])
        if isinstance(item, dict)
        and item.get("type") == "relation"
        and isinstance(item.get("id"), int)
    ]
    source_record_ids = [item["id"] for item in source_relation_records]
    if len(source_record_ids) != len(set(source_record_ids)):
        raise ValueError("Imported snapshot contains duplicate relation IDs")
    source_relations = {item["id"]: item for item in source_relation_records}
    if any(
        route_id not in discovered or route_id not in source_relations for route_id in expected_ids
    ):
        raise ValueError("Production discovery result does not contain imported relation records")
    for route_id in expected_ids:
        if discovered[route_id].get("tags") != source_relations[route_id].get("tags"):
            raise ValueError(
                "Production discovery relation tags do not match the imported snapshot"
            )
        if any(
            not isinstance(discovered[route_id].get(field), (int, str))
            or discovered[route_id].get(field) in (None, "")
            for field in ("version", "timestamp", "changeset")
        ):
            raise ValueError("Production discovery relation lacks OSM provenance fields")
        if any(
            discovered[route_id].get(field) != source_relations[route_id].get(field)
            for field in ("version", "timestamp", "changeset")
        ):
            raise ValueError("Production discovery provenance does not match the imported relation")
    metadata["discovery_result_payload"] = discovery_payload


def _validate_source_evidence(
    *,
    parsed: dict[str, Any],
    metadata: dict[str, Any],
    endpoint: str,
    selected: list[dict[str, Any]],
    retrieved_at: datetime,
) -> datetime | None:
    """Validate manifest evidence against the actual immutable response payload."""
    import_mode = metadata.get("import_mode")
    if import_mode not in {"test", "production", "validation"}:
        raise ValueError("Import mode is unsupported")
    if import_mode == "test" and not getattr(settings, "REFERENCE_ROUTE_ALLOW_TEST_IMPORTS", False):
        raise ValueError("Test imports are disabled outside an explicit test runtime")
    if import_mode == "production" and metadata.get("production_import") is not True:
        raise ValueError("Production imports require production mode")
    if import_mode == "validation" and metadata.get("validation_sample") is not True:
        raise ValueError("Validation mode requires a validation-only manifest")
    _validate_http_evidence(metadata)
    expected_ids = metadata.get("expected_relation_ids")
    if expected_ids is not None:
        if (
            not isinstance(expected_ids, list)
            or not expected_ids
            or any(not isinstance(value, int) or isinstance(value, bool) for value in expected_ids)
            or len(set(expected_ids)) != len(expected_ids)
        ):
            raise ValueError("Manifest relation IDs must be a non-empty unique integer list")
        expected_count = metadata.get("expected_relation_count")
        if isinstance(expected_count, bool) or not isinstance(expected_count, int):
            raise ValueError("Manifest relation count is invalid")
        selected_ids = sorted(int(relation["id"]) for relation in selected)
        if sorted(expected_ids) != selected_ids or expected_count != len(selected):
            raise ValueError("Manifest selected relation IDs/count do not match the payload")
    validation_sample = metadata.get("validation_sample", False)
    if not isinstance(validation_sample, bool):
        raise ValueError("Manifest validation_sample must be boolean")
    if validation_sample and metadata.get("validation_test_mode") is not True:
        raise ValueError("Validation-only source evidence requires explicit test mode")
    if metadata.get("production_import") is True:
        if validation_sample:
            raise ValueError("Validation-only source evidence cannot be imported as production")
        discovery_fields = (
            "discovery_mechanism",
            "discovery_query_or_extract_id",
            "discovery_executed_at",
            "discovery_result_sha256",
            "discovery_artifact_url",
            "discovery_selected_relation_ids",
            "discovery_artifact_content_base64",
        )
        if any(not metadata.get(field) for field in discovery_fields):
            raise ValueError("Production import requires complete discovery evidence")
        if metadata["discovery_mechanism"] not in {"overpass", "regional_extract"}:
            raise ValueError("Production discovery mechanism is unsupported")
        if (
            not isinstance(metadata["discovery_result_sha256"], str)
            or len(metadata["discovery_result_sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in metadata["discovery_result_sha256"])
        ):
            raise ValueError("Production discovery result hash is invalid")
        if expected_ids is None:
            raise ValueError("Production discovery evidence requires selected relation IDs")
        _validate_discovery_evidence(
            parsed=parsed,
            metadata=metadata,
            endpoint=endpoint,
            expected_ids=expected_ids,
            retrieved_at=retrieved_at,
        )
    count_fields = ("expected_way_count", "expected_node_count")
    present_counts = [field in metadata for field in count_fields]
    if any(present_counts):
        if not all(present_counts) or any(
            isinstance(metadata[field], bool) or not isinstance(metadata[field], int)
            for field in count_fields
        ):
            raise ValueError("Manifest way/node counts are invalid")
        actual_way_count = sum(
            isinstance(item, dict) and item.get("type") == "way"
            for item in parsed.get("elements", [])
        )
        actual_node_count = sum(
            isinstance(item, dict) and item.get("type") == "node"
            for item in parsed.get("elements", [])
        )
        if (
            metadata["expected_way_count"] != actual_way_count
            or metadata["expected_node_count"] != actual_node_count
        ):
            raise ValueError("Manifest way/node counts do not match the complete payload")
    relation_fields = ("relation_version", "relation_changeset", "relation_timestamp")
    present_relation_fields = [field in metadata for field in relation_fields]
    if any(present_relation_fields):
        if not all(present_relation_fields) or len(selected) != 1:
            raise ValueError("Manifest relation metadata is incomplete or ambiguous")
        relation = selected[0]
        for field in ("relation_version", "relation_changeset"):
            value = metadata[field]
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"Manifest {field} is invalid")
            if relation.get(field.removeprefix("relation_")) != value:
                raise ValueError(f"Manifest {field} does not match the selected relation")
        if str(relation.get("timestamp", "")) != str(metadata["relation_timestamp"]):
            raise ValueError("Manifest relation_timestamp does not match the selected relation")
        _parse_utc_timestamp(str(metadata["relation_timestamp"]), field="relation_timestamp")
    relation_timestamp = selected[0].get("timestamp") if len(selected) == 1 else None
    osm3s_timestamp = (
        parsed.get("osm3s", {}).get("timestamp_osm_base")
        if isinstance(parsed.get("osm3s"), dict)
        else None
    )
    endpoint_parts = urlsplit(endpoint)
    is_osm_api_relation = (
        endpoint_parts.hostname == "www.openstreetmap.org"
        and endpoint_parts.path.startswith("/api/0.6/relation/")
        and endpoint_parts.path.endswith("/full.json")
    )
    if is_osm_api_relation and not relation_timestamp:
        raise ValueError("OSM API response is missing the selected relation timestamp")
    evidence_value = (
        relation_timestamp if is_osm_api_relation else relation_timestamp or osm3s_timestamp
    )
    if evidence_value is None and "source_timestamp" in metadata:
        raise ValueError("Manifest source_timestamp has no corresponding payload evidence")
    source_timestamp = (
        _parse_utc_timestamp(str(evidence_value), field="payload source timestamp")
        if evidence_value
        else None
    )
    if "source_timestamp" in metadata:
        manifest_timestamp = _parse_utc_timestamp(
            metadata["source_timestamp"], field="source_timestamp"
        )
        if source_timestamp is None or manifest_timestamp != source_timestamp:
            raise ValueError("Manifest source_timestamp does not match payload evidence")
    return source_timestamp


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
    if response_metadata is None or "import_mode" not in response_metadata:
        raise ValueError("Source imports require an explicit import_mode")
    raw, parsed = _response_bytes(payload)
    metadata = dict(response_metadata)
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
    retrieved = _validate_retrieved_at(retrieved)
    relations, ways = _relation_elements(parsed)
    selected = _selected_relations(relations, parsed, expected_ids)
    source_timestamp = _validate_source_evidence(
        parsed=parsed,
        metadata=metadata,
        endpoint=endpoint,
        selected=selected,
        retrieved_at=retrieved,
    )
    source_base = source_timestamp.isoformat().replace("+00:00", "Z") if source_timestamp else None
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
                        "relation_version": relation.get("version"),
                        "relation_timestamp": relation.get("timestamp"),
                        "relation_changeset": relation.get("changeset"),
                        "relation_tags": tags,
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
    if "import_mode" not in response_metadata:
        raise ValueError("Stored snapshots require an explicit import_mode")
    retrieved_at = _validate_retrieved_at(retrieved_at)
    raw, parsed = _response_bytes(raw_response)
    checksum = hashlib.sha256(raw).hexdigest()
    relations, ways = _relation_elements(parsed)
    selected = _selected_relations(
        relations,
        parsed,
        response_metadata.get("expected_relation_ids"),
    )
    source_timestamp = _validate_source_evidence(
        parsed=parsed,
        metadata=response_metadata,
        endpoint=endpoint,
        selected=selected,
        retrieved_at=retrieved_at,
    )
    if source_timestamp is None:
        raise ValueError("OSM snapshot is missing its source timestamp")
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
    source_import = route.current_version.source_import if route.current_version else None
    if (
        route.collection.source_kind == ReferenceSourceKind.OSM_NUMBERED
        and not route.collection.permission_granted
    ):
        raise PermissionError("This source collection is not approved for publication")
    if (
        route.collection.source_kind != ReferenceSourceKind.OSM_NUMBERED
        and not has_publishable_reference_source(route.collection, source_import)
    ):
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
    if route.collection.source_kind == ReferenceSourceKind.VIA_CZECHIA:
        for stage in route.stages.select_related("current_version").order_by("pk"):
            if stage.current_version is None:
                continue
            stage.publication_status = ReferencePublicationStatus.APPROVED
            stage.reviewed_at = timezone.now()
            stage.reviewed_by = reviewer
            stage.active = True
            stage.save(
                update_fields=(
                    "publication_status",
                    "reviewed_at",
                    "reviewed_by",
                    "active",
                    "updated_at",
                )
            )
            stage.current_version.active = True
            stage.current_version.save(update_fields=("active",))
    from .completion_services import schedule_version_completions

    schedule_version_completions(route.current_version, reason="reference-route-version-approved")
    return route


def record_alteration_offer(
    *,
    source_import: ReferenceImport,
    manifest_url: str,
    artifact_url: str,
    method_url: str,
    published_at: datetime,
    offered_snapshot_hash: str,
    operator_evidence: str,
) -> ReferenceAlterationOffer:
    """Record immutable public evidence for one exact imported snapshot."""
    allowed_base = str(getattr(settings, "REFERENCE_ROUTE_DERIVATIVE_OFFER_URL", "") or "")
    if not allowed_base:
        raise ValueError("A deployable alteration-offer base URL is not configured")
    if source_import.status != ReferenceImportStatus.VALID:
        raise ValueError("Only a valid source import can receive an alteration offer")
    if source_import.response_metadata.get("import_mode") != "production":
        raise ValueError("Only production imports can receive a deployable alteration offer")
    if offered_snapshot_hash != source_import.raw_response_sha256:
        raise ValueError("Alteration offer hash does not match the immutable source snapshot")
    if not timezone.is_aware(published_at) or published_at.utcoffset() != timedelta(0):
        raise ValueError("Alteration offer publication time must be timezone-aware UTC")
    if not operator_evidence.strip():
        raise ValueError("Operator evidence is required for an alteration offer")
    offer = ReferenceAlterationOffer(
        source_import=source_import,
        manifest_url=manifest_url,
        artifact_url=artifact_url,
        method_url=method_url,
        published_at=published_at,
        offered_snapshot_hash=offered_snapshot_hash,
        operator_evidence=operator_evidence,
    )
    if not offer.is_complete_for(source_import, allowed_base):
        raise ValueError("Alteration offer URLs must be under the configured deployable base")
    offer.save(force_insert=True)
    return offer


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


def import_via_czechia_snapshot(
    *,
    collection: ReferenceCollection,
    payload: dict[str, Any],
    retrieved_at: datetime | None = None,
) -> dict[str, Any]:
    """Import a complete Via Czechia route only after explicit source gates.

    The provider contract is intentionally small and source-neutral: a
    snapshot contains one ``route`` object and optional ``stages`` objects,
    each with ``source_identifier``, ``title`` and GeoJSON ``geometry``.
    Until operator permission and source availability are configured this
    function returns a durable blocked result and creates no publication.
    """

    if collection.source_kind != ReferenceSourceKind.VIA_CZECHIA:
        raise ValueError("The collection is not a Via Czechia collection")
    if not getattr(settings, "REFERENCE_ROUTE_VIA_CZECHIA_ENABLED", False):
        return blocked_via_czechia_import(collection=collection, reason="Feature gate is disabled")
    if not collection.permission_granted or not collection.active:
        return blocked_via_czechia_import(
            collection=collection, reason="Source permission is unresolved"
        )
    if payload.get("source_available") is not True:
        return blocked_via_czechia_import(
            collection=collection, reason="Source availability is unverified"
        )
    route_data = payload.get("route")
    stages_data = payload.get("stages", [])
    if not isinstance(route_data, dict) or not isinstance(stages_data, list):
        raise ValueError("Via Czechia snapshot must contain a route and a stage list")
    records = [route_data, *[item for item in stages_data if isinstance(item, dict)]]
    if not records or any(
        not isinstance(item.get("source_identifier"), str)
        or not isinstance(item.get("title"), str)
        or not isinstance(item.get("geometry"), dict)
        for item in records
    ):
        raise ValueError("Via Czechia records require identifiers, titles and geometry")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    checksum = hashlib.sha256(raw).hexdigest()
    retrieved = retrieved_at or timezone.now()
    with transaction.atomic():
        source_import, created = ReferenceImport.objects.get_or_create(
            collection=collection,
            checksum=checksum,
            defaults={
                "endpoint": collection.source_url,
                "retrieved_at": retrieved,
                "response_metadata": {"source_available": True, "import_mode": "production"},
                "raw_payload": payload,
                "raw_response": raw,
                "raw_response_sha256": checksum,
                "status": ReferenceImportStatus.VALID,
            },
        )
        if not created:
            return {"status": "unchanged", "import_id": source_import.pk}
        parent = None
        imported = 0
        attribution_metadata = {
            "attribution_text": collection.attribution_text or collection.attribution,
            "attribution_url": collection.attribution_url,
            "licence": collection.licence,
            "licence_uri": collection.licence_uri,
            "derivative_offer_url": collection.derivative_offer_url,
            "rightsholder": collection.rightsholder,
            "contact_url": collection.contact_url,
            "source_url": collection.source_url,
        }
        version_status = (
            ReferenceValidationStatus.VALID
            if all(attribution_metadata.values())
            else ReferenceValidationStatus.PENDING_REVIEW
        )
        for index, item in enumerate(records):
            geometry = item["geometry"]
            coordinates = geometry.get("coordinates")
            if geometry.get("type") != "LineString" or not isinstance(coordinates, list):
                raise ValueError("Via Czechia geometries must be LineStrings")
            route = ReferenceRoute.objects.create(
                collection=collection,
                source_identifier=item["source_identifier"],
                route_number=str(item.get("route_number") or ""),
                title=item["title"],
                route_type=ReferenceRoute.RouteType.ROUTE
                if index == 0
                else ReferenceRoute.RouteType.STAGE,
                parent=parent,
                active=False,
                publication_status=ReferencePublicationStatus.PENDING,
            )
            version = ReferenceRouteVersion.objects.create(
                route=route,
                source_import=source_import,
                version_number=1,
                checksum=hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest(),
                source_geometry=geometry,
                normalized_geometry=_geometry_value(coordinates),
                provenance={"source": "Via Czechia", "snapshot_checksum": checksum},
                attribution=collection.attribution,
                attribution_metadata=attribution_metadata,
                validation_status=version_status,
                active=False,
            )
            route.current_version = version
            route.save(update_fields=("current_version", "updated_at"))
            if index == 0:
                parent = route
            imported += 1
        return {"status": "valid", "import_id": source_import.pk, "created": imported}
