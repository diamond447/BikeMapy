import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.db.models.deletion import ProtectedError
from django.test import Client, override_settings
from django.utils import timezone

from apps.api.views_reference_routes import reference_queryset
from apps.catalogue.fields import _GIS_AVAILABLE
from apps.reference_routes.models import (
    OSM_DISCOVERY_QUERY,
    ReferenceAlterationOffer,
    ReferenceCollection,
    ReferenceImport,
    ReferenceRecomputation,
    ReferenceRoute,
    ReferenceRouteVersion,
    ReferenceSourceKind,
    has_deployable_derivative_offer,
)
from apps.reference_routes.services import (
    _assemble_geometry,
    _validate_overpass_failure_evidence,
    _validate_retrieved_at,
    approve_reference_route,
    blocked_via_czechia_import,
    link_stage,
    record_alteration_offer,
    store_pending_snapshot,
    validate_osm_candidate,
)
from apps.reference_routes.services import (
    import_osm_snapshot as _import_osm_snapshot,
)
from apps.reference_routes.tasks import refresh_reference_routes

pytestmark = pytest.mark.django_db
OFFER_BASE_URL = "https://github.com/diamond447/BikeMapy"


@pytest.fixture(autouse=True)
def configure_test_offer_base(settings: Any) -> None:
    settings.REFERENCE_ROUTE_DERIVATIVE_OFFER_URL = OFFER_BASE_URL
    settings.REFERENCE_ROUTE_ALLOW_TEST_IMPORTS = True


def payload(*, changed: bool = False) -> dict[str, Any]:
    relation = {
        "type": "relation",
        "id": 101,
        "version": 2 if changed else 1,
        "timestamp": "2026-09-19T00:00:00Z",
        "changeset": 22 if changed else 21,
        "tags": {
            "type": "route",
            "route": "bicycle",
            "ref": "1",
            "network": "rcn",
            "name": "Brno 1",
            "operator": "Czech Cycling Routes",
        },
        "members": [{"type": "way", "ref": 501, "role": ""}],
    }
    way = {
        "type": "way",
        "id": 501,
        "version": 3,
        "nodes": [9001, 9002],
        "geometry": [{"lat": 49.2, "lon": 16.6}, {"lat": 49.3 if changed else 49.25, "lon": 16.7}],
    }
    nodes = [
        {"type": "node", "id": 9001, "lat": 49.2, "lon": 16.6},
        {"type": "node", "id": 9002, "lat": 49.3 if changed else 49.25, "lon": 16.7},
    ]
    return {
        "version": 0.6,
        "osm3s": {"timestamp_osm_base": "2026-09-19T00:00:00Z"},
        "elements": [relation, way, *nodes],
    }


def collection(*, slug: str = "osm-cz") -> ReferenceCollection:
    return ReferenceCollection.objects.create(
        slug=slug,
        name="OSM numbered routes",
        source_kind=ReferenceSourceKind.OSM_NUMBERED,
        source_url="https://www.openstreetmap.org",
        attribution="© OpenStreetMap contributors",
        licence="ODbL 1.0",
        attribution_text="© OpenStreetMap contributors",
        attribution_url="https://www.openstreetmap.org/copyright",
        licence_uri="https://opendatacommons.org/licenses/odbl/1-0/",
        derivative_offer_url="https://github.com/diamond447/BikeMapy/blob/main/docs/osm-alterations.md",
        rightsholder="OpenStreetMap contributors",
        contact_url="https://www.openstreetmap.org/fixthemap",
        permission_granted=True,
        active=True,
    )


def import_osm_snapshot(*args: Any, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("response_metadata", {"import_mode": "test"})
    return _import_osm_snapshot(*args, **kwargs)


def import_production_snapshot(
    item: ReferenceCollection, *, changed: bool = False, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    data = data or payload(changed=changed)
    relations = [element for element in data["elements"] if element["type"] == "relation"]
    relation_ids = [relation["id"] for relation in relations]
    executed_at = "2026-09-21T00:00:00Z"
    artifact_url = f"{OFFER_BASE_URL}/blob/main/discovery/{'-'.join(map(str, relation_ids))}.json"
    discovery_payload = {
        "version": 0.6,
        "osm3s": {"timestamp_osm_base": executed_at},
        "elements": relations,
    }
    discovery_bytes = json.dumps(
        discovery_payload, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return _import_osm_snapshot(
        collection=item,
        payload=data,
        response_metadata={
            "import_mode": "production",
            "production_import": True,
            "expected_relation_count": len(relation_ids),
            "expected_relation_ids": relation_ids,
            "discovery_mechanism": "overpass",
            "discovery_query_or_extract_id": (
                f"overpass:{hashlib.sha256(OSM_DISCOVERY_QUERY.encode()).hexdigest()}"
            ),
            "discovery_executed_at": executed_at,
            "discovery_result_sha256": hashlib.sha256(discovery_bytes).hexdigest(),
            "discovery_artifact_url": artifact_url,
            "discovery_selected_relation_ids": relation_ids,
            "discovery_artifact_content_base64": base64.b64encode(discovery_bytes).decode(),
        },
    )


def publish_offer(item: ReferenceCollection) -> None:
    source_import = item.imports.order_by("-pk").first()
    assert source_import is not None
    record_alteration_offer(
        source_import=source_import,
        manifest_url=f"{OFFER_BASE_URL}/blob/main/manifests/{source_import.raw_response_sha256}.json",
        artifact_url=f"{OFFER_BASE_URL}/blob/main/artifacts/{source_import.raw_response_sha256}.json",
        method_url=f"{OFFER_BASE_URL}/blob/main/docs/osm-alterations.md",
        published_at=timezone.now(),
        offered_snapshot_hash=source_import.raw_response_sha256,
        operator_evidence=(
            "Reviewed and published the complete immutable snapshot, output, and method."
        ),
    )


def test_import_is_idempotent_and_changed_geometry_schedules_recomputation() -> None:
    item = collection()
    first = import_production_snapshot(item)
    assert first["created"] == 1
    unchanged = import_production_snapshot(item)
    assert unchanged["status"] == "unchanged"
    changed = import_production_snapshot(item, changed=True)
    assert changed["created"] == 1
    route = ReferenceRoute.objects.get(collection=item)
    assert route.current_version is not None
    assert route.current_version.provenance["relation_version"] == 2
    assert route.current_version.provenance["relation_timestamp"] == "2026-09-19T00:00:00Z"
    assert route.current_version.provenance["relation_changeset"] == 22
    publish_offer(item)
    approve_reference_route(route, reviewer="reviewer@example.invalid")
    assert route.versions.count() == 2
    assert ReferenceRecomputation.objects.filter(route_version=route.current_version).exists()
    assert ReferenceRouteVersion.objects.filter(route=route, active=True).count() == 1


def test_invalid_record_isolated_with_diagnostics() -> None:
    item = collection()
    data = payload()
    data["elements"][0]["tags"]["network"] = "icn"
    result = import_osm_snapshot(collection=item, payload=data)
    assert result["invalid"] == 1
    assert item.routes.count() == 0
    assert item.imports.get().diagnostics[0]["diagnostics"][0]["code"] == "unsupported_network"


def test_via_czechia_is_blocked_without_request() -> None:
    item = ReferenceCollection.objects.create(
        slug="via",
        name="Via Czechia",
        source_kind=ReferenceSourceKind.VIA_CZECHIA,
        source_url="https://viaczechia.cz",
        attribution="Via Czechia",
        licence="CC BY-NC-SA 4.0",
    )
    assert blocked_via_czechia_import(collection=item)["status"] == "blocked"


def test_source_gate_blocks_import_and_activation() -> None:
    item = collection()
    item.permission_granted = False
    item.save(update_fields=["permission_granted", "updated_at"])
    with pytest.raises(PermissionError):
        import_osm_snapshot(collection=item, payload=payload())
    route = ReferenceRoute(
        collection=item, source_identifier="blocked", title="Blocked", active=True
    )
    with pytest.raises(ValidationError):
        route.save()


@override_settings(REFERENCE_ROUTE_DERIVATIVE_OFFER_URL="")
def test_missing_deployable_alteration_offer_blocks_import_and_activation() -> None:
    item = collection()
    import_osm_snapshot(collection=item, payload=payload())
    version = ReferenceRouteVersion.objects.get(route__collection=item)
    route = ReferenceRoute(
        collection=item,
        source_identifier="missing-offer",
        title="Missing offer",
        active=True,
        current_version=version,
    )
    with pytest.raises(ValidationError, match="alteration offer"):
        route.save()


def test_alteration_offer_is_immutable_hash_bound_and_required_per_version() -> None:
    item = collection()
    import_production_snapshot(item)
    source_import = item.imports.get()
    with pytest.raises(ValueError, match="hash"):
        record_alteration_offer(
            source_import=source_import,
            manifest_url=f"{OFFER_BASE_URL}/manifest.json",
            artifact_url=f"{OFFER_BASE_URL}/artifact.json",
            method_url=f"{OFFER_BASE_URL}/method.md",
            published_at=timezone.now(),
            offered_snapshot_hash="0" * 64,
            operator_evidence="evidence",
        )
    publish_offer(item)
    offer = source_import.alteration_offer
    with pytest.raises(ValidationError):
        offer.operator_evidence = "changed"
        offer.save()
    route = ReferenceRoute.objects.get(collection=item)
    approve_reference_route(route, reviewer="reviewer")
    import_production_snapshot(item, changed=True)
    route.refresh_from_db()
    assert route.publication_status == "pending"
    with pytest.raises(ValidationError, match="alteration offer"):
        approve_reference_route(route, reviewer="reviewer")


def test_validation_sample_requires_test_mode_and_cannot_activate(tmp_path: Any) -> None:
    item = collection()
    payload_path = tmp_path / "snapshot.json"
    manifest_path = tmp_path / "snapshot.manifest.json"
    payload_path.write_bytes(
        Path("backend/apps/reference_routes/fixtures/osm-cz-representative.json").read_bytes()
    )
    manifest_path.write_bytes(
        Path(
            "backend/apps/reference_routes/fixtures/osm-cz-representative.manifest.json"
        ).read_bytes()
    )
    with pytest.raises(CommandError, match="validation-test-mode"):
        call_command(
            "refresh_reference_routes",
            "--collection",
            item.slug,
            "--payload-file",
            payload_path,
            "--manifest",
            manifest_path,
        )
    call_command(
        "refresh_reference_routes",
        "--collection",
        item.slug,
        "--payload-file",
        payload_path,
        "--manifest",
        manifest_path,
        "--validation-test-mode",
    )
    source_import = item.imports.get()
    assert source_import.response_metadata["validation_sample"] is True
    with pytest.raises(ValueError, match="production imports"):
        publish_offer(item)
    version = ReferenceRouteVersion.objects.get(source_import=source_import)
    route = version.route
    route.current_version = version
    route.active = True
    with pytest.raises(ValidationError):
        route.save(update_fields=["current_version", "active", "updated_at"])


def test_production_import_requires_non_circular_discovery_evidence() -> None:
    item = collection()
    data = payload()
    with pytest.raises(ValueError, match="explicit import_mode"):
        import_osm_snapshot(collection=item, payload=data, response_metadata={})
    with pytest.raises(ValueError, match="discovery evidence"):
        import_osm_snapshot(
            collection=item,
            payload=data,
            response_metadata={
                "production_import": True,
                "import_mode": "production",
                "expected_relation_ids": [101],
                "expected_relation_count": 1,
            },
        )


def test_production_discovery_artifact_and_hash_are_verifiable() -> None:
    item = collection()
    relation = payload()["elements"][0]
    executed_at = "2026-09-21T00:00:00Z"
    discovery_payload = {
        "version": 0.6,
        "osm3s": {"timestamp_osm_base": executed_at},
        "elements": [relation],
    }
    discovery_bytes = json.dumps(discovery_payload, separators=(",", ":")).encode()
    artifact_url = "https://www.openstreetmap.org/api/0.6/relation/101/full.json"
    metadata: dict[str, Any] = {
        "import_mode": "production",
        "production_import": True,
        "expected_relation_ids": [101],
        "expected_relation_count": 1,
        "discovery_mechanism": "overpass",
        "discovery_query_or_extract_id": "overpass-query-v1",
        "discovery_executed_at": executed_at,
        "discovery_result_sha256": hashlib.sha256(discovery_bytes).hexdigest(),
        "discovery_artifact_url": artifact_url,
        "discovery_selected_relation_ids": [101],
        "discovery_artifact_content_base64": base64.b64encode(discovery_bytes).decode(),
    }
    metadata["discovery_artifact_url"] = f"{OFFER_BASE_URL}/blob/main/docs/osm-alterations.md"
    with pytest.raises(ValueError, match="separate trusted artifact"):
        import_osm_snapshot(collection=item, payload=payload(), response_metadata=metadata)
    metadata["discovery_artifact_url"] = (
        "https://www.openstreetmap.org/api/0.6/relation/101/full.json"
    )
    with pytest.raises(ValueError, match="separate trusted artifact"):
        import_osm_snapshot(collection=item, payload=payload(), response_metadata=metadata)
    metadata["discovery_artifact_url"] = f"{OFFER_BASE_URL}/blob/main/discovery/101.json"
    with pytest.raises(ValueError, match="approved query"):
        import_osm_snapshot(collection=item, payload=payload(), response_metadata=metadata)


def test_discovery_artifact_provenance_and_duplicate_ids_are_rejected() -> None:
    item = collection()
    data = payload()
    relation = data["elements"][0]
    discovery_payload = {
        "version": 0.6,
        "osm3s": {"timestamp_osm_base": "2026-09-21T00:00:00Z"},
        "elements": [dict(relation, version=99)],
    }
    content = json.dumps(discovery_payload, separators=(",", ":")).encode()
    metadata: dict[str, Any] = {
        "import_mode": "production",
        "production_import": True,
        "expected_relation_ids": [101],
        "expected_relation_count": 1,
        "discovery_mechanism": "overpass",
        "discovery_query_or_extract_id": (
            f"overpass:{hashlib.sha256(OSM_DISCOVERY_QUERY.encode()).hexdigest()}"
        ),
        "discovery_executed_at": "2026-09-21T00:00:00Z",
        "discovery_result_sha256": hashlib.sha256(content).hexdigest(),
        "discovery_artifact_url": f"{OFFER_BASE_URL}/blob/main/discovery/101.json",
        "discovery_selected_relation_ids": [101],
        "discovery_artifact_content_base64": base64.b64encode(content).decode(),
    }
    with pytest.raises(ValueError, match="provenance"):
        import_osm_snapshot(collection=item, payload=data, response_metadata=metadata)
    discovery_payload["elements"] = [relation, relation]
    content = json.dumps(discovery_payload, separators=(",", ":")).encode()
    metadata["discovery_result_sha256"] = hashlib.sha256(content).hexdigest()
    metadata["discovery_artifact_content_base64"] = base64.b64encode(content).decode()
    with pytest.raises(ValueError, match="duplicate relation IDs"):
        import_osm_snapshot(collection=item, payload=data, response_metadata=metadata)


def test_overpass_failure_log_is_structured_and_status_consistent() -> None:
    evidence: dict[str, Any] = {
        "endpoint": "https://overpass-api.de/api/interpreter",
        "query_text": OSM_DISCOVERY_QUERY,
        "attempted_at": "2026-09-21T00:00:00Z",
        "network_status": "http_error",
        "http_status": 403,
        "error": "Overpass returned HTTP 403",
    }
    evidence["log_base64"] = base64.b64encode(
        json.dumps(evidence, separators=(",", ":")).encode()
    ).decode()
    evidence["log_sha256"] = hashlib.sha256(base64.b64decode(evidence["log_base64"])).hexdigest()
    _validate_overpass_failure_evidence(evidence)
    evidence["log_base64"] = base64.b64encode(b"arbitrary text").decode()
    evidence["log_sha256"] = hashlib.sha256(base64.b64decode(evidence["log_base64"])).hexdigest()
    with pytest.raises(ValueError, match="not valid JSON"):
        _validate_overpass_failure_evidence(evidence)
    evidence["network_status"] = "network_error"
    evidence["http_status"] = 200
    evidence["log_base64"] = base64.b64encode(
        json.dumps(evidence, separators=(",", ":")).encode()
    ).decode()
    evidence["log_sha256"] = hashlib.sha256(base64.b64decode(evidence["log_base64"])).hexdigest()
    with pytest.raises(ValueError, match="cannot contain"):
        _validate_overpass_failure_evidence(evidence)

    evidence["network_status"] = "http_error"
    evidence["http_status"] = 403
    evidence["attempted_at"] = "2999-01-01T00:00:00Z"
    evidence["log_base64"] = base64.b64encode(
        json.dumps(evidence, separators=(",", ":")).encode()
    ).decode()
    evidence["log_sha256"] = hashlib.sha256(base64.b64decode(evidence["log_base64"])).hexdigest()
    with pytest.raises(ValueError, match="future"):
        _validate_overpass_failure_evidence(evidence)
    evidence["attempted_at"] = "2026-09-21T00:01:00Z"
    evidence["log_base64"] = base64.b64encode(
        json.dumps(evidence, separators=(",", ":")).encode()
    ).decode()
    evidence["log_sha256"] = hashlib.sha256(base64.b64decode(evidence["log_base64"])).hexdigest()
    with pytest.raises(ValueError, match="precede discovery"):
        _validate_overpass_failure_evidence(
            evidence, discovery_executed_at=datetime.fromisoformat("2026-09-21T00:00:00+00:00")
        )


def test_publication_gate_rechecks_mutated_evidence_without_stale_cache() -> None:
    item = collection()
    import_production_snapshot(item)
    publish_offer(item)
    source_import = item.imports.get()
    assert has_deployable_derivative_offer(item, source_import)
    source_import.response_metadata["discovery_result_sha256"] = "0" * 64
    assert not has_deployable_derivative_offer(item, source_import)
    source_import.response_metadata["discovery_result_sha256"] = hashlib.sha256(
        base64.b64decode(source_import.response_metadata["discovery_artifact_content_base64"])
    ).hexdigest()
    source_import.response_metadata["discovery_executed_at"] = "2999-01-01T00:00:00Z"
    assert not has_deployable_derivative_offer(item, source_import)


def test_future_retrieval_is_rejected_at_import_and_publication_boundary() -> None:
    item = collection()
    future = datetime.fromisoformat("2999-01-01T00:00:00+00:00")
    with pytest.raises(ValueError, match="retrieved_at cannot be in the future"):
        import_osm_snapshot(collection=item, payload=payload(), retrieved_at=future)
    now = timezone.now()
    assert _validate_retrieved_at(now) == now.astimezone(UTC)
    import_production_snapshot(item)
    publish_offer(item)
    source_import = item.imports.get()
    source_import.retrieved_at = future
    assert not has_deployable_derivative_offer(item, source_import)


def test_test_mode_is_never_deployable_even_when_enabled_for_fixtures() -> None:
    item = collection()
    import_osm_snapshot(collection=item, payload=payload())
    with pytest.raises(ValueError, match="production imports"):
        publish_offer(item)
    route = ReferenceRoute.objects.get(collection=item)
    route.active = True
    route.publication_status = "approved"
    with pytest.raises(ValidationError, match="blocked reference source"):
        route.save(update_fields=["active", "publication_status", "updated_at"])


@override_settings(REFERENCE_ROUTE_ALLOW_TEST_IMPORTS=False)
def test_test_mode_imports_are_disabled_without_explicit_runtime_setting() -> None:
    item = collection()
    with pytest.raises(ValueError, match="Test imports are disabled"):
        import_osm_snapshot(collection=item, payload=payload())


def test_regional_discovery_rejects_bare_overpass_unavailable_flag() -> None:
    item = collection()
    data = payload()
    executed_at = "2026-09-21T00:00:00Z"
    discovery_payload = {
        "version": 0.6,
        "osm3s": {"timestamp_osm_base": executed_at},
        "elements": [data["elements"][0]],
    }
    discovery_bytes = json.dumps(discovery_payload, separators=(",", ":")).encode()
    with pytest.raises(ValueError, match="failure evidence"):
        import_osm_snapshot(
            collection=item,
            payload=data,
            response_metadata={
                "import_mode": "production",
                "production_import": True,
                "expected_relation_ids": [101],
                "expected_relation_count": 1,
                "discovery_mechanism": "regional_extract",
                "discovery_query_or_extract_id": "regional-extract:cz-2026-09",
                "discovery_executed_at": executed_at,
                "discovery_result_sha256": hashlib.sha256(discovery_bytes).hexdigest(),
                "discovery_artifact_url": f"{OFFER_BASE_URL}/blob/main/discovery/101.json",
                "discovery_selected_relation_ids": [101],
                "discovery_artifact_content_base64": base64.b64encode(discovery_bytes).decode(),
                "overpass_unavailable": True,
            },
        )


def test_direct_incomplete_offer_and_sibling_origin_never_publish_or_appear() -> None:
    item = collection()
    import_production_snapshot(item)
    source_import = item.imports.get()
    with pytest.raises(ValidationError):
        ReferenceAlterationOffer.objects.create(
            source_import=source_import,
            manifest_url=f"{OFFER_BASE_URL}/manifest.json",
            artifact_url=f"{OFFER_BASE_URL}/artifact.json",
            method_url=f"{OFFER_BASE_URL}/method.md",
            published_at=timezone.now(),
            offered_snapshot_hash="0" * 64,
            operator_evidence="bypass",
        )
    publish_offer(item)
    item.derivative_offer_url = (
        "https://github.com/diamond447/BikeMapy-evil/docs/osm-alterations.md"
    )
    item.save(update_fields=["derivative_offer_url", "updated_at"])
    route = ReferenceRoute.objects.get(collection=item)
    route.publication_status = "approved"
    route.active = True
    with pytest.raises(ValidationError):
        route.save(update_fields=["publication_status", "active", "updated_at"])


def test_bulk_created_validation_route_is_filtered_from_public_queryset() -> None:
    item = collection()
    import_osm_snapshot(
        collection=item,
        payload=payload(),
        response_metadata={
            "import_mode": "validation",
            "validation_sample": True,
            "validation_test_mode": True,
        },
    )
    version = ReferenceRouteVersion.objects.get()
    route = ReferenceRoute(
        collection=item,
        source_identifier="bulk-validation",
        title="Bulk validation",
        active=True,
        publication_status="approved",
        current_version=version,
    )
    ReferenceRoute.objects.bulk_create([route])
    assert not reference_queryset().filter(pk=route.pk).exists()


def test_operator_and_human_review_are_required_before_activation() -> None:
    item = collection()
    data = payload()
    del data["elements"][0]["tags"]["operator"]
    result = import_osm_snapshot(collection=item, payload=data)
    assert result["invalid"] == 1
    assert item.routes.count() == 0


def test_invalid_refresh_does_not_replace_published_route() -> None:
    item = collection()
    import_production_snapshot(item)
    route = ReferenceRoute.objects.get(collection=item)
    publish_offer(item)
    approve_reference_route(route, reviewer="reviewer")
    current_id = route.current_version_id
    invalid = payload(changed=True)
    invalid["elements"][0]["tags"]["network"] = "icn"
    result = import_osm_snapshot(collection=item, payload=invalid)
    route.refresh_from_db()
    assert result["invalid"] == 1
    assert route.current_version_id == current_id
    assert route.active is True
    assert route.publication_status == "approved"


def test_raw_bytes_and_snapshot_are_immutable() -> None:
    item = collection()
    raw = (
        b'{"elements":[{"type":"relation","id":101,"tags":'
        b'{"route":"bicycle","ref":"1","network":"rcn",'
        b'"operator":"operator"},"members":[]}]}'
    )
    result = import_osm_snapshot(collection=item, payload=raw)
    source_import = item.imports.get(pk=result["import_id"])
    assert bytes(source_import.raw_response) == raw
    source_import.query_text = "changed"
    with pytest.raises(ValidationError):
        source_import.save()


def test_bulk_updates_cannot_rewrite_import_or_version_content() -> None:
    item = collection()
    result = import_osm_snapshot(collection=item, payload=payload())
    source_import = ReferenceImport.objects.get(pk=result["import_id"])
    version = ReferenceRouteVersion.objects.get()
    with pytest.raises(ValidationError):
        ReferenceImport.objects.filter(pk=source_import.pk).update(raw_response=b"changed")
    with pytest.raises(ValidationError):
        ReferenceRouteVersion.objects.filter(pk=version.pk).update(checksum="changed")
    route = ReferenceRoute.objects.get()
    with pytest.raises(ValidationError):
        ReferenceRoute.objects.filter(pk=route.pk).update(active=True)
    with pytest.raises(ProtectedError):
        ReferenceRoute.objects.filter(pk=route.pk).delete()
    with pytest.raises(ValidationError):
        ReferenceRouteVersion.objects.filter(pk=version.pk).update(active=True)


def test_valid_version_requires_structured_attribution_metadata() -> None:
    item = collection()
    import_osm_snapshot(collection=item, payload=payload())
    route = ReferenceRoute.objects.get()
    source_import = ReferenceImport.objects.get()
    with pytest.raises(ValidationError):
        ReferenceRouteVersion.objects.create(
            route=route,
            source_import=source_import,
            version_number=99,
            checksum="missing-attribution",
            source_geometry={},
            attribution="OSM",
            validation_status="valid",
        )


def test_osm_urls_and_attribution_offer_are_exact_and_deployable() -> None:
    item = collection()
    item.attribution_url = "https://evil.example/openstreetmap.org/copyright"
    with pytest.raises(ValidationError):
        item.full_clean()
    item.refresh_from_db()
    item.derivative_offer_url = (
        "https://github.com/diamond447/BikeMapy/tree/main/docs/osm-alterations.md"
    )
    with pytest.raises(ValidationError):
        item.full_clean()


def test_internal_zero_length_geometry_is_rejected() -> None:
    relation = {"members": [{"type": "way", "ref": 1, "role": ""}]}
    ways = {
        1: {
            "geometry": [
                {"lon": 16.4, "lat": 49.2},
                {"lon": 16.4, "lat": 49.2},
                {"lon": 16.5, "lat": 49.2},
            ]
        }
    }
    _, diagnostics, _ = _assemble_geometry(relation, ways)
    assert any(item.code == "zero_length" for item in diagnostics)


def test_operator_command_and_celery_task_use_bounded_stored_snapshot(tmp_path: Any) -> None:
    item = collection()
    path = tmp_path / "snapshot.json"
    manifest_path = tmp_path / "snapshot.manifest.json"
    fixture_path = Path("backend/apps/reference_routes/fixtures/osm-cz-representative.json")
    fixture_manifest_path = Path(
        "backend/apps/reference_routes/fixtures/osm-cz-representative.manifest.json"
    )
    path.write_bytes(fixture_path.read_bytes())
    manifest_path.write_bytes(fixture_manifest_path.read_bytes())
    call_command(
        "refresh_reference_routes",
        "--collection",
        item.slug,
        "--payload-file",
        path,
        "--manifest",
        manifest_path,
        "--validation-test-mode",
    )
    source_import = item.imports.get()
    assert source_import.status == "valid"
    assert source_import.response_metadata["http_headers"]["content-encoding"] == "identity"
    assert source_import.source_timestamp is not None
    pending = store_pending_snapshot(
        collection=item,
        raw_response=json.dumps(payload(), separators=(",", ":")).encode(),
        endpoint="https://www.openstreetmap.org/api/0.6/relation/7689870/full.json",
        query_text="query",
        retrieved_at=timezone.now(),
        response_metadata={
            "complete": True,
            "import_mode": "test",
            "expected_relation_count": 1,
            "expected_relation_ids": [101],
            "http_status": 200,
        },
    )
    task_result = refresh_reference_routes.apply(args=[pending.pk]).get()
    assert task_result["import_id"] == pending.pk
    assert task_result["created"] == 1


def test_manifest_fails_closed_for_http_error_and_missing_evidence(tmp_path: Any) -> None:
    item = collection()
    payload_path = tmp_path / "snapshot.json"
    manifest_path = tmp_path / "snapshot.manifest.json"
    payload_path.write_bytes(
        Path("backend/apps/reference_routes/fixtures/osm-cz-representative.json").read_bytes()
    )
    manifest = json.loads(
        Path(
            "backend/apps/reference_routes/fixtures/osm-cz-representative.manifest.json"
        ).read_text()
    )
    manifest["http_status"] = 500
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(CommandError, match="HTTP status"):
        call_command(
            "refresh_reference_routes",
            "--collection",
            item.slug,
            "--payload-file",
            payload_path,
            "--manifest",
            manifest_path,
            "--validation-test-mode",
        )
    manifest.pop("source_timestamp")
    manifest["http_status"] = 200
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(CommandError, match="missing required fields"):
        call_command(
            "refresh_reference_routes",
            "--collection",
            item.slug,
            "--payload-file",
            payload_path,
            "--manifest",
            manifest_path,
            "--validation-test-mode",
        )
    assert not item.imports.exists()


def test_manifest_rejects_future_retrieval_timestamp(tmp_path: Any) -> None:
    item = collection()
    payload_path = tmp_path / "snapshot.json"
    manifest_path = tmp_path / "snapshot.manifest.json"
    payload_path.write_bytes(
        Path("backend/apps/reference_routes/fixtures/osm-cz-representative.json").read_bytes()
    )
    manifest = json.loads(
        Path(
            "backend/apps/reference_routes/fixtures/osm-cz-representative.manifest.json"
        ).read_text()
    )
    manifest["retrieved_at"] = "2999-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(CommandError, match="retrieved_at cannot be in the future"):
        call_command(
            "refresh_reference_routes",
            "--collection",
            item.slug,
            "--payload-file",
            payload_path,
            "--manifest",
            manifest_path,
            "--validation-test-mode",
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "relation_version",
        "relation_changeset",
        "relation_timestamp",
        "expected_way_count",
        "expected_node_count",
        "source_timestamp",
        "retrieved_at",
        "content_type",
        "header_absence",
        "payload_node_count",
        "payload_source_timestamp",
    ],
)
def test_manifest_evidence_fields_fail_closed(tmp_path: Any, mutation: str) -> None:
    item = collection()
    payload_data = json.loads(
        Path("backend/apps/reference_routes/fixtures/osm-cz-representative.json").read_text()
    )
    manifest = json.loads(
        Path(
            "backend/apps/reference_routes/fixtures/osm-cz-representative.manifest.json"
        ).read_text()
    )
    if mutation == "relation_version":
        manifest["relation_version"] += 1
    elif mutation == "relation_changeset":
        manifest["relation_changeset"] += 1
    elif mutation == "relation_timestamp":
        manifest["relation_timestamp"] = "2026-09-17T07:11:42Z"
    elif mutation == "expected_way_count":
        manifest["expected_way_count"] += 1
    elif mutation == "expected_node_count":
        manifest["expected_node_count"] += 1
    elif mutation == "source_timestamp":
        manifest["source_timestamp"] = "2026-09-17T07:11:42Z"
    elif mutation == "retrieved_at":
        manifest["retrieved_at"] = "2026-09-20T21:36:47"
    elif mutation == "content_type":
        manifest["http_headers"]["content-type"] = ""
    elif mutation == "header_absence":
        manifest["http_header_absence"] = ["etag"]
    elif mutation == "payload_node_count":
        removed = False
        retained = []
        for element in payload_data["elements"]:
            if not removed and element.get("type") == "node":
                removed = True
                continue
            retained.append(element)
        payload_data["elements"] = retained
    elif mutation == "payload_source_timestamp":
        payload_data["elements"][-1]["timestamp"] = "2026-09-17T07:11:42Z"
        manifest["relation_timestamp"] = "2026-09-17T07:11:42Z"
    else:  # pragma: no cover
        raise AssertionError(mutation)
    payload_path = tmp_path / "snapshot.json"
    manifest_path = tmp_path / "snapshot.manifest.json"
    payload_bytes = (
        Path("backend/apps/reference_routes/fixtures/osm-cz-representative.json").read_bytes()
        if mutation not in {"payload_node_count", "payload_source_timestamp"}
        else json.dumps(payload_data, separators=(",", ":")).encode()
    )
    if mutation in {"payload_node_count", "payload_source_timestamp"}:
        manifest["sha256"] = hashlib.sha256(payload_bytes).hexdigest()
    payload_path.write_bytes(payload_bytes)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(CommandError):
        call_command(
            "refresh_reference_routes",
            "--collection",
            item.slug,
            "--payload-file",
            payload_path,
            "--manifest",
            manifest_path,
        )
    assert not item.imports.exists()


def _apply_reviewed_mutation(data: dict[str, Any], mutation: dict[str, Any]) -> None:
    relation = next(element for element in data["elements"] if element.get("type") == "relation")
    members = relation["members"]
    ways = [element for element in data["elements"] if element.get("type") == "way"]

    def member_way(index: int) -> dict[str, Any]:
        way_id = members[index]["ref"]
        return next(way for way in ways if way["id"] == way_id)

    operation = mutation["operation"]
    if operation == "remove_way":
        way_id = members[mutation["member_index"]]["ref"]
        data["elements"] = [
            element
            for element in data["elements"]
            if not (element.get("type") == "way" and element.get("id") == way_id)
        ]
    elif operation == "remove_node":
        node_id = member_way(mutation["member_index"])["nodes"][0]
        data["elements"] = [
            element
            for element in data["elements"]
            if not (element.get("type") == "node" and element.get("id") == node_id)
        ]
    elif operation == "duplicate_member":
        members.append(dict(members[mutation["member_index"]]))
    elif operation == "copy_way_nodes":
        source = member_way(mutation["source_member_index"])
        target = member_way(mutation["target_member_index"])
        target["nodes"] = list(source["nodes"])
    elif operation == "replace_way_geometry":
        member_way(mutation["member_index"])["geometry"] = [
            {"lon": 12.1, "lat": 48.6},
            {"lon": 12.2, "lat": 48.6},
        ]
    elif operation == "reverse_members":
        relation["members"] = list(reversed(members))
    elif operation == "signed_reverse_members":
        relation["tags"]["signed_direction"] = "yes"
        relation["members"] = [{**member, "role": "forward"} for member in reversed(members)]
    elif operation == "reverse_way_nodes":
        way = member_way(mutation["member_index"])
        way["nodes"] = list(reversed(way["nodes"]))
    elif operation == "forward_reverse_way":
        relation["tags"]["signed_direction"] = "yes"
        for member in members:
            member["role"] = "forward"
        way = member_way(mutation["member_index"])
        way["nodes"] = list(reversed(way["nodes"]))
    elif operation == "empty_members":
        relation["members"] = []
    else:  # pragma: no cover
        raise AssertionError(operation)


def test_reviewed_mutation_bundle_covers_geometry_gates() -> None:
    fixture_path = Path("backend/apps/reference_routes/fixtures/osm-cz-representative.json")
    manifest_path = Path(
        "backend/apps/reference_routes/fixtures/osm-cz-representative-mutations.manifest.json"
    )
    base_bytes = fixture_path.read_bytes()
    manifest = json.loads(manifest_path.read_text())
    assert hashlib.sha256(base_bytes).hexdigest() == manifest["base_sha256"]
    base = json.loads(base_bytes)
    for mutation in manifest["mutations"]:
        item = collection(slug=f"osm-cz-{mutation['label']}")
        data = json.loads(json.dumps(base))
        _apply_reviewed_mutation(data, mutation)
        result = import_osm_snapshot(collection=item, payload=data)
        source_import = ReferenceImport.objects.get(collection=item)
        if mutation["expected"] == "accept":
            assert result["created"] == 1
            assert not source_import.diagnostics
        else:
            assert result["invalid"] == 1
            diagnostics = {entry["code"] for entry in source_import.diagnostics[0]["diagnostics"]}
            assert set(mutation["expected_diagnostics"]) <= diagnostics


def test_duplicate_missing_and_disconnected_members_are_diagnostics() -> None:
    item = collection()
    data = payload()
    data["elements"][0]["members"] = [
        {"type": "way", "ref": 501, "role": "forward"},
        {"type": "way", "ref": 501, "role": "backward"},
        {"type": "way", "ref": 999, "role": "forward"},
    ]
    result = import_osm_snapshot(collection=item, payload=data)
    assert result["invalid"] == 1
    codes = {item["code"] for item in ReferenceImport.objects.get().diagnostics[0]["diagnostics"]}
    assert {"duplicate_member", "missing_way"} <= codes


def test_incomplete_snapshot_is_rejected_without_routes() -> None:
    item = collection()
    result = import_osm_snapshot(
        collection=item,
        payload=payload(),
        response_metadata={"complete": False, "import_mode": "test"},
    )
    assert result["status"] == "failed"
    assert item.routes.count() == 0
    assert item.imports.get().diagnostics[0]["code"] == "incomplete_snapshot"


def test_recursive_child_relations_are_not_imported_as_routes() -> None:
    item = collection()
    data = payload()
    data["elements"].append(
        {
            "type": "relation",
            "id": 102,
            "version": 1,
            "tags": {"route": "bicycle", "ref": "2", "network": "rcn", "operator": "operator"},
            "members": [{"type": "way", "ref": 501, "role": "stage"}],
        }
    )
    data["elements"][0]["members"].append({"type": "relation", "ref": 102, "role": "stage"})
    import_osm_snapshot(collection=item, payload=data)
    assert list(item.routes.values_list("source_identifier", flat=True)) == ["osm-relation:101"]


def test_manifest_selected_child_relation_is_imported_exactly() -> None:
    item = collection()
    data = payload()
    data["elements"].append(
        {
            "type": "relation",
            "id": 102,
            "version": 1,
            "tags": {"route": "bicycle", "ref": "2", "network": "rcn", "operator": "operator"},
            "members": [{"type": "way", "ref": 501, "role": "stage"}],
        }
    )
    data["elements"][0]["members"].append({"type": "relation", "ref": 102, "role": "stage"})
    result = import_osm_snapshot(
        collection=item,
        payload=data,
        response_metadata={
            "import_mode": "test",
            "expected_relation_count": 2,
            "expected_relation_ids": [101, 102],
            "http_status": 200,
            "raw_sha256": hashlib.sha256(
                json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
    )
    assert result["created"] == 2
    assert item.routes.count() == 2


def test_stage_linking_rejects_cycles() -> None:
    item = collection()
    parent = ReferenceRoute.objects.create(
        collection=item, source_identifier="parent", title="Parent"
    )
    stage = ReferenceRoute.objects.create(collection=item, source_identifier="stage", title="Stage")
    link_stage(stage=stage, parent=parent)
    with pytest.raises(ValueError):
        link_stage(stage=parent, parent=stage)


@pytest.mark.parametrize(
    ("tags", "code"),
    [
        ({"network": "ICN", "ref": "1", "operator": "x"}, "unsupported_network"),
        ({"network": "rcn", "ref": "EV-1", "operator": "x"}, "international_ref"),
        (
            {"network": "rcn", "ref": "1", "name": "EuroVelo1", "operator": "x"},
            "international_marker",
        ),
        ({"network": "rcn", "ref": "1", "operator": ""}, "missing_operator"),
    ],
)
def test_allow_deny_normalization_matrix(tags: dict[str, Any], code: str) -> None:
    assert code in {diagnostic.code for diagnostic in validate_osm_candidate(tags)}


def test_graph_assembly_handles_internal_minimum_and_undirected_reversal() -> None:
    relation = {
        "tags": {"signed_direction": "no"},
        "members": [
            {"type": "way", "ref": 2, "role": ""},
            {"type": "way", "ref": 1, "role": ""},
            {"type": "way", "ref": 3, "role": ""},
        ],
    }
    ways = {
        1: {"geometry": [{"lon": 16.5, "lat": 49.2}, {"lon": 16.4, "lat": 49.2}]},
        2: {"geometry": [{"lon": 16.6, "lat": 49.2}, {"lon": 16.5, "lat": 49.2}]},
        3: {"geometry": [{"lon": 16.6, "lat": 49.2}, {"lon": 16.7, "lat": 49.2}]},
    }
    coordinates, diagnostics, _ = _assemble_geometry(relation, ways)
    assert not diagnostics
    assert coordinates == [[16.4, 49.2], [16.5, 49.2], [16.6, 49.2], [16.7, 49.2]]


def test_signed_roles_cannot_be_reversed_and_closed_loop_is_deterministic() -> None:
    signed = {
        "tags": {"signed_direction": "yes"},
        "members": [
            {"type": "way", "ref": 1, "role": "forward"},
            {"type": "way", "ref": 2, "role": "forward"},
        ],
    }
    ways = {
        1: {"geometry": [{"lon": 16.5, "lat": 49.2}, {"lon": 16.4, "lat": 49.2}]},
        2: {"geometry": [{"lon": 16.6, "lat": 49.2}, {"lon": 16.5, "lat": 49.2}]},
    }
    _, diagnostics, _ = _assemble_geometry(signed, ways)
    assert any(item.code == "signed_direction_order" for item in diagnostics)
    loop = {
        "members": [
            {"type": "way", "ref": 1, "role": ""},
            {"type": "way", "ref": 2, "role": ""},
            {"type": "way", "ref": 3, "role": ""},
        ]
    }
    loop_ways = {
        1: {"geometry": [{"lon": 16.4, "lat": 49.2}, {"lon": 16.5, "lat": 49.2}]},
        2: {"geometry": [{"lon": 16.5, "lat": 49.2}, {"lon": 16.4, "lat": 49.3}]},
        3: {"geometry": [{"lon": 16.4, "lat": 49.3}, {"lon": 16.4, "lat": 49.2}]},
    }
    coordinates, diagnostics, _ = _assemble_geometry(loop, loop_ways)
    assert not diagnostics
    assert coordinates is not None and coordinates[0] == [16.4, 49.2]


def test_signed_member_order_is_preserved_and_wrong_order_reports_way_ids() -> None:
    relation = {
        "tags": {"signed_direction": "yes"},
        "members": [
            {"type": "way", "ref": 2, "role": "forward"},
            {"type": "way", "ref": 1, "role": "forward"},
        ],
    }
    ways = {
        1: {"geometry": [{"lon": 16.4, "lat": 49.2}, {"lon": 16.5, "lat": 49.2}]},
        2: {"geometry": [{"lon": 16.5, "lat": 49.2}, {"lon": 16.6, "lat": 49.2}]},
    }
    coordinates, diagnostics, _ = _assemble_geometry(relation, ways)
    assert coordinates is None
    order = next(item for item in diagnostics if item.code == "signed_direction_order")
    assert order.details == {
        "previous_way_id": 2,
        "offending_way_id": 1,
        "expected_endpoint": [16.6, 49.2],
        "actual_endpoint": [16.4, 49.2],
    }


def test_self_intersection_and_internal_duplicate_are_rejected() -> None:
    relation = {"members": [{"type": "way", "ref": 1, "role": ""}]}
    ways = {
        1: {
            "geometry": [
                {"lon": 16.4, "lat": 49.2},
                {"lon": 16.6, "lat": 49.4},
                {"lon": 16.4, "lat": 49.4},
                {"lon": 16.6, "lat": 49.2},
            ]
        }
    }
    _, diagnostics, _ = _assemble_geometry(relation, ways)
    assert any(item.code == "self_intersection" for item in diagnostics)


@pytest.mark.skipif(not _GIS_AVAILABLE, reason="GEOS is not available")
def test_geos_rejects_non_simple_linestring() -> None:
    relation = {"members": [{"type": "way", "ref": 1, "role": ""}]}
    ways = {
        1: {
            "geometry": [
                {"lon": 16.4, "lat": 49.2},
                {"lon": 16.6, "lat": 49.4},
                {"lon": 16.4, "lat": 49.4},
                {"lon": 16.6, "lat": 49.2},
            ]
        }
    }
    _, diagnostics, _ = _assemble_geometry(relation, ways)
    assert any(item.code == "self_intersection" for item in diagnostics)


@pytest.mark.skipif(not _GIS_AVAILABLE, reason="PostGIS/GeoDjango is not available")
def test_postgis_geometry_is_linestring_with_srid() -> None:
    item = collection()
    result = import_osm_snapshot(collection=item, payload=payload())
    version = ReferenceRouteVersion.objects.get(
        pk=cast(int, ReferenceRoute.objects.get().current_version_id)
    )
    assert result["created"] == 1
    assert version.normalized_geometry.geom_type == "LineString"
    assert version.normalized_geometry.srid == 4326


@override_settings(
    GAME_ENABLED=True,
    REFERENCE_ROUTE_AUTHORIZER="apps.api.reference_authorization.allow_session_claim_for_tests",
)
def test_reference_endpoints_require_game_session_claim() -> None:
    item = collection()
    import_production_snapshot(item)
    publish_offer(item)
    approve_reference_route(ReferenceRoute.objects.get(collection=item), reviewer="reviewer")
    client = Client()
    assert client.get("/api/v1/game/reference-routes/").status_code == 403
    user = get_user_model().objects.create_user(username="player")
    client.force_login(user)
    session = client.session
    session["game_session"] = {
        "competition_id": "competition-1",
        "reference_route_read": True,
        "test_authorized_competition": "competition-1",
    }
    session.save()
    response = client.get("/api/v1/game/reference-routes/")
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["route_number"] == "1"
    assert "geometry" not in body["results"][0]
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    session["game_session"]["test_authorized_competition"] = "revoked"
    session.save()
    assert client.get("/api/v1/game/reference-routes/").status_code == 403


@override_settings(
    GAME_ENABLED=True,
    REFERENCE_ROUTE_AUTHORIZER="apps.api.reference_authorization.allow_session_claim_for_tests",
)
def test_reference_api_paginates_and_filters_active_stages() -> None:
    item = collection()
    import_production_snapshot(item)
    parent = ReferenceRoute.objects.get(collection=item)
    publish_offer(item)
    approve_reference_route(parent, reviewer="reviewer")
    for route_number in ("2", "3"):
        extra = payload()
        relation = extra["elements"][0]
        way = extra["elements"][1]
        relation["id"] = 100 + int(route_number)
        relation["tags"]["ref"] = route_number
        relation["members"][0]["ref"] = 500 + int(route_number)
        way["id"] = 500 + int(route_number)
        import_production_snapshot(item, data=extra)
        publish_offer(item)
        approve_reference_route(
            ReferenceRoute.objects.get(route_number=route_number), reviewer="reviewer"
        )
    stage = ReferenceRoute.objects.create(
        collection=item,
        source_identifier="osm-stage:1",
        route_number="1A",
        title="Brno stage",
        operator="Czech Cycling Routes",
        network="rcn",
        parent=parent,
        active=True,
        publication_status="approved",
    )
    parent_version = parent.current_version
    assert parent_version is not None
    stage_version = ReferenceRouteVersion.objects.create(
        route=stage,
        source_import=parent_version.source_import,
        version_number=1,
        checksum="stage-checksum",
        source_geometry=parent_version.source_geometry,
        normalized_geometry=parent_version.normalized_geometry,
        provenance=parent_version.provenance,
        attribution=parent_version.attribution,
        attribution_metadata=parent_version.attribution_metadata,
        validation_status="valid",
        active=True,
    )
    stage.current_version = stage_version
    stage.save(update_fields=["current_version"])
    client = Client()
    user = get_user_model().objects.create_user(username="paged-player")
    client.force_login(user)
    session = client.session
    session["game_session"] = {
        "competition_id": "competition-1",
        "reference_route_read": True,
        "test_authorized_competition": "competition-1",
    }
    session.save()
    response = client.get("/api/v1/game/reference-routes/?page_size=1")
    assert response.status_code == 200
    assert len(response.json()["results"]) == 1
    assert "stages" not in response.json()["results"][0]
    route_numbers = [response.json()["results"][0]["route_number"]]
    next_url = response.json()["next"]
    while next_url:
        parsed = urlparse(next_url)
        page = client.get(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
        assert page.status_code == 200
        route_numbers.extend(item["route_number"] for item in page.json()["results"])
        next_url = page.json()["next"]
    assert route_numbers == ["1", "2", "3"]
    detail = client.get(f"/api/v1/game/reference-routes/{parent.pk}/").json()
    assert detail["stages"][0]["id"] == str(stage.pk)
    assert detail["stages"][0]["geometry"]["type"] == "LineString"


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
def test_concurrent_imports_allocate_distinct_versions() -> None:
    item = collection()
    base = payload()
    changed = payload(changed=True)

    def import_snapshot(data: dict[str, Any]) -> dict[str, Any]:
        close_old_connections()
        try:
            return import_osm_snapshot(
                collection=ReferenceCollection.objects.get(pk=item.pk), payload=data
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(import_snapshot, [base, changed]))

    assert sorted(result["created"] for result in results) == [1, 1]
    route = ReferenceRoute.objects.get(collection=item)
    assert list(route.versions.values_list("version_number", flat=True)) == [1, 2]
