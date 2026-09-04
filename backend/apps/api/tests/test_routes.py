# mypy: disable-error-code="import-untyped"

from decimal import Decimal
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.api.pagination import RoutePagination
from apps.api.views_routes import filter_routes, public_route_queryset
from apps.catalogue.models import (
    Category,
    ForumAuthor,
    ForumPost,
    ForumThread,
    ProcessingStatus,
    Route,
    RouteCategory,
    RouteLifecycle,
)
from apps.catalogue.services import (
    approve_version,
    merge_route_sources,
    record_route_version,
    register_source,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def public_route() -> Route:
    author = ForumAuthor.objects.create(username="route-author")
    thread = ForumThread.objects.create(
        url="https://bikeforum.example/thread/route", title="Brno gravel rides", locality="Brno"
    )
    post = ForumPost.objects.create(
        thread=thread,
        author=author,
        url="https://bikeforum.example/thread/route#post-1",
    )
    route = Route.objects.create(
        slug="brno-gravel", display_title="Brno gravel loop", lifecycle="published"
    )
    RouteCategory.objects.create(
        route=route, category=Category.objects.create(name="Gravel", slug="gravel")
    )
    source, _ = register_source(
        route=route, post=post, mapy_url="https://mapy.com/s/public-route", source_title="Mapy ride"
    )
    version, _ = record_route_version(
        source=source,
        checksum="public-api-route",
        normalized_geometry={
            "type": "LineString",
            "coordinates": [[16.6, 49.2], [16.7, 49.25]],
        },
        simplified_geometry={
            "type": "LineString",
            "coordinates": [[16.6, 49.2], [16.7, 49.25]],
        },
        distance_m=Decimal("12000.00"),
        ascent_m=Decimal("250.00"),
        technical_status=ProcessingStatus.VALID,
    )
    approve_version(version)
    return Route.objects.get(pk=route.pk)


def test_route_list_is_paginated_and_filters_public_metadata(public_route: Route) -> None:
    response = Client().get("/api/v1/routes/?search=gravel&category=gravel&min_distance=10000")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    item = payload["results"][0]
    assert item["id"] == str(public_route.pk)
    assert item["title"] == "Brno gravel loop"
    assert item["categories"][0]["slug"] == "gravel"
    assert item["geometry"] is None
    assert item["sources"][0]["mapy_url"] == "https://mapy.com/s/public-route"
    assert "original_gpx_storage_key" not in item["sources"][0]


def test_route_detail_resolves_stable_id_and_slug(public_route: Route) -> None:
    client = Client()
    response = client.get(f"/api/v1/routes/{public_route.pk}/")
    assert response.status_code == 200
    assert response.json()["geometry"]["coordinates"][0] == [16.6, 49.2]
    by_slug = client.get(f"/api/v1/routes/by-slug/{public_route.slug}/")
    assert by_slug.status_code == 200
    assert by_slug.json()["id"] == str(public_route.pk)


def test_stable_uuid_survives_readable_slug_change(public_route: Route) -> None:
    original_id = public_route.pk
    public_route.slug = "new-readable-name"
    public_route.save(update_fields=["slug", "updated_at"])
    response = Client().get(f"/api/v1/routes/{original_id}/")
    assert response.status_code == 200
    assert response.json()["id"] == str(original_id)
    assert response.json()["slug"] == "new-readable-name"


def test_unpublished_quarantined_and_soft_deleted_routes_are_not_public(
    public_route: Route,
) -> None:
    Route.objects.create(lifecycle=RouteLifecycle.PUBLISHED)
    Route.objects.create(
        lifecycle=RouteLifecycle.QUARANTINED,
        quarantine_reason="awaiting moderation",
    )
    Route.objects.create(
        lifecycle=RouteLifecycle.SOFT_DELETED,
        deleted_at=timezone.now(),
    )
    payload = Client().get("/api/v1/routes/?page_size=100").json()
    assert payload["count"] == 1
    assert payload["results"][0]["id"] == str(public_route.pk)


def test_list_is_bounded_and_does_not_issue_relationship_queries_per_item(
    public_route: Route,
) -> None:
    category = Category.objects.create(name="Performance", slug="performance")
    for index in range(19):
        author = ForumAuthor.objects.create(username=f"attributed-rider-{index}")
        thread = ForumThread.objects.create(
            url=f"https://bikeforum.example/thread/attributed-{index}",
            title=f"Attributed route {index}",
            locality="Brno",
        )
        post = ForumPost.objects.create(
            thread=thread,
            author=author,
            url=f"https://bikeforum.example/thread/attributed-{index}#post-1",
        )
        route = Route.objects.create(slug=f"attributed-route-{index}")
        RouteCategory.objects.create(route=route, category=category)
        source, _ = register_source(
            route=route,
            post=post,
            mapy_url=f"https://mapy.com/s/attributed-{index}",
        )
        version, _ = record_route_version(
            source=source,
            checksum=f"attributed-{index}",
            technical_status=ProcessingStatus.VALID,
        )
        approve_version(version)
    client = Client()
    started = perf_counter()
    with CaptureQueriesContext(connection) as queries:
        response = client.get("/api/v1/routes/?page_size=1000")
    elapsed = perf_counter() - started
    assert response.status_code == 200
    assert len(response.json()["results"]) == 20
    assert len(response.json()["results"]) <= 100
    assert len(queries) <= 15
    assert elapsed < 2.0


def test_pagination_caps_client_page_size() -> None:
    request = Request(APIRequestFactory().get("/api/v1/routes/?page_size=1000"))
    assert RoutePagination().get_page_size(request) == 100


def test_search_across_multiple_sources_has_one_result_per_route(public_route: Route) -> None:
    thread = ForumThread.objects.create(
        url=f"https://bikeforum.example/thread/{uuid4()}",
        title="Unique cardinality search phrase",
        locality="Brno",
    )
    author = ForumAuthor.objects.create(username="second-source-author")
    post = ForumPost.objects.create(
        thread=thread,
        author=author,
        url=f"https://bikeforum.example/post/{uuid4()}",
    )
    register_source(
        route=public_route,
        post=post,
        mapy_url=f"https://mapy.com/s/{uuid4()}",
    )
    response = Client().get("/api/v1/routes/?search=cardinality")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert len(payload["results"]) == 1


def test_search_and_author_filter_include_active_merged_sources(public_route: Route) -> None:
    duplicate = Route.objects.create(slug="merged-route", display_title="Merged route")
    thread = ForumThread.objects.create(
        url=f"https://bikeforum.example/thread/{uuid4()}",
        title="Merged provenance locality",
        locality="Mergedtown",
    )
    author = ForumAuthor.objects.create(username="merged-provenance-author")
    post = ForumPost.objects.create(
        thread=thread,
        author=author,
        url=f"https://bikeforum.example/post/{uuid4()}",
    )
    source, _ = register_source(
        route=duplicate,
        post=post,
        mapy_url=f"https://mapy.com/s/{uuid4()}",
    )
    merge_route_sources(public_route, duplicate, reason="Consolidated duplicate identity")
    assert public_route.provenance_sources.filter(pk=source.pk).exists()

    client = Client()
    by_place = client.get("/api/v1/routes/?search=Mergedtown")
    by_author = client.get("/api/v1/routes/?author=merged-provenance-author")
    assert by_place.json()["count"] == 1
    assert by_place.json()["results"][0]["id"] == str(public_route.pk)
    assert by_author.json()["count"] == 1
    assert by_author.json()["results"][0]["id"] == str(public_route.pk)


def test_postgres_route_search_uses_tsvector_match_not_rank_threshold(
    public_route: Route,
) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL query compilation")
    query = str(
        filter_routes(public_route_queryset(), {"search": "gravel"})
        .order_by("-_search_rank", "id")
        .query
    )
    assert "@@" in query
    assert "ts_rank" in query
    where_clause = query[query.find("WHERE") : query.find("ORDER BY")]
    assert "ts_rank" not in where_clause


def test_openapi_contract_contains_versioned_read_endpoints(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.yaml"
    call_command("spectacular", file=str(schema_path), validate=True)
    schema = schema_path.read_text(encoding="utf-8")
    assert "openapi: 3." in schema
    assert "/api/v1/routes/" in schema
    assert "/api/v1/routes/viewport/" in schema


def test_viewport_endpoint_requires_bounds_and_returns_bounded_data(public_route: Route) -> None:
    client = Client()
    missing = client.get("/api/v1/routes/viewport/?zoom=12")
    assert missing.status_code == 400
    response = client.get(
        "/api/v1/routes/viewport/?west=16.5&south=49.1&east=16.8&north=49.3&zoom=12&limit=1"
    )
    assert response.status_code == 200
    assert response.json()["routes"][0]["id"] == str(public_route.pk)

    truncated = client.get(
        "/api/v1/routes/viewport/?west=16.5&south=49.1&east=16.8&north=49.3&zoom=12&limit=0"
    )
    assert truncated.status_code == 200
    assert truncated.json()["routes"] == []
    assert truncated.json()["truncated"] is True
