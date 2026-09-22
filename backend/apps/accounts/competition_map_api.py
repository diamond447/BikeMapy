"""Bounded, private map data for competition members."""

# drf-spectacular and Django GIS do not currently ship complete type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from django.contrib.gis.db.models import GeometryField
from django.contrib.gis.db.models.functions import GeoFunc
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ParseError
from rest_framework.response import Response

from .activity_services import geometry_payload
from .game_api import GameEndpoint, _private
from .models import Competition, CompetitionMembership, ImportedActivity, Player
from .services import game_is_available

MAX_FEATURES = 1_200
MAX_FEATURES_PER_MEMBER = 240
MAX_COORDINATES = 120_000
MAX_CANDIDATES_SQLITE = MAX_FEATURES * 8
MAX_SOURCE_COORDINATES = 4_000
MAX_RESPONSE_BYTES = 4_000_000
MAX_MEMBERS = 100
MAX_MEMBER_METADATA_BYTES = 64_000
MIN_ZOOM = 0
MAX_ZOOM = 22


class STSimplify(GeoFunc):
    """PostGIS simplification function missing from Django's GIS helpers."""

    function = "ST_Simplify"
    output_field = GeometryField(srid=3857)


class CompetitionMapMemberSerializer(serializers.Serializer[dict[str, Any]]):
    player_id = serializers.IntegerField()
    display_name = serializers.CharField()
    nickname = serializers.CharField(allow_null=True)
    color = serializers.RegexField(regex=r"^#[0-9A-Fa-f]{6}$")
    is_owner = serializers.BooleanField()


class CompetitionMapActivitySerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField()
    player_id = serializers.IntegerField()
    calendar_date = serializers.DateField(allow_null=True)
    geometry = serializers.JSONField()


class CompetitionMapResponseSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=("loaded", "empty", "syncing"))
    competition_id = serializers.UUIDField()
    members = CompetitionMapMemberSerializer(many=True)
    activities = CompetitionMapActivitySerializer(many=True)
    truncated = serializers.BooleanField()
    limits = serializers.DictField(child=serializers.IntegerField())
    bounds = serializers.DictField(child=serializers.FloatField())


MAP_PARAMETERS = [
    OpenApiParameter(name, OpenApiTypes.NUMBER, required=True)
    for name in ("west", "south", "east", "north")
] + [
    OpenApiParameter("zoom", OpenApiTypes.INT, required=True),
    OpenApiParameter("member", OpenApiTypes.INT, many=True),
]


def _number(value: str | None, name: str) -> float:
    if value in (None, ""):
        raise ParseError(f"{name} is required")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ParseError(f"{name} must be a number") from None
    if not math.isfinite(result):
        raise ParseError(f"{name} must be finite")
    return result


def _zoom(value: str | None) -> int:
    if value in (None, ""):
        raise ParseError("zoom is required")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ParseError("zoom must be an integer") from None
    if result < MIN_ZOOM or result > MAX_ZOOM:
        raise ParseError(f"zoom must be between {MIN_ZOOM} and {MAX_ZOOM}")
    return result


def _parse_viewport(request: Any) -> tuple[float, float, float, float, int]:
    west = _number(request.query_params.get("west"), "west")
    south = _number(request.query_params.get("south"), "south")
    east = _number(request.query_params.get("east"), "east")
    north = _number(request.query_params.get("north"), "north")
    zoom = _zoom(request.query_params.get("zoom"))
    if not (-180 <= west < east <= 180):
        if not (-180 <= west <= 180 and -180 <= east <= 180 and west != east):
            raise ParseError("viewport longitude bounds are invalid")
    if not (-90 <= south < north <= 90):
        raise ParseError("viewport latitude bounds are invalid")
    # A global request at once would defeat the purpose of viewport bounds.
    longitude_width = (east - west) % 360 or 360
    is_world = west == -180 and east == 180
    if (longitude_width > 120 and not is_world) or north - south > 90:
        raise ParseError("viewport is too large")
    return west, south, east, north, zoom


def _coordinates(geometry: dict[str, Any]) -> list[tuple[float, float]]:
    raw = geometry.get("coordinates", [])
    if not isinstance(raw, list):
        return []
    result: list[tuple[float, float]] = []
    for point in raw:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            longitude, latitude = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(longitude) and math.isfinite(latitude):
            result.append((longitude, latitude))
    return result


def _line_parts(geometry: dict[str, Any]) -> list[list[tuple[float, float]]]:
    if geometry.get("type") == "LineString":
        points = _coordinates(geometry)
        return [points] if len(points) >= 2 else []
    if geometry.get("type") == "MultiLineString":
        result = []
        for raw in geometry.get("coordinates", []):
            points = _coordinates({"coordinates": raw})
            if len(points) >= 2:
                result.append(points)
        return result
    return []


def _line_simplify(
    points: list[tuple[float, float]], tolerance: float
) -> list[tuple[float, float]]:
    """Iterative Douglas-Peucker fallback for the SQLite quality suite."""
    if len(points) <= 2:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    pending = [(0, len(points) - 1)]
    tolerance = max(tolerance / 111_320.0, 0.000001)
    while pending:
        start, end = pending.pop()
        dx, dy = points[end][0] - points[start][0], points[end][1] - points[start][1]
        greatest, split = tolerance, -1
        for index in range(start + 1, end):
            if dx == dy == 0:
                distance = math.hypot(
                    points[index][0] - points[start][0],
                    points[index][1] - points[start][1],
                )
            else:
                factor = max(
                    0.0,
                    min(
                        1.0,
                        (
                            (points[index][0] - points[start][0]) * dx
                            + (points[index][1] - points[start][1]) * dy
                        )
                        / (dx * dx + dy * dy),
                    ),
                )
                distance = math.hypot(
                    points[index][0] - (points[start][0] + factor * dx),
                    points[index][1] - (points[start][1] + factor * dy),
                )
            if distance > greatest:
                greatest, split = distance, index
        if split >= 0:
            keep[split] = True
            pending.extend(((start, split), (split, end)))
    return [point for index, point in enumerate(points) if keep[index]]


def _viewport_parts(
    west: float, east: float, south: float, north: float
) -> list[tuple[float, float, float, float]]:
    if west <= east:
        return [(west, south, east, north)]
    return [(west, south, 180.0, north), (-180.0, south, east, north)]


def _clip_points(
    points: list[tuple[float, float]], bounds: tuple[float, float, float, float]
) -> list[list[tuple[float, float]]]:
    """Clip line segments to a bbox for the GIS-less quality suite."""
    west, south, east, north = bounds
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for index in range(len(points) - 1):
        start, end = points[index], points[index + 1]
        x0, y0 = start
        x1, y1 = end
        dx, dy = x1 - x0, y1 - y0
        parameters = [0.0, 1.0]
        for p, q in ((-dx, x0 - west), (dx, east - x0), (-dy, y0 - south), (dy, north - y0)):
            if p == 0:
                if q < 0:
                    parameters = []
                    break
                continue
            ratio = q / p
            if p < 0:
                if ratio > parameters[1]:
                    parameters = []
                    break
                parameters[0] = max(parameters[0], ratio)
            else:
                if ratio < parameters[0]:
                    parameters = []
                    break
                parameters[1] = min(parameters[1], ratio)
        if not parameters:
            if len(current) > 1:
                segments.append(current)
            current = []
            continue
        first = (x0 + parameters[0] * dx, y0 + parameters[0] * dy)
        last = (x0 + parameters[1] * dx, y0 + parameters[1] * dy)
        if not current or current[-1] != first:
            current.append(first)
        current.append(last)
    if len(current) > 1:
        segments.append(current)
    return segments


def _simplify(
    geometry: Any,
    zoom: int,
    bounds: tuple[float, float, float, float] | None = None,
    *,
    already_simplified: bool = False,
) -> dict[str, Any] | None:
    payload = geometry_payload(geometry)
    if not payload or payload.get("type") not in {"LineString", "MultiLineString"}:
        return None
    line_parts = _line_parts(payload)
    if not line_parts:
        return None
    tolerance = max(0.5, 156543.03392804097 / (2**zoom) * 0.5)
    if connection.vendor == "postgresql":
        try:
            from django.contrib.gis.geos import GEOSGeometry

            line = GEOSGeometry(json.dumps(payload), srid=4326)
            if bounds is not None:
                west, south, east, north = bounds
                envelope = GEOSGeometry(
                    json.dumps(
                        {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [west, south],
                                    [east, south],
                                    [east, north],
                                    [west, north],
                                    [west, south],
                                ]
                            ],
                        }
                    ),
                    srid=4326,
                )
                line = line.intersection(envelope)
            if not already_simplified:
                line.transform(3857)
                line = line.simplify(tolerance, preserve_topology=True)
                line.transform(4326)
            payload = geometry_payload(line)
            if payload:
                line_parts = _line_parts(payload)
        except (TypeError, ValueError, ValidationError):
            pass
    else:
        clipped_parts: list[list[tuple[float, float]]] = []
        for points in line_parts:
            clipped_parts.extend(_clip_points(points, bounds) if bounds is not None else [points])
        if not clipped_parts:
            return None
        line_parts = [_line_simplify(points, tolerance) for points in clipped_parts]
    line_parts = [points for points in line_parts if len(points) >= 2]
    if not line_parts:
        return None
    if len(line_parts) == 1:
        return {
            "type": "LineString",
            "coordinates": [[round(x, 6), round(y, 6)] for x, y in line_parts[0]],
        }
    return {
        "type": "MultiLineString",
        "coordinates": [[[round(x, 6), round(y, 6)] for x, y in points] for points in line_parts],
    }


def _member_ids(request: Any, memberships: Sequence[CompetitionMembership]) -> set[int]:
    values = request.query_params.getlist("member")
    if not values:
        return {membership.player_id for membership in memberships[:MAX_MEMBERS]}
    if len(values) > MAX_MEMBERS:
        raise ParseError(f"member accepts at most {MAX_MEMBERS} values")
    try:
        selected = {int(value) for value in values}
    except (TypeError, ValueError):
        raise ParseError("member must be a player id") from None
    known = {membership.player_id for membership in memberships}
    # The client uses zero as an explicit, non-member sentinel when every
    # visibility checkbox is off; it must not silently mean "all members".
    if selected == {0}:
        return set()
    if not selected <= known:
        raise ParseError("member is not in this competition")
    return selected


def _member_payload(membership: CompetitionMembership, competition: Competition) -> dict[str, Any]:
    return {
        "player_id": membership.player_id,
        "display_name": membership.player.strava_display_name,
        "nickname": membership.player.nickname or None,
        "color": membership.color,
        "is_owner": membership.player_id == competition.owner_id,
    }


@extend_schema(
    parameters=MAP_PARAMETERS,
    responses={200: CompetitionMapResponseSerializer},
    auth=[{"cookieAuth": []}],  # type: ignore[list-item]
    tags=["game-map"],
)
class CompetitionMapView(GameEndpoint):
    """Return only date-labelled traces inside an authenticated viewport."""

    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        return _private(super().dispatch(request, *args, **kwargs))

    def get(self, request: Any, competition_id: UUID) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        competition = Competition.objects.filter(
            pk=competition_id,
            is_active=True,
            memberships__player=player,
        ).first()
        # Keep absent, inactive, and non-member competitions indistinguishable.
        if competition is None:
            return _private(Response({"detail": "Competition not found."}, status=404))
        west, south, east, north, zoom = _parse_viewport(request)
        viewport_parts = _viewport_parts(west, east, south, north)
        memberships = list(
            CompetitionMembership.objects.filter(competition=competition)
            .select_related("player")
            .order_by("joined_at", "pk")
        )
        selected_ids = _member_ids(request, memberships)
        members = [
            _member_payload(membership, competition)
            for membership in memberships
            if membership.player_id in selected_ids
        ]
        member_bytes = len(json.dumps(members, separators=(",", ":")).encode())
        if member_bytes > MAX_MEMBER_METADATA_BYTES:
            return _private(
                Response(
                    {
                        "detail": "Competition member metadata exceeds the map response limit.",
                        "code": "member_limit",
                    },
                    status=413,
                )
            )
        queryset = ImportedActivity.objects.filter(
            player_id__in=selected_ids,
            removed_at__isnull=True,
            geometry__isnull=False,
        ).order_by("calendar_date", "pk")
        if connection.vendor == "postgresql":
            from django.contrib.gis.db.models.functions import (
                Intersection,
                NumPoints,
                Transform,
            )
            from django.contrib.gis.geos import Polygon

            envelopes = [Polygon.from_bbox(part) for part in viewport_parts]
            spatial_filter = Q()
            for envelope in envelopes:
                spatial_filter |= Q(geometry__intersects=envelope)
            clipped_candidates = (
                queryset.filter(spatial_filter)
                .only("pk", "player_id", "calendar_date")
                .annotate(source_points=NumPoints("geometry"))
                .filter(source_points__lte=MAX_SOURCE_COORDINATES)
            )
            candidate_count = clipped_candidates.count()
            candidates: list[ImportedActivity] = []
            tolerance = max(0.5, 156543.03392804097 / (2**zoom) * 0.5)
            for envelope in envelopes:
                clipped_geometry = Intersection("geometry", envelope)
                candidates.extend(
                    clipped_candidates.filter(geometry__intersects=envelope).annotate(
                        private_geometry=Transform(
                            STSimplify(Transform(clipped_geometry, 3857), tolerance), 4326
                        )
                    )[: MAX_FEATURES + 1]
                )
        else:
            candidates = list(queryset[:MAX_CANDIDATES_SQLITE])
            candidate_count = len(candidates)
        candidate_geometries: dict[UUID, tuple[ImportedActivity, list[Any]]] = {}
        for candidate in candidates:
            source_geometry = getattr(candidate, "private_geometry", None)
            if source_geometry is None:
                source_geometry = candidate.geometry
            entry = candidate_geometries.setdefault(candidate.pk, (candidate, []))
            entry[1].append(source_geometry)
        activities: list[dict[str, Any]] = []
        member_feature_counts: dict[int, int] = {}
        coordinate_count = 0
        truncated = candidate_count > MAX_FEATURES or len(candidate_geometries) > MAX_FEATURES
        for activity, geometries in candidate_geometries.values():
            if len(activities) >= MAX_FEATURES:
                truncated = True
                break
            if member_feature_counts.get(activity.player_id, 0) >= MAX_FEATURES_PER_MEMBER:
                truncated = True
                continue
            payloads = [
                value
                for value in (geometry_payload(item) for item in geometries)
                if value is not None
            ]
            if not payloads:
                continue
            payload: dict[str, Any] = payloads[0]
            if len(payloads) > 1:
                merged_parts = [
                    part for payload_part in payloads for part in _line_parts(payload_part)
                ]
                payload = {
                    "type": "MultiLineString",
                    "coordinates": merged_parts,
                }
            if connection.vendor != "postgresql" and len(viewport_parts) > 1:
                clipped_parts = []
                for part in _line_parts(payload):
                    for viewport_part in viewport_parts:
                        clipped_parts.extend(_clip_points(part, viewport_part))
                payload = {
                    "type": "MultiLineString",
                    "coordinates": [part for part in clipped_parts if len(part) >= 2],
                }
            points = [point for part in _line_parts(payload or {}) for point in part]
            if connection.vendor != "postgresql":
                clipped = [
                    segment
                    for line_part in _line_parts(payload or {})
                    for part in viewport_parts
                    for segment in _clip_points(line_part, part)
                ]
                if not points or not clipped:
                    continue
                if len(points) > MAX_SOURCE_COORDINATES:
                    truncated = True
                    continue
            geometry = _simplify(
                payload,
                zoom,
                viewport_parts[0] if len(viewport_parts) == 1 else None,
                already_simplified=connection.vendor == "postgresql",
            )
            if geometry is None:
                continue
            geometry_coordinates = geometry.get("coordinates", [])
            coordinate_count += (
                sum(len(part) for part in geometry_coordinates)
                if geometry.get("type") == "MultiLineString"
                else len(geometry_coordinates)
            )
            if coordinate_count > MAX_COORDINATES:
                truncated = True
                break
            activities.append(
                {
                    "id": activity.pk,
                    "player_id": activity.player_id,
                    "calendar_date": activity.calendar_date,
                    "geometry": geometry,
                }
            )
            member_feature_counts[activity.player_id] = (
                member_feature_counts.get(activity.player_id, 0) + 1
            )
        sync_statuses = set(
            player_row.strava_sync_state.status
            for player_row in Player.objects.filter(pk__in=selected_ids).select_related(
                "strava_sync_state"
            )
            if hasattr(player_row, "strava_sync_state")
        )
        if sync_statuses & {"queued", "running"}:
            state = "syncing"
        else:
            state = "loaded" if activities else "empty"
        limits = {
            "max_features": MAX_FEATURES,
            "max_features_per_member": MAX_FEATURES_PER_MEMBER,
            "max_coordinates": MAX_COORDINATES,
            "max_source_coordinates": MAX_SOURCE_COORDINATES,
            "max_response_bytes": MAX_RESPONSE_BYTES,
            "max_members": MAX_MEMBERS,
            "max_member_metadata_bytes": MAX_MEMBER_METADATA_BYTES,
        }
        response_payload = {
            "status": state,
            "competition_id": competition.pk,
            "members": members,
            "activities": activities,
            "truncated": truncated,
            "limits": limits,
            "bounds": {"west": west, "south": south, "east": east, "north": north},
        }
        encoded_response = json.dumps(response_payload, default=str, separators=(",", ":")).encode()
        if len(encoded_response) > MAX_RESPONSE_BYTES and activities:
            keep_ratio = MAX_RESPONSE_BYTES / len(encoded_response)
            keep_count = max(1, int(len(activities) * keep_ratio * 0.98))
            del activities[keep_count:]
            response_payload["truncated"] = True
            while (
                len(json.dumps(response_payload, default=str, separators=(",", ":")).encode())
                > MAX_RESPONSE_BYTES
                and activities
            ):
                activities.pop()
        return _private(Response(response_payload))
