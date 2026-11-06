"""Read-only HTTP views for the public route catalogue."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from uuid import UUID

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import connection
from django.db.models import Exists, F, OuterRef, Prefetch, Q, QuerySet
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import ParseError
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalogue.models import (
    ForumAuthor,
    ForumPost,
    ForumThread,
    ModerationDecision,
    Route,
    RouteCategory,
    RouteLifecycle,
    RouteSource,
    RouteSourceMerge,
    SimilarityRelationship,
)
from apps.catalogue.spatial import (
    SpatialQueryLimits,
    normalize_filter_inputs,
    query_selected_route,
    query_viewport,
)

from .pagination import RoutePagination
from .serializers import (
    RouteSerializer,
    RouteSummarySerializer,
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
    OpenApiParameter("west", OpenApiTypes.NUMBER),
    OpenApiParameter("south", OpenApiTypes.NUMBER),
    OpenApiParameter("east", OpenApiTypes.NUMBER),
    OpenApiParameter("north", OpenApiTypes.NUMBER),
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
    OpenApiParameter("search", OpenApiTypes.STR),
    OpenApiParameter("author", OpenApiTypes.STR),
    OpenApiParameter("category", OpenApiTypes.STR, description="Category slug."),
    OpenApiParameter("min_distance_m", OpenApiTypes.NUMBER),
    OpenApiParameter("max_distance_m", OpenApiTypes.NUMBER),
    OpenApiParameter("min_ascent_m", OpenApiTypes.NUMBER),
    OpenApiParameter("max_ascent_m", OpenApiTypes.NUMBER),
]


def _source_queryset() -> QuerySet[RouteSource]:
    return RouteSource.objects.prefetch_related("posts__thread", "posts__author").order_by("pk")


# The widest columns of a route row, which no list renders.
_HEAVY_VERSION_FIELDS = (
    "current_approved_version__normalized_geometry",
    "current_approved_version__simplified_geometry",
    "current_approved_version__elevation_profile",
)


def public_route_queryset(*, detail: bool = False, geometry: bool = False) -> QuerySet[Route]:
    """Return published routes with an approved version, prefetched for the API.

    ``detail`` adds variants and the elevation profile; geometries are loaded
    only with ``geometry``.
    """

    queryset = (
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
                "moderation_decisions",
                queryset=ModerationDecision.objects.filter(
                    action=ModerationDecision.Action.REVIEW
                ).order_by("-created_at", "-pk"),
                to_attr="public_review_decisions",
            ),
        )
    )
    if not detail:
        return queryset.defer(*_HEAVY_VERSION_FIELDS)
    if not geometry:
        queryset = queryset.defer(*_HEAVY_VERSION_FIELDS[:2])
    public_variant = SimilarityRelationship.objects.filter(
        relationship_type=SimilarityRelationship.RelationshipType.VARIANT,
        route_a__lifecycle=RouteLifecycle.PUBLISHED,
        route_a__current_approved_version__isnull=False,
        route_b__lifecycle=RouteLifecycle.PUBLISHED,
        route_b__current_approved_version__isnull=False,
    ).select_related("route_a", "route_b")
    return queryset.prefetch_related(
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


def _source_exists(source_filter: Q) -> Exists | Q:
    """Match routes whose direct or actively merged sources satisfy a filter.

    ``EXISTS`` instead of a join, which would repeat route rows.
    """

    direct = RouteSource.objects.filter(source_filter, route=OuterRef("pk"))
    merged = RouteSource.objects.filter(
        source_filter,
        canonical_route_links__canonical_route=OuterRef("pk"),
        canonical_route_links__active=True,
    )
    return Exists(direct) | Exists(merged)


def _routes_with_posts(post_filter: Q) -> QuerySet[RouteSource, Any]:
    """Route ids whose direct sources cite a post matching ``post_filter``."""

    return RouteSource.objects.filter(
        post_links__post__in=ForumPost.objects.filter(post_filter)
    ).values("route_id")


def _merged_routes_with_posts(post_filter: Q) -> QuerySet[RouteSourceMerge, Any]:
    """Canonical route ids whose actively merged sources cite a matching post."""

    return RouteSourceMerge.objects.filter(
        active=True,
        source__post_links__post__in=ForumPost.objects.filter(post_filter),
    ).values("canonical_route_id")


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
            # Match threads and authors first, then routes through ``IN``
            # subqueries: a correlated EXISTS with OR could not use the indexes.
            matching_threads = ForumThread.objects.annotate(
                _text=SearchVector("title", config="simple")
                + SearchVector("locality", config="simple")
            ).filter(
                Q(_text=query)
                | Q(title__trigram_similar=search)
                | Q(locality__trigram_similar=search)
            )
            matching_authors = ForumAuthor.objects.annotate(
                _text=SearchVector("username", config="simple")
            ).filter(Q(_text=query) | Q(username__trigram_similar=search))
            queryset = queryset.annotate(
                _search_vector=vector,
                _search_rank=SearchRank(F("_search_vector"), query),
            ).filter(
                Q(_search_vector=query)
                | Q(display_title__trigram_similar=search)
                | Q(generated_title__trigram_similar=search)
                | Q(pk__in=_routes_with_posts(Q(thread__in=matching_threads)))
                | Q(pk__in=_routes_with_posts(Q(author__in=matching_authors)))
                | Q(pk__in=_merged_routes_with_posts(Q(thread__in=matching_threads)))
                | Q(pk__in=_merged_routes_with_posts(Q(author__in=matching_authors)))
            )
        else:
            post_text = (
                Q(posts__thread__title__icontains=search)
                | Q(posts__thread__locality__icontains=search)
                | Q(posts__author__username__icontains=search)
            )
            queryset = queryset.filter(
                Q(display_title__icontains=search)
                | Q(generated_title__icontains=search)
                | Q(thread_title__icontains=search)
                | _source_exists(post_text)
            )

    author = params.get("author", "").strip()
    if author:
        queryset = queryset.filter(_source_exists(Q(posts__author__username__iexact=author)))
    category = params.get("category", "").strip()
    if category:
        queryset = queryset.filter(
            Exists(RouteCategory.objects.filter(route=OuterRef("pk"), category__slug=category))
        )

    numeric_filters = {
        "min_distance": ("current_approved_version__distance_m__gte", "distance"),
        "max_distance": ("current_approved_version__distance_m__lte", "distance"),
        "min_distance_m": ("current_approved_version__distance_m__gte", "min_distance_m"),
        "max_distance_m": ("current_approved_version__distance_m__lte", "max_distance_m"),
        "min_elevation": ("current_approved_version__ascent_m__gte", "elevation"),
        "max_elevation": ("current_approved_version__ascent_m__lte", "elevation"),
        "min_ascent_m": ("current_approved_version__ascent_m__gte", "min_ascent_m"),
        "max_ascent_m": ("current_approved_version__ascent_m__lte", "max_ascent_m"),
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
    bbox_values = {name: params.get(name) for name in ("west", "south", "east", "north")}
    if (
        all(value not in (None, "") for value in bbox_values.values())
        and connection.vendor == "postgresql"
    ):
        west = float(_decimal(bbox_values["west"], "west"))
        south = float(_decimal(bbox_values["south"], "south"))
        east = float(_decimal(bbox_values["east"], "east"))
        north = float(_decimal(bbox_values["north"], "north"))
        from django.contrib.gis.geos import Polygon

        viewport = Polygon.from_bbox((west, south, east, north))
        queryset = queryset.filter(
            Q(current_approved_version__normalized_geometry__intersects=viewport)
            | Q(
                current_approved_version__normalized_geometry__isnull=True,
                current_approved_version__simplified_geometry__intersects=viewport,
            )
        )
    return queryset


@extend_schema(parameters=ROUTE_FILTER_PARAMETERS)
class RouteListView(ListAPIView):
    serializer_class = RouteSummarySerializer
    pagination_class = RoutePagination

    def get_queryset(self) -> QuerySet[Route]:
        queryset = filter_routes(public_route_queryset(), self.request.query_params)
        if (
            self.request.query_params.get("search", "").strip()
            and connection.vendor == "postgresql"
        ):
            return queryset.order_by("-_search_rank", "-updated_at", "id")
        return queryset.order_by("-updated_at", "id")


@extend_schema(
    parameters=[
        OpenApiParameter(
            "include",
            OpenApiTypes.STR,
            enum=["geometry"],
            description=(
                "Set to `geometry` to embed the full normalized geometry. It is null "
                "otherwise; map clients read it from the geometry endpoint."
            ),
        )
    ],
    responses=RouteSerializer,
)
class RouteDetailView(APIView):
    """Resolve by stable UUID; a slug is only a readable alternate lookup."""

    def get(
        self, request: Request, route_id: UUID | None = None, slug: str | None = None
    ) -> Response:
        include = {
            item.strip()
            for value in request.query_params.getlist("include")
            for item in value.split(",")
        }
        include_geometry = "geometry" in include
        queryset = public_route_queryset(detail=True, geometry=include_geometry)
        if route_id is not None:
            route = get_object_or_404(queryset, pk=route_id)
        else:
            route = get_object_or_404(queryset, slug=slug)
        return Response(
            RouteSerializer(
                route,
                context={"include_geometry": include_geometry, "request": request},
            ).data
        )


@extend_schema(
    responses={
        (200, "application/gpx+xml"): OpenApiResponse(
            response=OpenApiTypes.BINARY,
            description="The approved GPX file.",
        ),
        404: OpenApiResponse(description="GPX download is unavailable."),
    }
)
class RouteGpxDownloadView(APIView):
    """Serve a route's GPX file."""

    def get(self, request: Request, route_id: UUID) -> Response:
        del request
        route = get_object_or_404(public_route_queryset(), pk=route_id)
        version = route.current_approved_version
        if version is None or not version.original_gpx_storage_key:
            return Response({"detail": "GPX download is unavailable."}, status=404)
        storage_key = version.original_gpx_storage_key
        path = PurePosixPath(storage_key)
        if path.is_absolute() or ".." in path.parts or not default_storage.exists(storage_key):
            return Response({"detail": "GPX download is unavailable."}, status=404)
        if getattr(settings, "GPX_INTERNAL_REDIRECT", False):
            # Django authorizes, Nginx serves the file from an internal location.
            response = HttpResponse(status=status.HTTP_200_OK, content_type="application/gpx+xml")
            response["X-Accel-Redirect"] = "/_protected_gpx/" + quote(storage_key, safe="/")
            response["Content-Disposition"] = f'attachment; filename="{route.slug}.gpx"'
            response["X-Content-Type-Options"] = "nosniff"
            return response
        try:
            payload = default_storage.open(storage_key, "rb")
        except (FileNotFoundError, OSError):
            return Response({"detail": "GPX download is unavailable."}, status=404)
        return FileResponse(
            payload,
            as_attachment=True,
            filename=f"{route.slug}.gpx",
            content_type="application/gpx+xml",
        )


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
            filter_names = (
                "search",
                "author",
                "category",
                "min_distance_m",
                "max_distance_m",
                "min_ascent_m",
                "max_ascent_m",
            )
            filter_inputs = {name: request.query_params.get(name, "") for name in filter_names}
            normalized_filters = normalize_filter_inputs(filter_inputs)
            filtered_routes = (
                filter_routes(public_route_queryset(), request.query_params)
                if normalized_filters
                else None
            )
            result = query_viewport(
                west=values["west"],
                south=values["south"],
                east=values["east"],
                north=values["north"],
                zoom=zoom,
                limits=SpatialQueryLimits(max_routes=max_routes, max_cells=max_cells),
                candidate_queryset=filtered_routes,
                filter_inputs=filter_inputs,
            )
        except ValidationError as exc:
            raise ParseError(str(exc)) from exc
        return _cacheable_response(request, result)


def _cacheable_response(request: Request, result: dict[str, Any]) -> Response:
    """Let browsers and the CDN reuse a viewport briefly; the ETag is a payload digest."""

    digest = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    etag = f'"{digest[:32]}"'
    max_age = int(getattr(settings, "SPATIAL_HTTP_MAX_AGE", 60))
    headers = {"ETag": etag, "Cache-Control": f"public, max-age={max_age}"}
    if_none_match = request.headers.get("If-None-Match", "")
    if etag in {value.strip().removeprefix("W/") for value in if_none_match.split(",")}:
        return Response(status=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(result, headers=headers)
