"""Private completion projections for reference routes."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from apps.accounts.competition_services import sharing_is_active
from apps.accounts.game_api import GameEndpoint, _private
from apps.accounts.models import CompetitionMembership
from apps.accounts.services import competition_is_available
from apps.api.reference_authorization import active_competition_for_player
from apps.reference_routes.completion_services import PUBLIC_COMPLETION_ERROR_CODE
from apps.reference_routes.models import (
    ReferencePublicationStatus,
    ReferenceRoute,
    ReferenceValidationStatus,
    RouteCompletion,
    has_publishable_reference_source,
)


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
        "error": PUBLIC_COMPLETION_ERROR_CODE if value.status == "failed" else "",
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
        if not competition_is_available():
            return _private(Response({"detail": "The private game is unavailable."}, status=404))
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
        competition = active_competition_for_player(player)
        if competition is None:
            return _private(Response({"detail": "Reference route not found."}, status=404))
        membership = CompetitionMembership.objects.filter(
            competition=competition, player=player
        ).first()
        if membership is None:
            return _private(Response({"detail": "Reference route not found."}, status=404))
        sharing_active = sharing_is_active(membership)
        route = (
            ReferenceRoute.objects.filter(
                pk=route_id,
                active=True,
                publication_status=ReferencePublicationStatus.APPROVED,
                current_version__isnull=False,
                current_version__active=True,
                current_version__validation_status=ReferenceValidationStatus.VALID,
                collection__active=True,
                collection__permission_granted=True,
            )
            .select_related("collection", "current_version", "current_version__source_import")
            .first()
        )
        if (
            route is None
            or route.current_version is None
            or not has_publishable_reference_source(
                route.collection, route.current_version.source_import
            )
        ):
            return _private(Response({"detail": "Reference route not found."}, status=404))
        player_result = RouteCompletion.objects.filter(
            route_version=route.current_version, player=player
        ).first()
        competition_result = (
            RouteCompletion.objects.filter(
                route_version=route.current_version, competition=competition
            ).first()
            if sharing_active
            else None
        )
        stages = []
        stages_query = route.stages.filter(
            active=True,
            publication_status=ReferencePublicationStatus.APPROVED,
            current_version__isnull=False,
            current_version__active=True,
            current_version__validation_status=ReferenceValidationStatus.VALID,
            collection__active=True,
            collection__permission_granted=True,
        ).select_related("collection", "current_version", "current_version__source_import")
        for stage in stages_query.order_by("route_number", "pk") if sharing_active else ():
            version = stage.current_version
            if version is None or not has_publishable_reference_source(
                stage.collection, version.source_import
            ):
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
