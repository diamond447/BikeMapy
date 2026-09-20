from typing import Any, cast

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import Client, override_settings

from apps.catalogue.fields import _GIS_AVAILABLE
from apps.reference_routes.models import (
    ReferenceCollection,
    ReferenceRecomputation,
    ReferenceRoute,
    ReferenceRouteVersion,
    ReferenceSourceKind,
)
from apps.reference_routes.services import (
    approve_reference_route,
    blocked_via_czechia_import,
    import_osm_snapshot,
    link_stage,
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
        "geometry": [{"lat": 49.2, "lon": 16.6}, {"lat": 49.3 if changed else 49.25, "lon": 16.7}],
    }
    return {"version": 0.6, "elements": [relation, way]}


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
        derivative_offer_url="https://example.invalid/bikemapy/osm-alterations",
        rightsholder="OpenStreetMap contributors",
        contact_url="https://www.openstreetmap.org/fixthemap",
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
    version = ReferenceRouteVersion.objects.get()
    assert version.validation_status == "invalid"
    assert version.diagnostics[0]["code"] == "unsupported_network"
    assert not version.route.active


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


def test_operator_and_human_review_are_required_before_activation() -> None:
    item = collection()
    data = payload()
    del data["elements"][0]["tags"]["operator"]
    result = import_osm_snapshot(collection=item, payload=data)
    route = ReferenceRoute.objects.get()
    assert result["invalid"] == 1
    assert route.active is False
    assert route.publication_status == "pending"


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


def test_operator_command_and_celery_task_use_bounded_stored_snapshot(tmp_path: Any) -> None:
    item = collection()
    path = tmp_path / "snapshot.json"
    path.write_bytes(
        b'{"elements":[{"type":"relation","id":101,"tags":{"route":"bicycle","ref":"1","network":"rcn","operator":"operator"},"members":[]}]}'
    )
    call_command("refresh_reference_routes", "--collection", item.slug, "--payload-file", path)
    source_import = item.imports.get()
    task_result = refresh_reference_routes.apply(args=[source_import.pk]).get()
    assert task_result["import_id"] == source_import.pk
    assert task_result["raw_response_sha256"] == source_import.raw_response_sha256


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
    codes = {item["code"] for item in ReferenceRouteVersion.objects.get().diagnostics}
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


@override_settings(GAME_ENABLED=True)
def test_reference_endpoints_require_game_session_claim() -> None:
    item = collection()
    import_osm_snapshot(collection=item, payload=payload())
    approve_reference_route(ReferenceRoute.objects.get(collection=item), reviewer="reviewer")
    client = Client()
    assert client.get("/api/v1/game/reference-routes/").status_code == 403
    user = get_user_model().objects.create_user(username="player")
    client.force_login(user)
    session = client.session
    session["game_session"] = {"competition_id": "competition-1", "reference_route_read": True}
    session.save()
    response = client.get("/api/v1/game/reference-routes/")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["results"][0]["route_number"] == "1"
    assert "geometry" not in body["results"][0]
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Robots-Tag"] == "noindex, nofollow, noarchive"


@override_settings(GAME_ENABLED=True)
def test_reference_api_paginates_and_filters_active_stages() -> None:
    item = collection()
    import_osm_snapshot(collection=item, payload=payload())
    parent = ReferenceRoute.objects.get(collection=item)
    approve_reference_route(parent, reviewer="reviewer")
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
        validation_status="valid",
        active=True,
    )
    stage.current_version = stage_version
    stage.save(update_fields=["current_version"])
    client = Client()
    user = get_user_model().objects.create_user(username="paged-player")
    client.force_login(user)
    session = client.session
    session["game_session"] = {"competition_id": "competition-1", "reference_route_read": True}
    session.save()
    response = client.get("/api/v1/game/reference-routes/?limit=1&offset=0")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert "stages" not in response.json()["results"][0]
    detail = client.get(f"/api/v1/game/reference-routes/{parent.pk}/").json()
    assert detail["stages"][0]["id"] == str(stage.pk)
    assert detail["stages"][0]["geometry"]["type"] == "LineString"
