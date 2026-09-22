"""Bounded, private map data for competition members."""

# drf-spectacular and Django GIS do not currently ship complete type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import QuerySet
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
MAX_COORDINATES = 120_000
MAX_CANDIDATES_SQLITE = MAX_FEATURES * 8
MIN_ZOOM = 0
MAX_ZOOM = 22


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
        raise ParseError("viewport longitude bounds are invalid")
    if not (-90 <= south < north <= 90):
        raise ParseError("viewport latitude bounds are invalid")
    # A global request at once would defeat the purpose of viewport bounds.
    if east - west > 120 or north - south > 90:
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


def _simplify(geometry: Any, zoom: int) -> dict[str, Any] | None:
    payload = geometry_payload(geometry)
    if not payload or payload.get("type") != "LineString":
        return None
    points = _coordinates(payload)
    if len(points) < 2:
        return None
    tolerance = max(0.5, 156543.03392804097 / (2**zoom) * 0.5)
    if connection.vendor == "postgresql":
        try:
            from django.contrib.gis.geos import GEOSGeometry

            line = GEOSGeometry(payload, srid=4326)
            line.transform(3857)
            line = line.simplify(tolerance, preserve_topology=True)
            line.transform(4326)
            payload = geometry_payload(line)
            if payload:
                points = _coordinates(payload)
        except (TypeError, ValueError, ValidationError):
            pass
    else:
        points = _line_simplify(points, tolerance)
    if len(points) < 2:
        return None
    return {"type": "LineString", "coordinates": [[round(x, 6), round(y, 6)] for x, y in points]}


def _member_ids(request: Any, memberships: QuerySet[CompetitionMembership]) -> set[int]:
    values = request.query_params.getlist("member")
    if not values:
        return set(memberships.values_list("player_id", flat=True))
    try:
        selected = {int(value) for value in values}
    except (TypeError, ValueError):
        raise ParseError("member must be a player id") from None
    known = set(memberships.values_list("player_id", flat=True))
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
        memberships = CompetitionMembership.objects.filter(competition=competition).select_related(
            "player"
        )
        selected_ids = _member_ids(request, memberships)
        members = [
            _member_payload(membership, competition)
            for membership in memberships
            if membership.player_id in selected_ids
        ]
        queryset = ImportedActivity.objects.filter(
            player_id__in=selected_ids,
            removed_at__isnull=True,
            geometry__isnull=False,
        ).order_by("calendar_date", "pk")
        if connection.vendor == "postgresql":
            from django.contrib.gis.geos import Polygon

            viewport = Polygon.from_bbox((west, south, east, north))
            queryset = queryset.filter(geometry__intersects=viewport)
            candidates = list(queryset[: MAX_FEATURES + 1])
        else:
            candidates = list(queryset[:MAX_CANDIDATES_SQLITE])
        activities: list[dict[str, Any]] = []
        coordinate_count = 0
        truncated = len(candidates) > MAX_FEATURES
        for activity in candidates:
            if len(activities) >= MAX_FEATURES:
                truncated = True
                break
            payload = geometry_payload(activity.geometry)
            points = _coordinates(payload or {})
            if not points or not any(west <= x <= east and south <= y <= north for x, y in points):
                continue
            geometry = _simplify(activity.geometry, zoom)
            if geometry is None:
                continue
            coordinate_count += len(geometry["coordinates"])
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
        return _private(
            Response(
                {
                    "status": state,
                    "competition_id": competition.pk,
                    "members": members,
                    "activities": activities,
                    "truncated": truncated,
                    "limits": {"max_features": MAX_FEATURES, "max_coordinates": MAX_COORDINATES},
                }
            )
        )
