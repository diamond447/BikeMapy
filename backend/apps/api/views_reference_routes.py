"""Bounded authenticated completion-reference read endpoints."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db.models import QuerySet
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.reference_routes.models import ReferenceRoute

from .serializers_reference_routes import ReferenceRouteSerializer


def reference_queryset() -> QuerySet[ReferenceRoute]:
    return (
        ReferenceRoute.objects.filter(
            active=True, current_version__isnull=False, collection__active=True
        )
        .select_related("collection", "current_version")
        .prefetch_related("stages__current_version")
    )


class ReferenceRouteListView(ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ReferenceRouteSerializer

    def get_queryset(self) -> QuerySet[ReferenceRoute]:
        queryset = reference_queryset().filter(parent__isnull=True)
        number = self.request.query_params.get("route_number", "").strip()
        if number:
            queryset = queryset.filter(route_number=number)
        source = self.request.query_params.get("source", "").strip()
        if source:
            queryset = queryset.filter(collection__source_kind=source)
        return queryset.order_by("route_number", "id")[:500]

    def get_serializer_context(self) -> dict[str, Any]:
        return {**super().get_serializer_context(), "include_geometry": False}


class ReferenceRouteDetailView(APIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ReferenceRouteSerializer

    def get(self, request: Request, route_id: UUID) -> Response:
        route = reference_queryset().filter(pk=route_id).first()
        if route is None:
            from rest_framework.exceptions import NotFound

            raise NotFound
        return Response(
            ReferenceRouteSerializer(
                route, context={"request": request, "include_geometry": True}
            ).data
        )
