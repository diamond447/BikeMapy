import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
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

from apps.catalogue.fields import _GIS_AVAILABLE
from apps.reference_routes.models import (
    ReferenceCollection,
    ReferenceImport,
    ReferenceRecomputation,
    ReferenceRoute,
    ReferenceRouteVersion,
    ReferenceSourceKind,
)
from apps.reference_routes.services import (
    _assemble_geometry,
    approve_reference_route,
    blocked_via_czechia_import,
    import_osm_snapshot,
    link_stage,
    store_pending_snapshot,
    validate_osm_candidate,
)
from apps.reference_routes.tasks import refresh_reference_routes

pytestmark = pytest.mark.django_db


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


def collection() -> ReferenceCollection:
    return ReferenceCollection.objects.create(
        slug="osm-cz",
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


def test_import_is_idempotent_and_changed_geometry_schedules_recomputation() -> None:
    item = collection()
    first = import_osm_snapshot(collection=item, payload=payload())
    assert first["created"] == 1
    unchanged = import_osm_snapshot(collection=item, payload=payload())
    assert unchanged["status"] == "unchanged"
    changed = import_osm_snapshot(collection=item, payload=payload(changed=True))
    assert changed["created"] == 1
    route = ReferenceRoute.objects.get(collection=item)
    assert route.current_version is not None
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


def test_operator_and_human_review_are_required_before_activation() -> None:
    item = collection()
    data = payload()
    del data["elements"][0]["tags"]["operator"]
    result = import_osm_snapshot(collection=item, payload=data)
    assert result["invalid"] == 1
    assert item.routes.count() == 0


def test_invalid_refresh_does_not_replace_published_route() -> None:
    item = collection()
    import_osm_snapshot(collection=item, payload=payload())
    route = ReferenceRoute.objects.get(collection=item)
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
        )
    assert not item.imports.exists()


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
        response_metadata={"complete": False},
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
    import_osm_snapshot(collection=item, payload=payload())
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
    import_osm_snapshot(collection=item, payload=payload())
    parent = ReferenceRoute.objects.get(collection=item)
    approve_reference_route(parent, reviewer="reviewer")
    for route_number in ("2", "3"):
        extra = payload()
        relation = extra["elements"][0]
        way = extra["elements"][1]
        relation["id"] = 100 + int(route_number)
        relation["tags"]["ref"] = route_number
        relation["members"][0]["ref"] = 500 + int(route_number)
        way["id"] = 500 + int(route_number)
        import_osm_snapshot(collection=item, payload=extra)
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
