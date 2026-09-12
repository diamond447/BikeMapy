# mypy: disable-error-code="import-untyped"

import os
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import cast
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import connection
from django.http import StreamingHttpResponse
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.api.pagination import RoutePagination
from apps.api.views_routes import filter_routes, public_route_queryset
from apps.catalogue.fields import _GIS_AVAILABLE
from apps.catalogue.models import (
    Category,
    ForumAuthor,
    ForumPost,
    ForumThread,
    ProcessingStatus,
    Route,
    RouteBrowseGeometry,
    RouteCategory,
    RouteLifecycle,
    RouteSource,
    RouteVersion,
)
from apps.catalogue.services import (
    approve_version,
    merge_route_sources,
    record_route_version,
    register_source,
    review_route,
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
        storage_key="gpx/public-api-route.gpx",
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
        elevation_profile=[
            {"distance_m": 0.0, "elevation_m": 210.0},
            {"distance_m": 12000.0, "elevation_m": 460.0},
        ],
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


def test_detail_exposes_profile_and_keeps_review_badge_narrow(public_route: Route) -> None:
    payload = Client().get(f"/api/v1/routes/{public_route.pk}/").json()
    assert payload["elevation_profile"][-1]["elevation_m"] == 460.0
    assert payload["reviewed"] is False
    review_route(
        public_route,
        reason="Checked technical validity, source context, and suitability",
        technical_validity=True,
        source_context=True,
        content_suitability=True,
    )
    payload = Client().get(f"/api/v1/routes/{public_route.pk}/").json()
    assert payload["reviewed"] is True


def test_gpx_download_contract_is_closed_before_legal_approval(public_route: Route) -> None:
    with override_settings(GPX_REDISTRIBUTION_APPROVED=False):
        response = Client().get(f"/api/v1/routes/{public_route.pk}/gpx/")
    assert response.status_code == 404
    assert Client().get(f"/api/v1/routes/{public_route.pk}/").json()["gpx_download_url"] is None


def test_gpx_download_uses_backend_origin_and_serves_only_existing_payload(
    public_route: Route, tmp_path: Path
) -> None:
    with override_settings(GPX_REDISTRIBUTION_APPROVED=True, MEDIA_ROOT=tmp_path):
        payload = Client().get(f"/api/v1/routes/{public_route.pk}/").json()
        assert payload["gpx_download_url"] == (
            f"http://testserver/api/v1/routes/{public_route.pk}/gpx/"
        )
        missing = Client().get(f"/api/v1/routes/{public_route.pk}/gpx/")
        assert missing.status_code == 404
        default_storage.save("gpx/public-api-route.gpx", ContentFile(b"<gpx />"))
        response = Client().get(f"/api/v1/routes/{public_route.pk}/gpx/")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/gpx+xml"
    assert "attachment" in response["Content-Disposition"]
    streamed_response = cast(StreamingHttpResponse, response)
    content = cast(Iterable[bytes], streamed_response.streaming_content)
    assert b"<gpx />" in b"".join(content)


def test_gpx_download_can_use_internal_proxy_handoff(public_route: Route, tmp_path: Path) -> None:
    with override_settings(
        GPX_REDISTRIBUTION_APPROVED=True,
        GPX_INTERNAL_REDIRECT=True,
        MEDIA_ROOT=tmp_path,
    ):
        default_storage.save("gpx/public-api-route.gpx", ContentFile(b"<gpx />"))
        response = Client().get(f"/api/v1/routes/{public_route.pk}/gpx/")
    assert response.status_code == 200
    assert response["X-Accel-Redirect"] == "/_protected_gpx/gpx/public-api-route.gpx"
    assert response.content == b""


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
        .order_by("-_search_rank", "id")  # type: ignore[misc]
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


def test_viewport_endpoint_applies_catalogue_filters(public_route: Route) -> None:
    client = Client()
    base = "west=16.5&south=49.1&east=16.8&north=49.3&zoom=12"

    matching = client.get(f"/api/v1/routes/viewport/?{base}&search=gravel")
    assert matching.status_code == 200
    assert [item["id"] for item in matching.json()["routes"]] == [str(public_route.pk)]

    omitted = client.get(f"/api/v1/routes/viewport/?{base}&search=forest")
    assert omitted.status_code == 200
    assert omitted.json()["routes"] == []


def test_filtered_viewport_cache_invalidates_when_match_is_removed(public_route: Route) -> None:
    client = Client()
    base = "west=16.5&south=49.1&east=16.8&north=49.3&zoom=12&search=loop"
    assert client.get(f"/api/v1/routes/viewport/?{base}").json()["routes"]

    public_route.display_title = "Brno gravel ride"
    public_route.save(update_fields=["display_title", "updated_at"])

    assert client.get(f"/api/v1/routes/viewport/?{base}").json()["routes"] == []


def test_filtered_viewport_cache_invalidates_when_match_is_added(public_route: Route) -> None:
    client = Client()
    base = "west=16.5&south=49.1&east=16.8&north=49.3&zoom=12&search=newly-added"
    assert client.get(f"/api/v1/routes/viewport/?{base}").json()["routes"] == []

    public_route.display_title = "Newly-added gravel ride"
    public_route.save(update_fields=["display_title", "updated_at"])

    assert client.get(f"/api/v1/routes/viewport/?{base}").json()["routes"]


def _create_dense_viewport_catalogue(public_route: Route, count: int) -> list[Route]:
    """Create public route rows without evaluating a filtered route queryset."""

    routes = Route.objects.bulk_create(
        [Route(display_title=f"Dense gravel route {index}") for index in range(count)]
    )
    sources = RouteSource.objects.bulk_create(
        [
            RouteSource(route=route, mapy_url=f"https://mapy.com/s/dense-{route.pk}")
            for route in routes
        ]
    )
    geometry: object = {
        "type": "LineString",
        "coordinates": [[16.6, 49.2], [16.7, 49.25]],
    }
    if _GIS_AVAILABLE:
        from django.contrib.gis.geos import GEOSGeometry

        geometry = GEOSGeometry(
            '{"type":"LineString","coordinates":[[16.6,49.2],[16.7,49.25]]}',
            srid=4326,
        )
    versions = RouteVersion.objects.bulk_create(
        [
            RouteVersion(
                source=source,
                version_number=1,
                checksum=f"dense-{source.pk}",
                normalized_geometry=geometry,
                simplified_geometry=geometry,
                distance_m=Decimal("12000.00"),
                technical_status=ProcessingStatus.VALID,
            )
            for source in sources
        ]
    )
    for route, version in zip(routes, versions, strict=True):
        route.current_approved_version_id = version.pk
    Route.objects.bulk_update(routes, ["current_approved_version"])
    if _GIS_AVAILABLE:
        RouteBrowseGeometry.objects.bulk_create(
            [
                RouteBrowseGeometry(
                    version=version,
                    zoom=12,
                    geometry=geometry,
                    tolerance_m=10,
                )
                for version in versions
            ]
        )
    return [public_route, *routes]


def test_filtered_viewport_http_query_count_stays_bounded_for_dense_catalogue(
    public_route: Route,
) -> None:
    _create_dense_viewport_catalogue(public_route, count=80)
    cache.clear()
    query = (
        "west=16.5&south=49.1&east=16.8&north=49.3&zoom=12&limit=3&"
        "search=dense&min_distance_m=12000.00"
    )
    with CaptureQueriesContext(connection) as queries:
        response = Client().get(f"/api/v1/routes/viewport/?{query}")

    assert response.status_code == 200
    assert len(response.json()["routes"]) == 3
    assert len(queries) <= 8
    assert any(" IN (SELECT" in query["sql"].upper() for query in queries)


@pytest.mark.benchmark
@pytest.mark.skipif(
    os.getenv("RUN_SPATIAL_BENCHMARK") != "1",
    reason="opt-in benchmark; run against the Compose PostGIS database",
)
def test_filtered_viewport_http_cold_cache_benchmark(public_route: Route) -> None:
    """Measure the complete filtered HTTP path against a dense catalogue."""

    _create_dense_viewport_catalogue(public_route, count=1_500)
    endpoint = (
        "/api/v1/routes/viewport/?west=16.5&south=49.1&east=16.8&north=49.3&"
        "zoom=12&limit=50&search=dense"
    )
    samples = []
    client = Client()
    for _ in range(10):
        cache.clear()
        started = perf_counter()
        response = client.get(endpoint)
        samples.append((perf_counter() - started) * 1000)
        assert response.status_code == 200
        assert len(response.json()["routes"]) == 50
    ordered = sorted(samples)
    p95 = ordered[-1]
    print(
        f"filtered_viewport_http_cold_cache_ms_median={ordered[len(ordered) // 2]:.2f} "
        f"p95={p95:.2f} budget=2000.00"
    )
    assert len(samples) == 10
    # This is deliberately generous for a cold application/cache process, but
    # catches accidental evaluation of the full dense candidate catalogue.
    assert p95 < 2_000


@pytest.mark.skipif(not _GIS_AVAILABLE, reason="requires the PostGIS geometry backend")
def test_route_list_bbox_filter_runs_before_pagination(public_route: Route) -> None:
    client = Client()
    included = client.get("/api/v1/routes/?page_size=100&west=16.5&south=49.1&east=16.8&north=49.3")
    assert included.status_code == 200
    assert included.json()["count"] == 1
    assert included.json()["results"][0]["id"] == str(public_route.pk)

    excluded = client.get("/api/v1/routes/?page_size=100&west=17&south=49.1&east=17.2&north=49.3")
    assert excluded.status_code == 200
    assert excluded.json()["count"] == 0
