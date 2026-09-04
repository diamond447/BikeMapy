"""Derived spatial products and bounded map browse queries.

The canonical normalized route geometry is deliberately kept separate from
these products.  Browse geometries and heatmap memberships can be rebuilt
after a tolerance or grid-policy change without touching route history.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from .fields import _GIS_AVAILABLE
from .models import (
    Route,
    RouteBrowseGeometry,
    RouteHeatmapCell,
    RouteHeatmapMembership,
    RouteLifecycle,
    RouteVersion,
)

MAX_LATITUDE = 85.05112878
_CACHE_EPOCH_KEY = "bikemapy:spatial:epoch"


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def browse_zooms() -> tuple[int, ...]:
    value = _setting("SPATIAL_BROWSE_ZOOMS", "6,8,10,12,14,16,18")
    if isinstance(value, str):
        values = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    else:
        values = tuple(sorted({int(item) for item in value}))
    return tuple(zoom for zoom in values if 0 <= zoom <= 22) or (12,)


def heatmap_zooms() -> tuple[int, ...]:
    value = _setting("SPATIAL_HEATMAP_ZOOMS", "3,4,5,6,7,8")
    if isinstance(value, str):
        values = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    else:
        values = tuple(sorted({int(item) for item in value}))
    return tuple(zoom for zoom in values if 0 <= zoom <= 22) or (6,)


def heatmap_max_zoom() -> int:
    return max(heatmap_zooms())


def _coordinates(value: Any) -> list[tuple[float, float]]:
    if value is None:
        return []
    if hasattr(value, "coords"):
        return [(float(point[0]), float(point[1])) for point in value.coords]
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if isinstance(value, dict):
        value = value.get("coordinates", [])
    return [(float(point[0]), float(point[1])) for point in value]


def _geojson(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if hasattr(value, "geojson"):
        return cast(dict[str, Any], json.loads(value.geojson))
    if isinstance(value, str):
        try:
            return cast(dict[str, Any], json.loads(value))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _line_simplify(
    points: list[tuple[float, float]], tolerance_m: float
) -> list[tuple[float, float]]:
    """Douglas-Peucker fallback for the SQLite quality suite.

    Production PostGIS uses the same tolerance policy through GEOS-backed
    geometry simplification; converting metres to latitude degrees keeps the
    fallback deterministic and intentionally conservative.
    """

    if len(points) <= 2:
        return points
    tolerance = tolerance_m / 111_320.0

    def distance(
        point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]
    ) -> float:
        dx, dy = end[0] - start[0], end[1] - start[1]
        if dx == dy == 0:
            return math.hypot(point[0] - start[0], point[1] - start[1])
        t = max(
            0.0,
            min(
                1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)
            ),
        )
        return math.hypot(point[0] - (start[0] + t * dx), point[1] - (start[1] + t * dy))

    # An explicit stack avoids RecursionError on long GPX tracks and keeps the
    # fallback's work bounded by the number of vertices retained by DP.
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    pending = [(0, len(points) - 1)]
    while pending:
        start_index, end_index = pending.pop()
        greatest = tolerance
        split_index = -1
        for candidate in range(start_index + 1, end_index):
            current = distance(points[candidate], points[start_index], points[end_index])
            if current > greatest:
                greatest, split_index = current, candidate
        if split_index >= 0:
            keep[split_index] = True
            pending.append((start_index, split_index))
            pending.append((split_index, end_index))
    return [point for index, point in enumerate(points) if keep[index]]


def simplification_tolerance_m(zoom: int) -> float:
    if zoom < 0 or zoom > 22:
        raise ValidationError("zoom must be between 0 and 22")
    # Half a Web-Mercator pixel at the equator.  The floor avoids storing
    # pointless sub-millimetre vertices at close zooms.
    return float(max(0.5, 156543.03392804097 / (2**zoom) * 0.5))


def generate_browse_geometries(
    version: RouteVersion, *, zooms: tuple[int, ...] | None = None
) -> list[RouteBrowseGeometry]:
    """Generate or replace derived geometries for a route version."""

    source = version.normalized_geometry or version.simplified_geometry
    points = _coordinates(source)
    if len(points) < 2:
        return []
    result: list[RouteBrowseGeometry] = []
    for zoom in zooms or browse_zooms():
        tolerance = simplification_tolerance_m(zoom)
        geometry: Any
        if _GIS_AVAILABLE:
            from django.contrib.gis.geos import GEOSGeometry

            # Simplify in EPSG:3857 so the tolerance is metres.  Transforming
            # through GEOS uses the same robust implementation as PostGIS and
            # preserves topology for route lines at realistic GPX sizes.
            geometry = GEOSGeometry(
                json.dumps({"type": "LineString", "coordinates": points}), srid=4326
            )
            geometry.transform(3857)
            geometry = geometry.simplify(tolerance, preserve_topology=True)
            geometry.transform(4326)
        else:
            simplified = _line_simplify(points, tolerance)
            geometry = {"type": "LineString", "coordinates": simplified}
        row, _ = RouteBrowseGeometry.objects.update_or_create(
            version=version,
            zoom=zoom,
            defaults={
                "geometry": geometry,
                "tolerance_m": tolerance,
                "generated_at": timezone.now(),
            },
        )
        result.append(row)
    return result


def _tile_xy(longitude: float, latitude: float, zoom: int) -> tuple[int, int]:
    latitude = max(-MAX_LATITUDE, min(MAX_LATITUDE, latitude))
    scale = 2**zoom
    x = int(math.floor((longitude + 180.0) / 360.0 * scale))
    radians = math.radians(latitude)
    y = int(math.floor((1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * scale))
    return max(0, min(scale - 1, x)), max(0, min(scale - 1, y))


def _tile_bounds(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
    scale = 2**zoom
    west = x / scale * 360.0 - 180.0
    east = (x + 1) / scale * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / scale))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / scale))))
    return west, south, east, north


def _cell_geometry(x: int, y: int, zoom: int) -> Any:
    west, south, east, north = _tile_bounds(x, y, zoom)
    payload = {
        "type": "Polygon",
        "coordinates": [
            [[west, south], [east, south], [east, north], [west, north], [west, south]]
        ],
    }
    if _GIS_AVAILABLE:
        from django.contrib.gis.geos import GEOSGeometry

        return GEOSGeometry(json.dumps(payload), srid=4326)
    return payload


def _segment_intersects_cell(
    start: tuple[float, float], end: tuple[float, float], bounds: tuple[float, float, float, float]
) -> bool:
    west, south, east, north = bounds
    if west <= start[0] <= east and south <= start[1] <= north:
        return True
    if west <= end[0] <= east and south <= end[1] <= north:
        return True
    dx, dy = end[0] - start[0], end[1] - start[1]
    candidates = []
    if dx:
        candidates.extend([(west - start[0]) / dx, (east - start[0]) / dx])
    if dy:
        candidates.extend([(south - start[1]) / dy, (north - start[1]) / dy])
    return any(
        0 <= t <= 1 and west <= start[0] + t * dx <= east and south <= start[1] + t * dy <= north
        for t in candidates
    )


def _crossing_tiles(points: list[tuple[float, float]], zoom: int) -> set[tuple[int, int]]:
    if len(points) < 2:
        return set()
    minimum_x, minimum_y = _tile_xy(min(p[0] for p in points), max(p[1] for p in points), zoom)
    maximum_x, maximum_y = _tile_xy(max(p[0] for p in points), min(p[1] for p in points), zoom)
    candidate_count = (maximum_x - minimum_x + 1) * (maximum_y - minimum_y + 1)
    if candidate_count > int(_setting("SPATIAL_MAX_HEATMAP_CANDIDATE_CELLS", 100_000)):
        raise ValidationError("route geometry spans too many heatmap cells")
    output: set[tuple[int, int]] = set()
    for x in range(minimum_x, maximum_x + 1):
        for y in range(minimum_y, maximum_y + 1):
            bounds = _tile_bounds(x, y, zoom)
            if any(
                _segment_intersects_cell(a, b, bounds)
                for a, b in zip(points, points[1:], strict=False)
            ):
                output.add((x, y))
    return output


def _public_geometry(route: Route) -> Any:
    if not route.current_approved_version_id:
        return None
    version = route.current_approved_version
    return version.normalized_geometry or version.simplified_geometry if version else None


def _full_geometry(route: Route) -> Any:
    version = route.current_approved_version
    return version.normalized_geometry if version else None


@transaction.atomic
def refresh_route_heatmap(
    route: Route, *, before_cell_lock: Callable[[], None] | None = None
) -> set[int]:
    """Refresh memberships for one route and recalculate only touched cells."""

    # Lock only the route row. PostgreSQL rejects FOR UPDATE on the nullable
    # side of the current-approved-version outer join.
    route = Route.objects.select_for_update().get(pk=route.pk)
    if route.current_approved_version_id:
        route.current_approved_version = RouteVersion.objects.get(
            pk=route.current_approved_version_id
        )
    old_cells = set(
        RouteHeatmapMembership.objects.filter(route=route).values_list("cell_id", flat=True)
    )
    geometry = _public_geometry(route) if route.lifecycle == RouteLifecycle.PUBLISHED else None
    new_tiles_by_zoom: dict[int, set[tuple[int, int]]] = {}
    if geometry is not None:
        points = _coordinates(geometry)
        for zoom in heatmap_zooms():
            crossing_tiles = _crossing_tiles(points, zoom)
            if _GIS_AVAILABLE and connection.vendor == "postgresql" and crossing_tiles:
                # The Python bbox pass bounds the candidate set; PostGIS is
                # the authority for the final line/cell intersection test.
                existing_cells = RouteHeatmapCell.objects.filter(
                    zoom=zoom,
                    x__in=[x for x, _ in crossing_tiles],
                    y__in=[y for _, y in crossing_tiles],
                )
                existing_tiles = set(existing_cells.values_list("x", "y"))
                if existing_tiles:
                    crossing_cells = existing_cells.filter(boundary__intersects=geometry)
                    crossing_tiles = (crossing_tiles - existing_tiles) | set(
                        crossing_cells.values_list("x", "y")
                    )
            new_tiles_by_zoom[zoom] = crossing_tiles
    # Membership writes do not touch the shared counter row. This lets
    # competing refreshes overlap; the stable cell lock below serializes their
    # read/replace of route_count. Empty cells are retained to avoid a delete
    # racing a concurrent membership insert.
    new_cells: dict[tuple[int, int, int], RouteHeatmapCell] = {}
    for zoom, tiles in new_tiles_by_zoom.items():
        for x, y in sorted(tiles):
            cell, _ = RouteHeatmapCell.objects.get_or_create(
                zoom=zoom,
                x=x,
                y=y,
                defaults={"boundary": _cell_geometry(x, y, zoom)},
            )
            new_cells[(zoom, x, y)] = cell
    affected = set(old_cells) | {cell.pk for cell in new_cells.values()}
    RouteHeatmapMembership.objects.filter(route=route).delete()
    for cell in new_cells.values():
        RouteHeatmapMembership.objects.get_or_create(cell=cell, route=route)
    if before_cell_lock is not None:
        before_cell_lock()
    locked_cells = list(
        RouteHeatmapCell.objects.select_for_update().filter(pk__in=affected).order_by("pk")
    )
    for candidate in locked_cells:
        count = RouteHeatmapMembership.objects.filter(cell=candidate).count()
        if count:
            if candidate.route_count != count:
                candidate.route_count = count
                candidate.save(update_fields=["route_count", "updated_at"])
        else:
            if candidate.route_count:
                candidate.route_count = 0
                candidate.save(update_fields=["route_count", "updated_at"])
    schedule_spatial_cache_invalidation()
    return affected


def bump_spatial_cache_epoch() -> int:
    try:
        cache.add(_CACHE_EPOCH_KEY, 0, timeout=None)
        return int(cache.incr(_CACHE_EPOCH_KEY))
    except Exception:  # pragma: no cover - cache outage is deployment-specific
        try:
            epoch = int(cache.get(_CACHE_EPOCH_KEY, 0) or 0) + 1
        except Exception:
            return 0
        try:
            cache.set(_CACHE_EPOCH_KEY, epoch, timeout=None)
        except Exception:
            pass
        return epoch


def schedule_spatial_cache_invalidation() -> None:
    """Advance the shared cache epoch only after the surrounding transaction commits."""

    transaction.on_commit(bump_spatial_cache_epoch)


def _validate_bounds(west: float, south: float, east: float, north: float) -> None:
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValidationError(
            "viewport bounds must satisfy -180 <= west < east <= 180 and -90 <= south < north <= 90"
        )
    if east - west > float(_setting("SPATIAL_MAX_VIEWPORT_WIDTH_DEGREES", 90)):
        raise ValidationError("viewport is too wide")
    if north - south > float(_setting("SPATIAL_MAX_VIEWPORT_HEIGHT_DEGREES", 90)):
        raise ValidationError("viewport is too tall")


def _route_dict(route: Route, geometry: Any) -> dict[str, Any]:
    return {
        "id": str(route.pk),
        "slug": route.slug,
        "title": route.effective_title,
        "geometry": _geojson(geometry),
    }


def _route_values_dict(route: dict[str, Any], geometry: Any) -> dict[str, Any]:
    return {
        "id": str(route["id"]),
        "slug": route["slug"],
        "title": route["admin_title_override"]
        or route["display_title"]
        or route["generated_title"],
        "geometry": _geojson(geometry),
    }


@dataclass(frozen=True)
class SpatialQueryLimits:
    max_routes: int = 500
    max_cells: int = 10_000

    def __post_init__(self) -> None:
        hard_routes = int(_setting("SPATIAL_HARD_MAX_ROUTES_PER_QUERY", 500))
        hard_cells = int(_setting("SPATIAL_HARD_MAX_CELLS_PER_QUERY", 10_000))
        if self.max_routes < 0 or self.max_cells < 0:
            raise ValidationError("query limits cannot be negative")
        object.__setattr__(self, "max_routes", min(self.max_routes, hard_routes))
        object.__setattr__(self, "max_cells", min(self.max_cells, hard_cells))


def query_viewport(
    *,
    west: float,
    south: float,
    east: float,
    north: float,
    zoom: int,
    limits: SpatialQueryLimits | None = None,
) -> dict[str, Any]:
    """Return heatmap cells or simplified lines for a bounded viewport."""

    _validate_bounds(west, south, east, north)
    if not 0 <= zoom <= 22:
        raise ValidationError("zoom must be between 0 and 22")
    limits = limits or SpatialQueryLimits(
        max_routes=int(_setting("SPATIAL_MAX_ROUTES_PER_QUERY", 500)),
        max_cells=int(_setting("SPATIAL_MAX_CELLS_PER_QUERY", 10_000)),
    )
    epoch = int(cache.get(_CACHE_EPOCH_KEY, 0))
    raw_key = f"{west}:{south}:{east}:{north}:{zoom}:{limits.max_routes}:{limits.max_cells}:{epoch}"
    cache_key = "bikemapy:spatial:viewport:" + hashlib.sha256(raw_key.encode()).hexdigest()
    cached = cache.get(cache_key)
    if cached is not None:
        return cast(dict[str, Any], cached)
    selected_heatmap_zoom = next((item for item in heatmap_zooms() if item >= zoom), None)
    if selected_heatmap_zoom is None and zoom <= heatmap_max_zoom():
        selected_heatmap_zoom = heatmap_max_zoom()
    if selected_heatmap_zoom is not None and zoom <= heatmap_max_zoom():
        cell_query = RouteHeatmapCell.objects.filter(zoom=selected_heatmap_zoom, route_count__gt=0)
        if _GIS_AVAILABLE and connection.vendor == "postgresql":
            from django.contrib.gis.geos import Polygon

            cell_query = cell_query.filter(
                boundary__intersects=Polygon.from_bbox((west, south, east, north))
            )
        cells = list(cell_query.order_by("-route_count", "y", "x")[: limits.max_cells + 1])
        items = []
        for cell in cells[: limits.max_cells]:
            bounds = _tile_bounds(cell.x, cell.y, cell.zoom)
            if (
                bounds[2] >= west
                and bounds[0] <= east
                and bounds[3] >= south
                and bounds[1] <= north
            ):
                items.append(
                    {
                        "zoom": cell.zoom,
                        "x": cell.x,
                        "y": cell.y,
                        "count": cell.route_count,
                        "geometry": _geojson(cell.boundary),
                    }
                )
        result = {
            "mode": "heatmap",
            "zoom": zoom,
            "data_zoom": selected_heatmap_zoom,
            "cells": items,
            "routes": [],
            "truncated": len(cells) > limits.max_cells,
        }
    else:
        candidates = Route.objects.filter(
            lifecycle=RouteLifecycle.PUBLISHED, current_approved_version__isnull=False
        )
        if _GIS_AVAILABLE and connection.vendor == "postgresql":
            from django.contrib.gis.geos import Polygon

            viewport_geometry = Polygon.from_bbox((west, south, east, north))
            intersecting_versions = RouteBrowseGeometry.objects.filter(
                geometry__intersects=viewport_geometry, zoom__lte=zoom
            ).values("version_id")
            candidates = candidates.filter(current_approved_version_id__in=intersecting_versions)
        candidate_scan_limit = max(
            limits.max_routes + 1,
            int(_setting("SPATIAL_MAX_CANDIDATE_SCAN", 5_000)),
        )
        candidate_routes = cast(
            list[dict[str, Any]],
            list(
                candidates.values(
                    "id",
                    "slug",
                    "display_title",
                    "generated_title",
                    "admin_title_override",
                    "current_approved_version_id",
                )[:candidate_scan_limit]
            ),
        )
        version_ids = [
            route["current_approved_version_id"]
            for route in candidate_routes
            if route["current_approved_version_id"] is not None
        ]
        browse_rows = RouteBrowseGeometry.objects.filter(
            version_id__in=version_ids, zoom__lte=zoom
        ).order_by("version_id", "-zoom")
        browse_by_version: dict[Any, Any] = {}
        if _GIS_AVAILABLE and connection.vendor == "postgresql":
            from django.contrib.gis.db.models.functions import AsGeoJSON

            annotated_browse_rows = browse_rows.annotate(
                _geometry_json=AsGeoJSON("geometry")
            ).values("version_id", "_geometry_json")
            for browse in annotated_browse_rows:
                browse_by_version.setdefault(
                    browse["version_id"], json.loads(browse["_geometry_json"])
                )
        else:
            for browse_geometry in browse_rows:
                browse_by_version.setdefault(browse_geometry.version_id, browse_geometry.geometry)
        missing_version_ids = set(version_ids) - set(browse_by_version)
        fallback_geometry_by_version: dict[Any, Any] = {}
        if missing_version_ids:
            fallback_geometry_by_version = {
                # A viewport must never fall back to the full normalized
                # payload; selected-route queries are the full-geometry boundary.
                version.pk: version.simplified_geometry
                for version in RouteVersion.objects.filter(pk__in=missing_version_ids)
            }
        rows: list[dict[str, Any]] = []
        for route in candidate_routes:
            version_id = route["current_approved_version_id"]
            geometry = browse_by_version.get(version_id)
            geometry = geometry or fallback_geometry_by_version.get(version_id)
            points = _coordinates(geometry)
            if (
                not points
                or max(p[0] for p in points) < west
                or min(p[0] for p in points) > east
                or max(p[1] for p in points) < south
                or min(p[1] for p in points) > north
            ):
                continue
            rows.append(_route_values_dict(route, geometry))
            if len(rows) > limits.max_routes:
                break
        result = {
            "mode": "routes",
            "zoom": zoom,
            "cells": [],
            "routes": rows[: limits.max_routes],
            # A full candidate scan is intentionally bounded; reaching that
            # bound means there may be qualifying simplified lines beyond it.
            "truncated": len(rows) > limits.max_routes
            or len(candidate_routes) >= candidate_scan_limit,
        }
    cache.set(cache_key, result, timeout=int(_setting("SPATIAL_QUERY_CACHE_TTL", 60)))
    return result


def query_selected_route(route_id: Any = None, *, slug: str | None = None) -> dict[str, Any]:
    """Return metadata and full normalized geometry for one public route."""

    if route_id is None and not slug:
        raise ValidationError("route_id or slug is required")
    query = Route.objects.filter(
        lifecycle=RouteLifecycle.PUBLISHED, current_approved_version__isnull=False
    ).select_related("current_approved_version")
    route = query.filter(slug=slug).first() if slug else query.filter(pk=route_id).first()
    if route is None or route.current_approved_version is None:
        raise ValidationError("public route was not found")
    geometry = _full_geometry(route)
    if geometry is None:
        raise ValidationError("public route has no geometry")
    return _route_dict(route, geometry)


# Short aliases make the data products convenient to import from callers that
# use their product names rather than their persistence names.
build_browse_geometries = generate_browse_geometries
refresh_heatmap = refresh_route_heatmap
viewport_query = query_viewport
selected_route_query = query_selected_route
