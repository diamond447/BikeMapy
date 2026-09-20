from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.reference_routes.models import (
    ReferenceCollection,
    ReferenceRecomputation,
    ReferenceRoute,
    ReferenceRouteVersion,
    ReferenceSourceKind,
)
from apps.reference_routes.services import blocked_via_czechia_import, import_osm_snapshot

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


def test_reference_endpoints_require_authentication() -> None:
    item = collection()
    import_osm_snapshot(collection=item, payload=payload())
    client = Client()
    assert client.get("/api/v1/game/reference-routes/").status_code == 403
    user = get_user_model().objects.create_user(username="player")
    client.force_login(user)
    response = client.get("/api/v1/game/reference-routes/")
    assert response.status_code == 200
    assert response.json()[0]["route_number"] == "1"
