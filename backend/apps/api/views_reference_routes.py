"""Bounded, private completion-reference read endpoints."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.conf import settings
from django.db.models import Prefetch, QuerySet
from django.utils.module_loading import import_string
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.generics import ListAPIView
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.reference_routes.models import (
    ReferenceRoute,
    ReferenceRouteVersion,
    ReferenceSourceKind,
    ReferenceValidationStatus,
    has_deployable_derivative_offer,
)

from .serializers_reference_routes import ReferenceRouteListSerializer, ReferenceRouteSerializer


def reference_route_authorizer() -> Any:
    """Resolve the authoritative competition-membership checker per request."""
    return import_string(settings.REFERENCE_ROUTE_AUTHORIZER)


class GameReferencePermission(BasePermission):
    """Require the future game session contract, not any arbitrary Django user."""

    def has_permission(self, request: Request, view: object) -> bool:
        if not getattr(settings, "GAME_ENABLED", False) or not request.user.is_authenticated:
            return False
        claims = request.session.get("game_session")
        if not bool(
            isinstance(claims, dict)
            and claims.get("competition_id")
            and claims.get("reference_route_read") is True
        ):
            return False
        return bool(
            reference_route_authorizer()(request.user, str(claims["competition_id"]), request)
        )


class ReferenceRoutePagination(CursorPagination):
    page_size = 50
    max_page_size = 100
    page_size_query_param = "page_size"
    cursor_query_param = "cursor"
    ordering = ("route_number", "id")


def reference_queryset() -> QuerySet[ReferenceRoute]:
    offer_base = str(getattr(settings, "REFERENCE_ROUTE_DERIVATIVE_OFFER_URL", "") or "").rstrip(
        "/"
    )
    if not offer_base:
        return ReferenceRoute.objects.none()
    active_stage_versions = ReferenceRouteVersion.objects.filter(
        active=True,
        validation_status=ReferenceValidationStatus.VALID,
    ).select_related("route__collection", "source_import", "source_import__alteration_offer")
    active_stage_version_ids = [
        version.pk
        for version in active_stage_versions
        if has_deployable_derivative_offer(version.route.collection, version.source_import)
    ]
    active_stage_versions = active_stage_versions.filter(pk__in=active_stage_version_ids)
    active_stages = ReferenceRoute.objects.filter(
        active=True,
        publication_status="approved",
        current_version__isnull=False,
        current_version__validation_status=ReferenceValidationStatus.VALID,
    ).select_related(
        "collection",
        "current_version",
        "current_version__source_import",
        "current_version__source_import__alteration_offer",
    )
    active_stage_ids = [
        route.pk
        for route in active_stages
        if route.current_version
        and has_deployable_derivative_offer(route.collection, route.current_version.source_import)
    ]
    active_stages = active_stages.filter(pk__in=active_stage_ids).prefetch_related(
        Prefetch("current_version", queryset=active_stage_versions)
    )
    candidates = ReferenceRoute.objects.filter(
        active=True,
        publication_status="approved",
        current_version__isnull=False,
        current_version__validation_status=ReferenceValidationStatus.VALID,
        collection__active=True,
        collection__permission_granted=True,
        collection__source_kind=ReferenceSourceKind.OSM_NUMBERED,
        parent__isnull=True,
    ).select_related(
        "collection",
        "current_version",
        "current_version__source_import",
        "current_version__source_import__alteration_offer",
    )
    candidate_ids = [
        route.pk
        for route in candidates
        if route.current_version
        and has_deployable_derivative_offer(route.collection, route.current_version.source_import)
    ]
    return candidates.filter(pk__in=candidate_ids).prefetch_related(
        Prefetch("stages", queryset=active_stages)
    )


def _private_headers(response: Response) -> Response:
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


REFERENCE_PARAMETERS = [
    OpenApiParameter("source", OpenApiTypes.STR, OpenApiParameter.QUERY),
    OpenApiParameter("route_number", OpenApiTypes.STR, OpenApiParameter.QUERY),
    OpenApiParameter("cursor", OpenApiTypes.STR, OpenApiParameter.QUERY),
    OpenApiParameter("page_size", OpenApiTypes.INT, OpenApiParameter.QUERY),
]


@extend_schema(parameters=REFERENCE_PARAMETERS)
class ReferenceRouteListView(ListAPIView):
    permission_classes = [GameReferencePermission]
    serializer_class = ReferenceRouteListSerializer
    pagination_class = ReferenceRoutePagination

    def get_queryset(self) -> QuerySet[ReferenceRoute]:
        queryset = reference_queryset()
        number = self.request.query_params.get("route_number", "").strip()
        if number:
            queryset = queryset.filter(route_number=number)
        source = self.request.query_params.get("source", "").strip()
        if source:
            queryset = queryset.filter(collection__source_kind=source)
        return queryset.order_by("route_number", "id")

    def finalize_response(
        self, request: Request, response: Response, *args: Any, **kwargs: Any
    ) -> Response:
        return _private_headers(super().finalize_response(request, response, *args, **kwargs))


class ReferenceRouteDetailView(APIView):
    permission_classes = [GameReferencePermission]
    serializer_class = ReferenceRouteSerializer

    def finalize_response(
        self, request: Request, response: Response, *args: Any, **kwargs: Any
    ) -> Response:
        return _private_headers(super().finalize_response(request, response, *args, **kwargs))

    def get(self, request: Request, route_id: UUID) -> Response:
        route = reference_queryset().filter(pk=route_id).first()
        if route is None:
            raise NotFound
        return Response(ReferenceRouteSerializer(route, context={"request": request}).data)
