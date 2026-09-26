"""Private completion projections for reference routes."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any
from uuid import UUID

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.response import Response

from apps.accounts.competition_services import sharing_is_active
from apps.accounts.game_api import GameEndpoint, _private
from apps.accounts.models import CompetitionMembership, StravaSyncState
from apps.accounts.services import competition_is_available, game_is_available
from apps.reference_routes.models import (
    ReferencePublicationStatus,
    ReferenceRoute,
    ReferenceValidationStatus,
    RouteCompletion,
    RouteCompletionMonthly,
    has_publishable_reference_source,
)

from .serializers_reference_routes import ReferenceAttributionSerializer, geometry_json
from .views_reference_routes import reference_competition_id

PARTIAL_SYNC_STATUSES = ("queued", "running", "paused", "failed")
PUBLIC_COMPLETION_ERROR = "completion_unavailable"


class CompletionMonthlySerializer(serializers.Serializer[dict[str, Any]]):
    month = serializers.DateField()
    covered_length_meters = serializers.DecimalField(max_digits=14, decimal_places=3)
    gain_length_meters = serializers.DecimalField(max_digits=14, decimal_places=3)


class CompletionProjectionSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.CharField()
    total_length_meters = serializers.DecimalField(max_digits=14, decimal_places=3)
    covered_length_meters = serializers.DecimalField(max_digits=14, decimal_places=3)
    completion_percent = serializers.DecimalField(max_digits=7, decimal_places=3)
    calculated_at = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField()
    covered_geometry = serializers.JSONField(allow_null=True)
    monthly = CompletionMonthlySerializer(many=True)
    partial = serializers.BooleanField()
    sync_status = serializers.CharField()


class ReferenceCompletionSerializer(serializers.Serializer[dict[str, Any]]):
    route_id = serializers.UUIDField()
    version = serializers.IntegerField()
    player = CompletionProjectionSerializer(allow_null=True)
    competition = CompletionProjectionSerializer(allow_null=True)
    competition_access = serializers.ChoiceField(
        choices=("available", "consent_required", "competition_disabled")
    )
    stages = serializers.ListField(child=serializers.DictField())
    title = serializers.CharField()
    route_number = serializers.CharField(allow_blank=True)
    source_kind = serializers.CharField()
    geometry = serializers.JSONField(allow_null=True)
    attribution = serializers.DictField()


def _projection(
    value: RouteCompletion | None,
    *,
    version: Any,
    player: Any,
    competition: Any,
    partial: bool,
    sync_status: str,
) -> dict[str, Any]:
    if value is None:
        return {
            "status": "pending",
            "total_length_meters": "0.000",
            "covered_length_meters": "0.000",
            "completion_percent": "0.000",
            "calculated_at": None,
            "error": "",
            "covered_geometry": None,
            "monthly": [],
            "partial": partial,
            "sync_status": sync_status,
        }
    status = value.status
    if status == "fresh" and value.route_checksum != version.checksum:
        status = "stale"
    if (
        status == "fresh"
        and competition is not None
        and value.competition_id is not None
        and value.membership_revision != competition.revision
    ):
        status = "stale"
    monthly_query = RouteCompletionMonthly.objects.filter(
        route_version=version,
        subject_type=value.subject_type,
    )
    if value.player_id:
        monthly_query = monthly_query.filter(player=player)
    elif value.competition_id:
        monthly_query = monthly_query.filter(competition=competition)
    is_fresh = status == "fresh"
    covered_geometry = geometry_json(value.covered_geometry) if is_fresh else None
    return {
        "status": status,
        "total_length_meters": value.total_length_meters,
        "covered_length_meters": value.covered_length_meters if is_fresh else "0.000",
        "completion_percent": value.completion_percent if is_fresh else "0.000",
        "calculated_at": value.calculated_at,
        "error": PUBLIC_COMPLETION_ERROR if status == "failed" else "",
        "covered_geometry": covered_geometry,
        "monthly": [
            {
                "month": item.month,
                "covered_length_meters": item.covered_length_meters,
                # Keep the original field for backwards compatibility while
                # naming the projection's semantic value explicitly for clients.
                "gain_length_meters": item.covered_length_meters,
            }
            for item in monthly_query.order_by("month")
        ]
        if is_fresh
        else [],
        "partial": partial,
        "sync_status": sync_status,
    }


@extend_schema(
    parameters=[
        OpenApiParameter("competition_id", OpenApiTypes.UUID, OpenApiParameter.QUERY),
    ],
    responses={
        200: ReferenceCompletionSerializer,
        401: OpenApiResponse(description="Authentication required."),
        404: OpenApiResponse(description="Reference route not found."),
    },
    tags=["game-reference-completion"],
)
class ReferenceRouteCompletionView(GameEndpoint):
    def get(self, request: Any, route_id: UUID) -> Response:
        if not game_is_available():
            return _private(Response({"detail": "The private game is unavailable."}, status=404))
        player = self.player_or_401(request)
        if isinstance(player, Response):
            return player
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
        competition_id = reference_competition_id(request, player)
        membership = (
            CompetitionMembership.objects.select_related("competition")
            .filter(
                competition_id=competition_id,
                player=player,
                competition__is_active=True,
            )
            .first()
            if competition_id is not None
            else None
        )
        competition = membership.competition if membership is not None else None
        if competition is None:
            return _private(Response({"detail": "Reference route not found."}, status=404))
        competition_allowed = bool(
            competition_is_available() and membership is not None and sharing_is_active(membership)
        )
        visible_competition = competition if competition_allowed else None
        competition_access = (
            "available"
            if competition_allowed
            else "competition_disabled"
            if not competition_is_available()
            else "consent_required"
        )
        player_sync_status = (
            StravaSyncState.objects.filter(player=player).values_list("status", flat=True).first()
            or ""
        )
        player_partial = player_sync_status in PARTIAL_SYNC_STATUSES
        competition_statuses = (
            list(
                CompetitionMembership.objects.filter(
                    competition=visible_competition,
                    sharing_consent_at__isnull=False,
                )
                .exclude(sharing_scope=CompetitionMembership.SharingScope.NONE)
                .values_list("player__strava_sync_state__status", flat=True)
            )
            if visible_competition is not None
            else []
        )
        competition_sync_status = next(
            (
                candidate
                for candidate in ("failed", "paused", "running", "queued")
                if candidate in competition_statuses
            ),
            "",
        )
        competition_partial = competition_sync_status in PARTIAL_SYNC_STATUSES
        player_result = RouteCompletion.objects.filter(
            route_version=route.current_version, player=player
        ).first()
        competition_result = (
            RouteCompletion.objects.filter(
                route_version=route.current_version, competition=visible_competition
            ).first()
            if visible_competition is not None
            else None
        )
        attribution = dict(ReferenceAttributionSerializer(route.collection).data)
        if not attribution.get("attribution_text"):
            attribution["attribution_text"] = (
                route.current_version.attribution_metadata.get("attribution_text")
                or route.current_version.attribution
            )
        route_payload = {
            "title": route.title,
            "route_number": route.route_number,
            "source_kind": route.collection.source_kind,
            "geometry": geometry_json(route.current_version.normalized_geometry),
            "attribution": attribution,
        }
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
        for stage in stages_query.order_by("route_number", "pk"):
            version = stage.current_version
            if version is None or not has_publishable_reference_source(
                stage.collection, version.source_import
            ):
                continue
            stages.append(
                {
                    "route_id": stage.pk,
                    "version": version.version_number,
                    "title": stage.title,
                    "route_number": stage.route_number,
                    "geometry": geometry_json(version.normalized_geometry),
                    "player": _projection(
                        RouteCompletion.objects.filter(
                            route_version=version, player=player
                        ).first(),
                        version=version,
                        player=player,
                        competition=visible_competition,
                        partial=player_partial,
                        sync_status=player_sync_status,
                    ),
                    "competition": _projection(
                        RouteCompletion.objects.filter(
                            route_version=version, competition=visible_competition
                        ).first()
                        if visible_competition is not None
                        else None,
                        version=version,
                        player=player,
                        competition=visible_competition,
                        partial=competition_partial,
                        sync_status=competition_sync_status,
                    ),
                }
            )
        return _private(
            Response(
                {
                    "route_id": route.pk,
                    "version": route.current_version.version_number,
                    **route_payload,
                    "player": _projection(
                        player_result,
                        version=route.current_version,
                        player=player,
                        competition=visible_competition,
                        partial=player_partial,
                        sync_status=player_sync_status,
                    ),
                    "competition": _projection(
                        competition_result,
                        version=route.current_version,
                        player=player,
                        competition=visible_competition,
                        partial=competition_partial,
                        sync_status=competition_sync_status,
                    ),
                    "competition_access": competition_access,
                    "stages": stages,
                }
            )
        )
