"""Read-only HTTP views for the public route catalogue."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import Exists, F, OuterRef, Prefetch, Q, QuerySet
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.exceptions import ParseError
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalogue.models import (
    ForumAuthor,
    ForumThread,
    Route,
    RouteCategory,
    RouteLifecycle,
    RouteSource,
    RouteSourceMerge,
    SimilarityRelationship,
)
from apps.catalogue.spatial import SpatialQueryLimits, query_selected_route, query_viewport

from .pagination import RoutePagination
from .serializers import (
    RouteSerializer,
    SpatialRouteSerializer,
    ViewportResponseSerializer,
)

ROUTE_FILTER_PARAMETERS = [
    OpenApiParameter(
        "search", OpenApiTypes.STR, description="Full-text route, place, or author search."
    ),
    OpenApiParameter("author", OpenApiTypes.STR),
    OpenApiParameter("category", OpenApiTypes.STR, description="Category slug."),
    OpenApiParameter("loop_status", OpenApiTypes.STR),
    OpenApiParameter("source_status", OpenApiTypes.STR),
    OpenApiParameter("min_distance_m", OpenApiTypes.NUMBER),
    OpenApiParameter("max_distance_m", OpenApiTypes.NUMBER),
    OpenApiParameter("min_ascent_m", OpenApiTypes.NUMBER),
    OpenApiParameter("max_ascent_m", OpenApiTypes.NUMBER),
    OpenApiParameter("page", OpenApiTypes.INT),
    OpenApiParameter("page_size", OpenApiTypes.INT),
]

VIEWPORT_PARAMETERS = [
    OpenApiParameter(name, OpenApiTypes.NUMBER, required=True)
    for name in ("west", "south", "east", "north")
] + [
    OpenApiParameter("zoom", OpenApiTypes.INT, required=True),
    OpenApiParameter("limit", OpenApiTypes.INT),
    OpenApiParameter("cell_limit", OpenApiTypes.INT),
]


def _source_queryset() -> QuerySet[RouteSource]:
    return RouteSource.objects.prefetch_related("posts__thread", "posts__author").order_by("pk")


def public_route_queryset() -> QuerySet[Route]:
    """Return only published routes with a technically approved version.

    All relationships rendered by the public serializers are prefetched here,
    keeping list responses bounded in query count as the catalogue grows.
    """

    public_variant = SimilarityRelationship.objects.filter(
        relationship_type=SimilarityRelationship.RelationshipType.VARIANT,
        route_a__lifecycle=RouteLifecycle.PUBLISHED,
        route_a__current_approved_version__isnull=False,
        route_b__lifecycle=RouteLifecycle.PUBLISHED,
        route_b__current_approved_version__isnull=False,
    ).select_related("route_a", "route_b")
    return (
        Route.objects.filter(
            lifecycle=RouteLifecycle.PUBLISHED,
            current_approved_version__isnull=False,
        )
        .select_related("current_approved_version__source")
        .prefetch_related(
            Prefetch(
                "category_links",
                queryset=RouteCategory.objects.select_related("category").order_by(
                    "category__name"
                ),
                to_attr="public_category_links",
            ),
            Prefetch(
                "sources",
                queryset=_source_queryset(),
                to_attr="public_sources",
            ),
            Prefetch(
                "merged_source_links",
                queryset=RouteSourceMerge.objects.filter(active=True)
                .select_related("source")
                .prefetch_related("source__posts__thread", "source__posts__author"),
                to_attr="public_source_merges",
            ),
            Prefetch(
                "similarity_a",
                queryset=public_variant,
                to_attr="public_variant_relationships_a",
            ),
            Prefetch(
                "similarity_b",
                queryset=public_variant,
                to_attr="public_variant_relationships_b",
            ),
        )
        .distinct()
    )


def _decimal(value: str, name: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ParseError(f"{name} must be a decimal number") from None
    if not result.is_finite():
        raise ParseError(f"{name} must be a finite decimal number")
    return result


def _int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise ParseError(f"{name} must be an integer") from None


def filter_routes(queryset: QuerySet[Route], params: Any) -> QuerySet[Route]:
    """Apply the documented deterministic route filters."""

    search = params.get("search", "").strip()
    if search:
        if connection.vendor == "postgresql":
            vector = (
                SearchVector("display_title", config="simple")
                + SearchVector("generated_title", config="simple")
                + SearchVector("thread_title", config="simple")
            )
            query = SearchQuery(search, search_type="websearch", config="simple")
            source_threads = ForumThread.objects.filter(
                Q(posts__source_links__source__route=OuterRef("pk"))
                | Q(
                    posts__source_links__source__canonical_route_links__canonical_route=OuterRef(
                        "pk"
                    ),
                    posts__source_links__source__canonical_route_links__active=True,
                )
            )
            source_authors = ForumAuthor.objects.filter(
                Q(posts__source_links__source__route=OuterRef("pk"))
                | Q(
                    posts__source_links__source__canonical_route_links__canonical_route=OuterRef(
                        "pk"
                    ),
                    posts__source_links__source__canonical_route_links__active=True,
                )
            )
            thread_fts = source_threads.annotate(
                _text=SearchVector("title", config="simple")
                + SearchVector("locality", config="simple")
            ).filter(_text=query)
            author_fts = source_authors.annotate(
                _text=SearchVector("username", config="simple")
            ).filter(_text=query)
            # ``__trigram_similar`` emits the `%` operator, allowing the GIN
            # trigram indexes to serve typo-tolerant searches.
            thread_trigram = source_threads.filter(
                Q(title__trigram_similar=search) | Q(locality__trigram_similar=search)
            )
            author_trigram = source_authors.filter(username__trigram_similar=search)
            queryset = queryset.annotate(
                _search_vector=vector,
                _search_rank=SearchRank(F("_search_vector"), query),
            ).filter(
                Q(_search_vector=query)
                | Exists(thread_fts)
                | Exists(author_fts)
                | Q(display_title__trigram_similar=search)
                | Q(generated_title__trigram_similar=search)
                | Exists(thread_trigram)
                | Exists(author_trigram)
            )
        else:
            queryset = queryset.filter(
                Q(display_title__icontains=search)
                | Q(generated_title__icontains=search)
                | Q(thread_title__icontains=search)
                | Q(sources__posts__thread__title__icontains=search)
                | Q(sources__posts__thread__locality__icontains=search)
                | Q(sources__posts__author__username__icontains=search)
                | Q(
                    merged_source_links__active=True,
                    merged_source_links__source__posts__thread__title__icontains=search,
                )
                | Q(
                    merged_source_links__active=True,
                    merged_source_links__source__posts__thread__locality__icontains=search,
                )
                | Q(
                    merged_source_links__active=True,
                    merged_source_links__source__posts__author__username__icontains=search,
                )
            )

    author = params.get("author", "").strip()
    if author:
        queryset = queryset.filter(
            Q(sources__posts__author__username__iexact=author)
            | Q(
                merged_source_links__active=True,
                merged_source_links__source__posts__author__username__iexact=author,
            )
        )
    category = params.get("category", "").strip()
    if category:
        queryset = queryset.filter(category_links__category__slug=category)

    numeric_filters = {
        "min_distance": ("current_approved_version__distance_m__gte", "distance"),
        "max_distance": ("current_approved_version__distance_m__lte", "distance"),
        "min_distance_m": ("current_approved_version__distance_m__gte", "min_distance_m"),
        "max_distance_m": ("current_approved_version__distance_m__lte", "max_distance_m"),
        "min_elevation": ("current_approved_version__ascent_m__gte", "elevation"),
        "max_elevation": ("current_approved_version__ascent_m__lte", "elevation"),
        "min_ascent_m": ("current_approved_version__ascent_m__gte", "min_ascent_m"),
        "max_ascent_m": ("current_approved_version__ascent_m__lte", "max_ascent_m"),
        # Explicit aliases make the contract pleasant for clients that use
        # the model field names rather than the shorter filter names.
        "distance_min": ("current_approved_version__distance_m__gte", "distance_min"),
        "distance_max": ("current_approved_version__distance_m__lte", "distance_max"),
        "elevation_min": ("current_approved_version__ascent_m__gte", "elevation_min"),
        "elevation_max": ("current_approved_version__ascent_m__lte", "elevation_max"),
    }
    for parameter, (lookup, label) in numeric_filters.items():
        value = params.get(parameter)
        if value not in (None, ""):
            queryset = queryset.filter(**{lookup: _decimal(value, label)})

    loop_status = params.get("loop_status", "").strip()
    if loop_status:
        queryset = queryset.filter(current_approved_version__loop_status=loop_status)
    source_status = params.get("source_status", "").strip()
    if source_status:
        queryset = queryset.filter(current_approved_version__source__source_status=source_status)
    return queryset.distinct()


@extend_schema(parameters=ROUTE_FILTER_PARAMETERS)
class RouteListView(ListAPIView):
    serializer_class = RouteSerializer
    pagination_class = RoutePagination

    def get_queryset(self) -> QuerySet[Route]:
        queryset = filter_routes(public_route_queryset(), self.request.query_params)
        if (
            self.request.query_params.get("search", "").strip()
            and connection.vendor == "postgresql"
        ):
            return queryset.order_by("-_search_rank", "-updated_at", "id")
        return queryset.order_by("-updated_at", "id")

    def get_serializer_context(self) -> dict[str, Any]:
        return {**super().get_serializer_context(), "include_geometry": False}


@extend_schema(responses=RouteSerializer)
class RouteDetailView(APIView):
    """Resolve by stable UUID; a slug is only a readable alternate lookup."""

    def get(
        self, request: Request, route_id: UUID | None = None, slug: str | None = None
    ) -> Response:
        del request
        queryset = public_route_queryset()
        if route_id is not None:
            route = get_object_or_404(queryset, pk=route_id)
        else:
            route = get_object_or_404(queryset, slug=slug)
        return Response(RouteSerializer(route, context={"include_geometry": True}).data)


@extend_schema(responses=SpatialRouteSerializer)
class SelectedRouteView(APIView):
    def get(self, request: Request, route_id: UUID) -> Response:
        del request
        try:
            result = query_selected_route(route_id)
        except ValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(result)


@extend_schema(parameters=VIEWPORT_PARAMETERS, responses=ViewportResponseSerializer)
class ViewportRouteView(APIView):
    """Return bounded heatmap cells or simplified route lines for a viewport."""

    def get(self, request: Request) -> Response:
        values: dict[str, float | int] = {}
        for name in ("west", "south", "east", "north"):
            raw = request.query_params.get(name)
            if raw is None:
                raise ParseError(f"{name} is required")
            values[name] = float(_decimal(raw, name))
        raw_zoom = request.query_params.get("zoom")
        if raw_zoom is None:
            raise ParseError("zoom is required")
        zoom = _int(raw_zoom, "zoom")
        max_routes = _int(request.query_params.get("limit", "500"), "limit")
        max_cells = _int(request.query_params.get("cell_limit", "10000"), "cell_limit")
        try:
            result = query_viewport(
                west=values["west"],
                south=values["south"],
                east=values["east"],
                north=values["north"],
                zoom=zoom,
                limits=SpatialQueryLimits(max_routes=max_routes, max_cells=max_cells),
            )
        except ValidationError as exc:
            raise ParseError(str(exc)) from exc
        return Response(result)
