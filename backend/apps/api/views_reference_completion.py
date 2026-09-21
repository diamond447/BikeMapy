"""Private completion projections for reference routes."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from apps.accounts.game_api import GameEndpoint, _private
from apps.reference_routes.models import ReferenceRoute, RouteCompletion


class CompletionProjectionSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.CharField()
    total_length_meters = serializers.DecimalField(max_digits=14, decimal_places=3)
    covered_length_meters = serializers.DecimalField(max_digits=14, decimal_places=3)
    completion_percent = serializers.DecimalField(max_digits=7, decimal_places=3)
    calculated_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField()


class ReferenceCompletionSerializer(serializers.Serializer[dict[str, Any]]):
    route_id = serializers.UUIDField()
    version = serializers.IntegerField()
    player = CompletionProjectionSerializer(allow_null=True)
    competition = CompletionProjectionSerializer(allow_null=True)
    stages = serializers.ListField(child=serializers.DictField())


def _projection(value: RouteCompletion | None) -> dict[str, Any]:
    if value is None:
        return {
            "status": "pending",
            "total_length_meters": "0.000",
            "covered_length_meters": "0.000",
            "completion_percent": "0.000",
            "calculated_at": None,
            "error": "",
        }
    return {
        "status": value.status,
        "total_length_meters": value.total_length_meters,
        "covered_length_meters": value.covered_length_meters,
        "completion_percent": value.completion_percent,
        "calculated_at": value.calculated_at,
        "error": value.error,
    }


@extend_schema(
    responses={
        200: ReferenceCompletionSerializer,
        401: OpenApiResponse(description="Authentication required."),
        404: OpenApiResponse(description="Reference route not found."),
    },
    tags=["game-reference-completion"],
)
class ReferenceRouteCompletionView(GameEndpoint):
    def get(self, request: Any, route_id: UUID) -> Response:
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        route = (
            ReferenceRoute.objects.filter(
                pk=route_id,
                active=True,
                current_version__isnull=False,
                current_version__active=True,
            )
            .select_related("current_version")
            .prefetch_related("stages__current_version")
            .first()
        )
        if route is None or route.current_version is None:
            return _private(Response({"detail": "Reference route not found."}, status=404))
        competition = player.active_competition
        player_result = RouteCompletion.objects.filter(
            route_version=route.current_version, player=player
        ).first()
        competition_result = (
            RouteCompletion.objects.filter(
                route_version=route.current_version, competition=competition
            ).first()
            if competition is not None
            else None
        )
        stages = []
        for stage in route.stages.filter(active=True, current_version__isnull=False).order_by(
            "route_number", "pk"
        ):
            version = stage.current_version
            if version is None:
                continue
            stages.append(
                {
                    "route_id": stage.pk,
                    "version": version.version_number,
                    "player": _projection(
                        RouteCompletion.objects.filter(route_version=version, player=player).first()
                    ),
                    "competition": _projection(
                        RouteCompletion.objects.filter(
                            route_version=version, competition=competition
                        ).first()
                        if competition is not None
                        else None
                    ),
                }
            )
        return _private(
            Response(
                {
                    "route_id": route.pk,
                    "version": route.current_version.version_number,
                    "player": _projection(player_result),
                    "competition": _projection(competition_result),
                    "stages": stages,
                }
            )
        )
