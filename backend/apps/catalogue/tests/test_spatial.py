import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from statistics import median
from threading import Barrier
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

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
    RouteHeatmapCell,
    RouteHeatmapMembership,
    RouteSource,
    RouteVersion,
)
from apps.catalogue.services import approve_version, record_route_version, register_source
from apps.catalogue.spatial import (
    SpatialQueryLimits,
    _cell_geometry,
    _line_simplify,
    _tile_xy,
    generate_browse_geometries,
    normalize_filter_inputs,
    query_selected_route,
    query_viewport,
    refresh_route_heatmap,
    schedule_spatial_cache_invalidation,
    schedule_viewport_filter_cache_invalidation,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def published_route() -> Route:
    thread = ForumThread.objects.create(url="https://example.test/thread", title="Routes")
    post = ForumPost.objects.create(thread=thread, url="https://example.test/thread#1")
    route = Route.objects.create(display_title="South Moravia")
    source, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/spatial")
    version, _ = record_route_version(
        source=source,
        checksum="spatial",
        normalized_geometry={
            "type": "LineString",
            "coordinates": [[16.0, 49.0], [16.1, 49.001], [16.2, 49.0]],
        },
        simplified_geometry={
            "type": "LineString",
            "coordinates": [[16.0, 49.0], [16.2, 49.0]],
        },
        technical_status=ProcessingStatus.VALID,
        distance_m=Decimal("20000"),
    )
    approve_version(version)
    return Route.objects.get(pk=route.pk)


@override_settings(SPATIAL_BROWSE_ZOOMS="8,12", SPATIAL_HEATMAP_ZOOMS="5,6")
def test_browse_geometries_are_zoom_specific_and_selected_route_is_full(
    published_route: Route,
) -> None:
    version = published_route.current_approved_version
    assert version is not None
    RouteBrowseGeometry.objects.filter(version=version).delete()
    generate_browse_geometries(version, zooms=(8, 12))
    rows = RouteBrowseGeometry.objects.filter(version=version).order_by("zoom")
    assert list(rows.values_list("zoom", flat=True)) == [8, 12]
    selected = query_selected_route(published_route.pk)
    assert selected["geometry"]["coordinates"] == [[16.0, 49.0], [16.1, 49.001], [16.2, 49.0]]
    viewport = query_viewport(west=15.9, south=48.9, east=16.3, north=49.1, zoom=8)
    assert viewport["routes"][0]["geometry"]["coordinates"] == [[16.0, 49.0], [16.2, 49.0]]


@override_settings(SPATIAL_BROWSE_ZOOMS="12", SPATIAL_HEATMAP_ZOOMS="5,6")
def test_heatmap_counts_distinct_routes_crossing_cells(published_route: Route) -> None:
    refresh_route_heatmap(published_route)
    assert RouteHeatmapMembership.objects.filter(route=published_route).exists()
    assert RouteHeatmapCell.objects.filter(route_count=1).exists()
    result = query_viewport(west=15.9, south=48.9, east=16.3, north=49.1, zoom=6)
    assert result["mode"] == "heatmap"
    assert any(item["count"] == 1 for item in result["cells"])


@override_settings(SPATIAL_HEATMAP_ZOOMS="5")
def test_filtered_heatmap_limits_after_filtered_count_ordering(published_route: Route) -> None:
    """A filtered low-zoom map must retain the strongest matching cells."""

    other_route = Route.objects.create()
    another_other_route = Route.objects.create()
    matching_route = Route.objects.create()
    RouteHeatmapCell.objects.filter(zoom=5).delete()
    x, y = _tile_xy(16.0, 49.0, 5)
    stronger_unfiltered = RouteHeatmapCell.objects.create(
        zoom=5,
        x=x,
        y=y,
        boundary=_cell_geometry(x, y, 5),
        route_count=3,
    )
    weaker_filtered = RouteHeatmapCell.objects.create(
        zoom=5,
        x=x + 1,
        y=y,
        boundary=_cell_geometry(x + 1, y, 5),
        route_count=2,
    )
    RouteHeatmapMembership.objects.create(cell=stronger_unfiltered, route=published_route)
    RouteHeatmapMembership.objects.create(cell=stronger_unfiltered, route=other_route)
    RouteHeatmapMembership.objects.create(cell=stronger_unfiltered, route=another_other_route)
    RouteHeatmapMembership.objects.create(cell=weaker_filtered, route=published_route)
    RouteHeatmapMembership.objects.create(cell=weaker_filtered, route=matching_route)

    cache.clear()
    with CaptureQueriesContext(connection) as queries:
        result = query_viewport(
            west=0,
            south=30,
            east=40,
            north=60,
            zoom=2,
            limits=SpatialQueryLimits(max_cells=1),
            route_ids={published_route.pk, matching_route.pk},
        )

    assert result["mode"] == "heatmap"
    assert len(result["cells"]) == 1
    assert result["cells"][0]["zoom"] == 5
    assert result["cells"][0]["x"] == weaker_filtered.x
    assert result["cells"][0]["y"] == weaker_filtered.y
    assert result["cells"][0]["count"] == 2
    assert result["truncated"] is True
    heatmap_sql = "\n".join(query["sql"].upper() for query in queries)
    assert "EXISTS" in heatmap_sql
    assert "LIMIT" in heatmap_sql
    assert "IN (SELECT DISTINCT" not in heatmap_sql


@override_settings(SPATIAL_HEATMAP_ZOOMS="5", SPATIAL_MAX_CANDIDATE_SCAN=3)
def test_filtered_heatmap_limits_after_viewport_relevance_and_reports_overflow() -> None:
    in_view_x, in_view_y = _tile_xy(16.0, 49.0, 5)
    in_view_cell = RouteHeatmapCell.objects.create(
        zoom=5,
        x=in_view_x,
        y=in_view_y,
        boundary=_cell_geometry(in_view_x, in_view_y, 5),
        route_count=2,
    )
    off_view_cell = RouteHeatmapCell.objects.create(
        zoom=5,
        x=in_view_x + 5,
        y=in_view_y,
        boundary=_cell_geometry(in_view_x + 5, in_view_y, 5),
        route_count=3,
    )
    off_view_routes = [Route.objects.create(id=UUID(int=index)) for index in (1, 2, 3)]
    first_match = Route.objects.create(id=UUID(int=100))
    second_match = Route.objects.create(id=UUID(int=101))
    third_match = Route.objects.create(id=UUID(int=102))
    RouteHeatmapMembership.objects.bulk_create(
        [
            *(RouteHeatmapMembership(cell=off_view_cell, route=route) for route in off_view_routes),
            RouteHeatmapMembership(cell=in_view_cell, route=first_match),
            RouteHeatmapMembership(cell=in_view_cell, route=second_match),
            RouteHeatmapMembership(cell=in_view_cell, route=third_match),
        ]
    )
    cache.clear()

    result = query_viewport(
        west=15.9,
        south=48.9,
        east=16.3,
        north=49.1,
        zoom=2,
        limits=SpatialQueryLimits(max_routes=1, max_cells=1),
        route_ids=set(route.pk for route in [*off_view_routes, first_match]),
    )
    assert result["cells"][0]["x"] == in_view_x
    assert result["cells"][0]["count"] == 1
    assert result["truncated"] is False

    overflow = query_viewport(
        west=15.9,
        south=48.9,
        east=16.3,
        north=49.1,
        zoom=2,
        limits=SpatialQueryLimits(max_routes=1, max_cells=1),
        route_ids=set(
            route.pk for route in [*off_view_routes, first_match, second_match, third_match]
        ),
    )
    assert overflow["cells"][0]["count"] == 2
    assert overflow["truncated"] is True


def test_viewport_and_selected_queries_are_bounded(published_route: Route) -> None:
    with pytest.raises(ValidationError):
        query_viewport(west=-180, south=-90, east=180, north=90, zoom=12)
    result = query_viewport(
        west=15.9,
        south=48.9,
        east=16.3,
        north=49.1,
        zoom=12,
        limits=SpatialQueryLimits(max_routes=0, max_cells=1),
    )
    assert result["mode"] == "routes"
    assert result["routes"] == []


@pytest.mark.skipif(
    connection.vendor == "postgresql", reason="covers the non-GIS Python bbox fallback"
)
@override_settings(SPATIAL_MAX_CANDIDATE_SCAN=5)
def test_non_gis_viewport_scan_keeps_configured_bound_before_bbox_filter(
    published_route: Route,
) -> None:
    offscreen_geometry = {"type": "LineString", "coordinates": [[0, 0], [0.1, 0]]}
    matching_geometry = {"type": "LineString", "coordinates": [[16, 49], [16.1, 49]]}
    routes: list[Route] = []
    for route_id, geometry in (
        (1, offscreen_geometry),
        (2, offscreen_geometry),
        (100, matching_geometry),
    ):
        route = Route(id=UUID(int=route_id), display_title=f"candidate-{route_id}")
        route.save(force_insert=True)
        source = RouteSource.objects.create(
            route=route, mapy_url=f"https://mapy.com/s/candidate-{route_id}"
        )
        version = RouteVersion.objects.create(
            source=source,
            version_number=1,
            checksum=f"candidate-{route_id}",
            normalized_geometry=geometry,
            simplified_geometry=geometry,
            technical_status=ProcessingStatus.VALID,
        )
        route.current_approved_version = version
        route.save(update_fields=["current_approved_version", "updated_at"])
        routes.append(route)

    cache.clear()
    result = query_viewport(
        west=15,
        south=48,
        east=17,
        north=50,
        zoom=12,
        limits=SpatialQueryLimits(max_routes=1),
    )

    assert [item["id"] for item in result["routes"]] == [str(routes[-1].pk)]


def test_viewport_filter_cache_inputs_are_normalized() -> None:
    assert normalize_filter_inputs(
        {
            "search": "  gravel  ",
            "max_distance_m": "12000.00",
            "min_distance_m": "1.2e2",
        }
    ) == (
        ("max_distance_m", "12000"),
        ("min_distance_m", "120"),
        ("search", "gravel"),
    )


def test_filter_invalidation_signals_coalesce_and_skip_unlinked_creates() -> None:
    route = Route.objects.create()
    category = Category.objects.create(name="Signal test", slug="signal-test")
    with patch("apps.catalogue.spatial.bump_viewport_filter_cache_epoch") as bump:
        with TestCase.captureOnCommitCallbacks(execute=True):
            route.display_title = "Changed title"
            route.save(update_fields=["display_title", "updated_at"])
            category.slug = "signal-test-updated"
            category.save(update_fields=["slug"])
            RouteCategory.objects.create(route=route, category=category)
        assert bump.call_count == 1

    with patch("apps.catalogue.spatial.bump_viewport_filter_cache_epoch") as bump:
        with TestCase.captureOnCommitCallbacks(execute=True):
            author = ForumAuthor.objects.create(username="unlinked-author")
            thread = ForumThread.objects.create(
                url="https://example.test/unlinked-thread", title="Unlinked thread"
            )
            ForumPost.objects.create(
                thread=thread,
                author=author,
                url="https://example.test/unlinked-thread#1",
            )
            RouteSource.objects.create(route=route, mapy_url="https://mapy.com/s/unlinked-source")
        assert bump.call_count == 0


@override_settings(SPATIAL_HEATMAP_ZOOMS="5,6")
def test_sparse_requested_heatmap_zoom_uses_nearest_generated_grid(
    published_route: Route,
) -> None:
    refresh_route_heatmap(published_route)
    result = query_viewport(west=15.9, south=48.9, east=16.3, north=49.1, zoom=2)
    assert result["mode"] == "heatmap"
    assert result["data_zoom"] == 5
    assert result["cells"]


def test_query_limits_are_rejected_or_clamped(published_route: Route) -> None:
    with pytest.raises(ValidationError):
        SpatialQueryLimits(max_routes=-1)
    limits = SpatialQueryLimits(max_routes=50_000, max_cells=50_000)
    assert limits.max_routes == 500
    assert limits.max_cells == 10_000


@pytest.mark.django_db(transaction=True)
def test_cache_epoch_advances_only_after_commit_and_not_after_rollback() -> None:
    from apps.catalogue.spatial import _FILTER_CACHE_EPOCH_KEY

    cache.clear()
    initial = int(cache.get("bikemapy:spatial:epoch", 0) or 0)
    filter_initial = int(cache.get(_FILTER_CACHE_EPOCH_KEY, 0) or 0)
    with transaction.atomic():
        schedule_spatial_cache_invalidation()
        for _ in range(4):
            schedule_viewport_filter_cache_invalidation()
        assert int(cache.get("bikemapy:spatial:epoch", 0) or 0) == initial
        assert int(cache.get(_FILTER_CACHE_EPOCH_KEY, 0) or 0) == filter_initial
    committed = int(cache.get("bikemapy:spatial:epoch", 0) or 0)
    filter_committed = int(cache.get(_FILTER_CACHE_EPOCH_KEY, 0) or 0)
    assert committed > initial
    assert filter_committed == filter_initial + 1
    try:
        with transaction.atomic():
            schedule_spatial_cache_invalidation()
            for _ in range(3):
                schedule_viewport_filter_cache_invalidation()
            raise RuntimeError("rollback")
    except RuntimeError:
        pass
    assert int(cache.get("bikemapy:spatial:epoch", 0) or 0) == committed
    assert int(cache.get(_FILTER_CACHE_EPOCH_KEY, 0) or 0) == filter_committed
    with transaction.atomic():
        schedule_viewport_filter_cache_invalidation()
    assert int(cache.get(_FILTER_CACHE_EPOCH_KEY, 0) or 0) == filter_committed + 1
    with transaction.atomic():
        schedule_viewport_filter_cache_invalidation()
    assert int(cache.get(_FILTER_CACHE_EPOCH_KEY, 0) or 0) == filter_committed + 2

    @transaction.atomic
    def decorated_schedule() -> None:
        schedule_viewport_filter_cache_invalidation()

    decorated_schedule()
    decorated_schedule()
    assert int(cache.get(_FILTER_CACHE_EPOCH_KEY, 0) or 0) == filter_committed + 4


def test_cache_epoch_invalidation_does_not_raise_when_redis_is_down() -> None:
    from apps.catalogue.spatial import bump_spatial_cache_epoch

    with (
        patch.object(cache, "add", side_effect=ConnectionError("redis unavailable")),
        patch.object(cache, "get", side_effect=ConnectionError("redis unavailable")),
    ):
        assert bump_spatial_cache_epoch() == 0


@pytest.mark.redis
@pytest.mark.skipif(
    os.getenv("RUN_REDIS_TEST") != "1",
    reason="opt-in Redis integration test",
)
def test_redis_cache_increment_is_atomic_and_visible_to_independent_clients() -> None:
    from django.core.cache.backends.redis import RedisCache

    url = os.getenv("DJANGO_CACHE_URL", "redis://127.0.0.1:6379/1")
    first = RedisCache(url, {})
    second = RedisCache(url, {})
    key = "bikemapy:test:spatial:epoch"
    first.delete(key)
    assert first.add(key, 0, timeout=None)
    assert first.incr(key) == 1
    assert second.get(key) == 1
    assert second.incr(key) == 2
    assert first.get(key) == 2
    first.delete(key)


def test_long_sqlite_fallback_simplification_is_iterative() -> None:
    points = [(index / 100_000, math.sin(index / 100) / 100_000) for index in range(20_000)]
    simplified = _line_simplify(points, tolerance_m=2)
    assert simplified[0] == points[0]
    assert simplified[-1] == points[-1]
    assert len(simplified) < len(points)


def test_long_route_browse_geometry_generation_is_bounded(published_route: Route) -> None:
    source = published_route.sources.first()
    assert source is not None
    coordinates = [
        [16.0 + index * 0.00001, 49.0 + math.sin(index / 30) * 0.001] for index in range(5_000)
    ]
    payload: Any = {"type": "LineString", "coordinates": coordinates}
    if _GIS_AVAILABLE:
        from django.contrib.gis.geos import GEOSGeometry

        payload = GEOSGeometry(json.dumps(payload), srid=4326)
    version = RouteVersion.objects.create(
        source=source,
        version_number=2,
        checksum="long-browse",
        normalized_geometry=payload,
        simplified_geometry=payload,
        technical_status=ProcessingStatus.VALID,
    )
    rows = generate_browse_geometries(version, zooms=(12,))
    assert len(rows) == 1
    assert (
        len(rows[0].geometry.coords if _GIS_AVAILABLE else rows[0].geometry["coordinates"]) < 5_000
    )


def test_spatial_products_can_be_rebuilt_for_existing_routes(published_route: Route) -> None:
    version = published_route.current_approved_version
    assert version is not None
    RouteBrowseGeometry.objects.filter(version=version).delete()
    call_command("rebuild_spatial_products", route_id=str(published_route.pk))
    assert RouteBrowseGeometry.objects.filter(version=version).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="requires PostgreSQL row locking")
def test_concurrent_route_refreshes_preserve_shared_cell_count(published_route: Route) -> None:
    """Two route refreshes sharing cells must not lose a count update."""

    source_for_thread = published_route.sources.first()
    assert source_for_thread is not None
    post_for_thread = source_for_thread.posts.first()
    assert post_for_thread is not None
    post = ForumPost.objects.create(
        thread=post_for_thread.thread, url="https://example.test/thread#concurrent"
    )
    route = Route.objects.create(display_title="Concurrent route")
    source, _ = register_source(route=route, post=post, mapy_url="https://mapy.com/s/concurrent")
    version, _ = record_route_version(
        source=source,
        checksum="concurrent",
        normalized_geometry={
            "type": "LineString",
            "coordinates": [[16.0, 49.0], [16.2, 49.0]],
        },
        technical_status=ProcessingStatus.VALID,
    )
    approve_version(version)
    start_barrier = Barrier(2)
    shared_cell_ids = list(
        RouteHeatmapCell.objects.filter(
            memberships__route__in=[published_route, route]
        ).values_list("pk", flat=True)
    )
    RouteHeatmapMembership.objects.filter(route__in=[published_route, route]).delete()
    RouteHeatmapCell.objects.filter(pk__in=shared_cell_ids).update(route_count=0)

    def refresh(route_id: UUID) -> None:
        start_barrier.wait()
        close_old_connections()

        def wait_for_cell_lock() -> None:
            start_barrier.wait()

        refresh_route_heatmap(Route.objects.get(pk=route_id), before_cell_lock=wait_for_cell_lock)
        close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(refresh, [published_route.pk, route.pk]))
    shared_cells = RouteHeatmapCell.objects.filter(pk__in=shared_cell_ids)
    assert shared_cells.exists()
    assert all(
        cell.route_count == RouteHeatmapMembership.objects.filter(cell=cell).count()
        for cell in shared_cells
    )


@pytest.mark.benchmark
@pytest.mark.skipif(
    os.getenv("RUN_SPATIAL_BENCHMARK") != "1",
    reason="opt-in benchmark; run against the Compose PostGIS database",
)
def test_representative_route_viewport_benchmark(published_route: Route) -> None:
    """Keep a repeatable query measurement next to the spatial contract."""

    source_version = published_route.current_approved_version
    assert source_version is not None
    geometries: list[Any] = []
    simplified_geometries: list[Any] = []
    for index in range(1, 2_000):
        longitude = 14.0 + (index % 40) * 0.1
        latitude = 47.5 + (index % 30) * 0.1
        vertex_count = 4 + index % 40
        coordinates = [
            [longitude + step * 0.002, latitude + math.sin(step / 3) * 0.01]
            for step in range(vertex_count)
        ]
        payload = {"type": "LineString", "coordinates": coordinates}
        simplified_payload = {
            "type": "LineString",
            "coordinates": [coordinates[0], coordinates[-1]],
        }
        if _GIS_AVAILABLE:
            from django.contrib.gis.geos import GEOSGeometry

            geometries.append(GEOSGeometry(json.dumps(payload), srid=4326))
            simplified_geometries.append(GEOSGeometry(json.dumps(simplified_payload), srid=4326))
        else:
            geometries.append(payload)
            simplified_geometries.append(simplified_payload)
    routes = Route.objects.bulk_create(
        [Route(display_title=f"Benchmark route {index}") for index in range(1, 2_000)]
    )
    sources = RouteSource.objects.bulk_create(
        [
            RouteSource(route=route, mapy_url=f"https://mapy.com/s/benchmark-{route.pk}")
            for route in routes
        ]
    )
    versions = RouteVersion.objects.bulk_create(
        [
            RouteVersion(
                source=source,
                version_number=1,
                checksum=f"benchmark-{source.pk}",
                normalized_geometry=geometries[index],
                simplified_geometry=simplified_geometries[index],
                technical_status=ProcessingStatus.VALID,
            )
            for index, source in enumerate(sources)
        ]
    )
    for route, version in zip(routes, versions, strict=True):
        route.current_approved_version = version
    Route.objects.bulk_update(routes, ["current_approved_version"])
    RouteBrowseGeometry.objects.bulk_create(
        [
            RouteBrowseGeometry(
                version=version,
                zoom=12,
                geometry=simplified_geometries[index],
                tolerance_m=10,
            )
            for index, version in enumerate(versions)
        ]
    )
    heatmap_cells = [
        RouteHeatmapCell.objects.get_or_create(
            zoom=6,
            x=34,
            y=21 + index,
            defaults={"boundary": _cell_geometry(34, 21 + index, 6)},
        )[0]
        for index in range(3)
    ]
    RouteHeatmapMembership.objects.bulk_create(
        [
            RouteHeatmapMembership(cell=heatmap_cells[index % 3], route=route)
            for index, route in enumerate(routes)
        ]
    )
    for index, cell in enumerate(heatmap_cells):
        cell.route_count = sum(1 for item in range(len(routes)) if item % 3 == index)
        cell.save(update_fields=["route_count", "updated_at"])

    def measure(call: Any) -> list[float]:
        samples = []
        for _ in range(20):
            cache.clear()
            started = time.perf_counter()
            call()
            samples.append((time.perf_counter() - started) * 1000)
        return samples

    route_samples = measure(
        lambda: query_viewport(
            west=14.5,
            south=47.5,
            east=17.5,
            north=50.2,
            zoom=12,
            limits=SpatialQueryLimits(max_routes=500, max_cells=10_000),
        )
    )
    route_result = query_viewport(
        west=14.5,
        south=47.5,
        east=17.5,
        north=50.2,
        zoom=12,
        limits=SpatialQueryLimits(max_routes=500, max_cells=10_000),
    )
    print(f"route_viewport_returned={len(route_result['routes'])}")
    heatmap_samples = measure(
        lambda: query_viewport(
            west=14.5,
            south=47.5,
            east=17.5,
            north=50.2,
            zoom=6,
            limits=SpatialQueryLimits(max_routes=500, max_cells=10_000),
        )
    )
    selected_samples = measure(lambda: query_selected_route(routes[0].pk))

    def report(name: str, samples: list[float], budget: float) -> None:
        ordered = sorted(samples)
        p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
        status = "within-budget" if p95 <= budget else "over-budget"
        print(
            f"{name}_ms_min={min(samples):.2f} median={median(samples):.2f} "
            f"p95={p95:.2f} budget={budget:.0f} status={status}"
        )

    report("route_viewport", route_samples, 250)
    report("heatmap_viewport", heatmap_samples, 150)
    report("selected_route", selected_samples, 150)
    assert len(route_samples) == len(heatmap_samples) == len(selected_samples) == 20
