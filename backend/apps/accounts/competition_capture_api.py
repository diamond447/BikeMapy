"""Private, bounded capture territory and leaderboard projections."""

# DRF and GeoDjango do not currently ship complete type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db import connection
from django.db.models import Prefetch
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.exceptions import ParseError
from rest_framework.response import Response

from .activity_services import geometry_payload
from .competition_map_api import (
    MAX_MEMBERS,
    _member_ids,
    _parse_viewport,
    _requested_member_ids,
    _viewport_parts,
)
from .game_api import GameEndpoint, _private
from .models import (
    CaptureCalculation,
    CaptureFace,
    Competition,
    CompetitionMembership,
)
from .services import game_is_available

MAX_FACES = 1_200
MAX_RESPONSE_BYTES = 4_000_000


class CaptureOwnerSerializer(serializers.Serializer[dict[str, Any]]):
    player_id = serializers.IntegerField()
    display_name = serializers.CharField()
    nickname = serializers.CharField(allow_null=True)
    color = serializers.RegexField(regex=r"^#[0-9A-Fa-f]{6}$")
    shared_area_m2 = serializers.DecimalField(max_digits=20, decimal_places=3)


class CaptureFaceSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.IntegerField()
    geometry = serializers.JSONField()
    area_m2 = serializers.DecimalField(max_digits=20, decimal_places=3)
    effective_date = serializers.DateField(allow_null=True)
    shared = serializers.BooleanField()
    owners = CaptureOwnerSerializer(many=True)


class CaptureMemberSerializer(serializers.Serializer[dict[str, Any]]):
    player_id = serializers.IntegerField()
    display_name = serializers.CharField()
    nickname = serializers.CharField(allow_null=True)
    color = serializers.RegexField(regex=r"^#[0-9A-Fa-f]{6}$")
    is_owner = serializers.BooleanField()
    area_m2 = serializers.DecimalField(max_digits=20, decimal_places=3)
    rank = serializers.IntegerField()
    monthly_net_change_m2 = serializers.ListField(child=serializers.DictField())


class CaptureResponseSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=("fresh", "pending", "failed", "empty"))
    is_final = serializers.BooleanField()
    competition_id = serializers.UUIDField()
    generation = serializers.IntegerField(allow_null=True)
    snapshot_generation = serializers.IntegerField(allow_null=True)
    calculated_at = serializers.DateTimeField(allow_null=True)
    faces = CaptureFaceSerializer(many=True)
    members = CaptureMemberSerializer(many=True)
    help = serializers.DictField(child=serializers.CharField())
    limits = serializers.DictField(child=serializers.IntegerField())


CAPTURE_PARAMETERS = [
    OpenApiParameter(name, OpenApiTypes.NUMBER, required=True)
    for name in ("west", "south", "east", "north")
] + [
    OpenApiParameter("zoom", OpenApiTypes.INT, required=True),
    OpenApiParameter("member", OpenApiTypes.INT, many=True),
]


def _capture_member_ids(request: Any, memberships: list[CompetitionMembership]) -> set[int]:
    requested = _requested_member_ids(request)
    if requested is None:
        return {membership.player_id for membership in memberships[:MAX_MEMBERS]}
    if requested == {0}:
        return set()
    known = {membership.player_id for membership in memberships}
    if not requested <= known:
        raise ParseError("member is not in this competition")
    return _member_ids(request, memberships)


def _help_copy() -> dict[str, str]:
    return {
        "connection_rule": "Úseky se propojí, pokud jsou jejich mezery nejvýše 50 metrů.",
        "boundary_priority": "Starší hranice má přednost před novějším zásahem.",
        "same_day_sharing": "Území získané ve stejný den se dělí rovným dílem.",
        "pending": "Výsledky se přepočítávají; zobrazené skóre nemusí být konečné.",
        "failed": "Přepočet se nepodařil. Zobrazené skóre je poslední platný výsledek.",
    }


def _member_payload(
    membership: CompetitionMembership,
    competition: Competition,
    area: Decimal,
    rank: int,
    monthly: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "player_id": membership.player_id,
        "display_name": membership.player.strava_display_name,
        "nickname": membership.player.nickname or None,
        "color": membership.color,
        "is_owner": membership.player_id == competition.owner_id,
        "area_m2": area,
        "rank": rank,
        "monthly_net_change_m2": monthly,
    }


def _face_geometry_filter(queryset: Any, parts: list[tuple[float, float, float, float]]) -> Any:
    if connection.vendor != "postgresql":
        return queryset
    from django.contrib.gis.geos import Polygon
    from django.db.models import Q

    spatial = Q()
    for west, south, east, north in parts:
        spatial |= Q(geometry__intersects=Polygon.from_bbox((west, south, east, north)))
    return queryset.filter(spatial)


def _monthly_area(
    calculations: list[CaptureCalculation],
) -> dict[int, list[dict[str, Any]]]:
    """Return signed area deltas bucketed by each snapshot's completion month.

    A viewport is a presentation concern, so monthly history must not be derived
    from the faces returned for the current map.  The immutable player-area
    totals are the canonical values for each fresh generation.  Treating the
    first generation as a delta from zero also makes the history useful for a
    competition's initial result; later generations can produce negative
    deltas when territory is removed or reassigned.
    """

    totals: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    previous: dict[int, Decimal] = {}
    prague = ZoneInfo("Europe/Prague")

    for calculation in calculations:
        if calculation.completed_at is None:
            continue
        month = calculation.completed_at.astimezone(prague).strftime("%Y-%m")
        current = {area.player_id: area.owned_area_m2 for area in calculation.player_areas.all()}
        for player_id in set(previous) | set(current):
            totals[player_id][month] += current.get(player_id, Decimal("0")) - previous.get(
                player_id, Decimal("0")
            )
        previous = current

    return {
        player_id: [
            {"month": month, "net_change_m2": value.quantize(Decimal("0.001"))}
            for month, value in sorted(months.items())
            if value
        ]
        for player_id, months in totals.items()
    }


@extend_schema(
    parameters=CAPTURE_PARAMETERS,
    responses={200: CaptureResponseSerializer},
    auth=[{"cookieAuth": []}],  # type: ignore[list-item]
    tags=["game-capture"],
)
class CompetitionCaptureView(GameEndpoint):
    """Return only the authenticated member's competition capture snapshot."""

    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        return _private(super().dispatch(request, *args, **kwargs))

    def get(self, request: Any, competition_id: UUID) -> Response:
        if not game_is_available():
            return self.unavailable()
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        competition = (
            Competition.objects.filter(
                pk=competition_id,
                is_active=True,
                memberships__player=player,
            )
            .prefetch_related(
                Prefetch(
                    "memberships",
                    queryset=CompetitionMembership.objects.select_related("player").order_by(
                        "joined_at", "pk"
                    ),
                )
            )
            .first()
        )
        if competition is None:
            return _private(Response({"detail": "Competition not found."}, status=404))

        west, south, east, north, _zoom = _parse_viewport(request)
        memberships = list(competition.memberships.all())
        selected_ids = _capture_member_ids(request, memberships)
        latest = CaptureCalculation.objects.filter(
            competition=competition, generation=competition.revision
        ).first()
        current = (
            CaptureCalculation.objects.filter(
                competition=competition,
                is_current=True,
                status=CaptureCalculation.Status.FRESH,
            )
            .prefetch_related("faces__owners", "player_areas")
            .first()
        )
        calculation = current
        status = "empty"
        if latest is not None and latest.status in {
            CaptureCalculation.Status.PENDING,
            CaptureCalculation.Status.RUNNING,
        }:
            status = "pending"
        elif latest is not None and latest.status == CaptureCalculation.Status.FAILED:
            status = "failed"
        elif current is not None:
            status = "fresh"
        if calculation is None:
            faces: list[CaptureFace] = []
            all_faces: list[CaptureFace] = []
            area_by_player: dict[int, Decimal] = {}
            snapshot_generation = None
            calculated_at = None
        else:
            parts = _viewport_parts(west, east, south, north)
            face_query = _face_geometry_filter(
                calculation.faces.prefetch_related("owners__player").order_by("face_id"),
                parts,
            )
            all_faces = list(face_query[: MAX_FACES + 1])[:MAX_FACES]
            faces = [
                face
                for face in all_faces
                if any(owner.player_id in selected_ids for owner in face.owners.all())
            ]
            area_by_player = {
                area.player_id: area.owned_area_m2 for area in calculation.player_areas.all()
            }
            snapshot_generation = calculation.generation
            calculated_at = calculation.completed_at

        member_by_id = {membership.player_id: membership for membership in memberships}
        ranked_members = list(member_by_id.values())
        ranked_members.sort(
            key=lambda item: (
                -area_by_player.get(item.player_id, Decimal("0")),
                item.joined_at,
                item.pk,
            )
        )
        monthly_calculations = list(
            CaptureCalculation.objects.filter(
                competition=competition,
                status=CaptureCalculation.Status.FRESH,
            )
            .order_by("generation")
            .prefetch_related("player_areas")
        )
        monthly = _monthly_area(monthly_calculations)
        leaderboard = [
            _member_payload(
                membership,
                competition,
                area_by_player.get(membership.player_id, Decimal("0.000")),
                index + 1,
                monthly.get(membership.player_id, []),
            )
            for index, membership in enumerate(ranked_members)
        ]
        owner_by_id = {membership.player_id: membership for membership in memberships}
        face_payload = []
        for face in faces:
            owners = [owner for owner in face.owners.all() if owner.player_id in owner_by_id]
            face_payload.append(
                {
                    "id": face.face_id,
                    "geometry": geometry_payload(face.geometry),
                    "area_m2": face.area_m2,
                    "effective_date": face.effective_date,
                    "shared": len(owners) > 1,
                    "owners": [
                        {
                            "player_id": owner.player_id,
                            "display_name": owner_by_id[owner.player_id].player.strava_display_name,
                            "nickname": owner_by_id[owner.player_id].player.nickname or None,
                            "color": owner_by_id[owner.player_id].color,
                            "shared_area_m2": owner.shared_area_m2,
                        }
                        for owner in owners
                    ],
                }
            )
        payload = {
            "status": status,
            "is_final": status == "fresh"
            and current is not None
            and current.generation == competition.revision,
            "competition_id": competition.pk,
            "generation": competition.revision,
            "snapshot_generation": snapshot_generation,
            "calculated_at": calculated_at,
            "faces": face_payload,
            "members": leaderboard,
            "help": _help_copy(),
            "limits": {"max_faces": MAX_FACES, "max_response_bytes": MAX_RESPONSE_BYTES},
        }

        def encoded_size() -> int:
            return len(json.dumps(payload, default=str, separators=(",", ":")).encode())

        if encoded_size() > MAX_RESPONSE_BYTES:
            low, high = 0, len(face_payload)
            while low < high:
                middle = (low + high + 1) // 2
                payload["faces"] = face_payload[:middle]
                if encoded_size() <= MAX_RESPONSE_BYTES:
                    low = middle
                else:
                    high = middle - 1
            payload["faces"] = face_payload[:low]
            if encoded_size() > MAX_RESPONSE_BYTES:
                return _private(
                    Response(
                        {
                            "detail": "Capture response exceeds the response limit.",
                            "code": "response_limit",
                        },
                        status=413,
                    )
                )
        return _private(Response(payload))
