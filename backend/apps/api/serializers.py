"""Public, read-only representations of the route catalogue."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any, cast

from django.conf import settings
from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.catalogue.models import Category, ForumPost, ModerationDecision, Route, RouteSource
from apps.catalogue.spatial import _geojson


class CategorySerializer(serializers.ModelSerializer[Category]):
    class Meta:
        model = Category
        fields = ("slug", "name", "description")


class ForumPostAttributionSerializer(serializers.ModelSerializer[ForumPost]):
    thread_title = serializers.CharField(source="thread.title", read_only=True)
    thread_url = serializers.URLField(source="thread.url", read_only=True)
    author = serializers.CharField(source="author.username", read_only=True, allow_null=True)

    class Meta:
        model = ForumPost
        fields = ("url", "thread_title", "thread_url", "author", "posted_at")


class SourceAttributionSerializer(serializers.ModelSerializer[RouteSource]):
    title = serializers.CharField(source="source_title", read_only=True)
    status = serializers.CharField(source="source_status", read_only=True)
    posts = ForumPostAttributionSerializer(many=True, read_only=True)
    last_checked_at = serializers.DateTimeField(read_only=True, allow_null=True)
    last_successful_check_at = serializers.DateTimeField(read_only=True, allow_null=True)

    class Meta:
        model = RouteSource
        fields = (
            "mapy_url",
            "title",
            "status",
            "last_checked_at",
            "last_successful_check_at",
            "posts",
        )


class VariantSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    slug = serializers.CharField()
    title = serializers.CharField()
    similarity_score = serializers.DecimalField(
        max_digits=5, decimal_places=4, allow_null=True, required=False
    )


class ElevationProfilePointSerializer(serializers.Serializer):
    distance_m = serializers.FloatField()
    elevation_m = serializers.FloatField()


GEOJSON_GEOMETRY_SCHEMA = {
    "type": "object",
    "required": ["type", "coordinates"],
    "properties": {
        "type": {
            "type": "string",
            "enum": ["Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon"],
        },
        "coordinates": {
            "oneOf": [
                {"type": "array", "items": {"type": "number"}},
                {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "number"}},
                },
                {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "number"},
                        },
                    },
                },
                {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "number"}},
                        },
                    },
                },
            ]
        },
    },
}


@extend_schema_field(GEOJSON_GEOMETRY_SCHEMA)
class GeoJSONGeometryField(serializers.JSONField):
    pass


class SpatialRouteSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    slug = serializers.CharField()
    title = serializers.CharField()
    geometry = GeoJSONGeometryField(allow_null=True)


class ViewportCellSerializer(serializers.Serializer):
    zoom = serializers.IntegerField()
    x = serializers.IntegerField()
    y = serializers.IntegerField()
    count = serializers.IntegerField()
    geometry = GeoJSONGeometryField(allow_null=True)


class ViewportResponseSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=("heatmap", "routes"))
    zoom = serializers.IntegerField()
    data_zoom = serializers.IntegerField(required=False, allow_null=True)
    cells = ViewportCellSerializer(many=True)
    routes = SpatialRouteSerializer(many=True)
    truncated = serializers.BooleanField()


class RouteSerializer(serializers.ModelSerializer[Route]):
    title = serializers.CharField(source="effective_title", read_only=True)
    categories = serializers.SerializerMethodField()
    distance_m = serializers.DecimalField(
        source="current_approved_version.distance_m",
        max_digits=12,
        decimal_places=2,
        allow_null=True,
        read_only=True,
    )
    ascent_m = serializers.DecimalField(
        source="current_approved_version.ascent_m",
        max_digits=10,
        decimal_places=2,
        allow_null=True,
        read_only=True,
    )
    descent_m = serializers.DecimalField(
        source="current_approved_version.descent_m",
        max_digits=10,
        decimal_places=2,
        allow_null=True,
        read_only=True,
    )
    loop_status = serializers.CharField(
        source="current_approved_version.loop_status", read_only=True
    )
    source_status = serializers.SerializerMethodField()
    sources = serializers.SerializerMethodField()
    variants = serializers.SerializerMethodField()
    geometry = serializers.SerializerMethodField()
    reviewed = serializers.SerializerMethodField()
    elevation_profile = serializers.SerializerMethodField()
    gpx_download_url = serializers.SerializerMethodField()

    class Meta:
        model = Route
        fields = (
            "id",
            "slug",
            "title",
            "categories",
            "distance_m",
            "ascent_m",
            "descent_m",
            "loop_status",
            "source_status",
            "sources",
            "variants",
            "geometry",
            "reviewed",
            "elevation_profile",
            "gpx_download_url",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    @extend_schema_field(CategorySerializer(many=True))
    def get_categories(self, route: Route) -> list[dict[str, Any]]:
        links = getattr(route, "public_category_links", None)
        if links is None:
            links = route.category_links.select_related("category").all()
        categories = [link.category for link in links]
        return cast(list[dict[str, Any]], CategorySerializer(categories, many=True).data)

    def get_source_status(self, route: Route) -> str:
        version = route.current_approved_version
        source = version.source if version is not None else None
        return source.source_status if source is not None else "unknown"

    @extend_schema_field(serializers.BooleanField())
    def get_reviewed(self, route: Route) -> bool:
        """Expose the badge only when all three explicit review checks pass."""

        decisions = getattr(route, "public_review_decisions", None)
        if decisions is None:
            decisions = route.moderation_decisions.filter(
                action=ModerationDecision.Action.REVIEW
            ).order_by("-created_at", "-pk")
        decision = next(iter(decisions), None)
        if decision is None:
            return False
        criteria = decision.metadata or {}
        return all(
            criteria.get(name) is True
            for name in ("technical_validity", "source_context", "content_suitability")
        )

    @extend_schema_field(ElevationProfilePointSerializer(many=True, allow_null=True))
    def get_elevation_profile(self, route: Route) -> list[dict[str, float]] | None:
        profile = getattr(route.current_approved_version, "elevation_profile", None)
        return profile or None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_gpx_download_url(self, route: Route) -> str | None:
        """Never expose a download URL until legal redistribution is approved."""

        if not getattr(settings, "GPX_REDISTRIBUTION_APPROVED", False):
            return None
        version = route.current_approved_version
        if version is None or not version.original_gpx_storage_key:
            return None
        request = self.context.get("request")
        path = reverse("public-route-gpx", kwargs={"route_id": route.pk})
        return request.build_absolute_uri(path) if request else path

    def _route_sources(self, route: Route) -> list[RouteSource]:
        direct = list(getattr(route, "public_sources", route.sources.all()))
        merged = [link.source for link in getattr(route, "public_source_merges", []) if link.active]
        seen: set[int] = set()
        result: list[RouteSource] = []
        for source in [*direct, *merged]:
            if source.pk not in seen:
                seen.add(source.pk)
                result.append(source)
        return result

    @extend_schema_field(SourceAttributionSerializer(many=True))
    def get_sources(self, route: Route) -> list[dict[str, Any]]:
        return cast(
            list[dict[str, Any]],
            SourceAttributionSerializer(self._route_sources(route), many=True).data,
        )

    @extend_schema_field(VariantSerializer(many=True))
    def get_variants(self, route: Route) -> list[dict[str, Any]]:
        relationships = [
            *getattr(route, "public_variant_relationships_a", []),
            *getattr(route, "public_variant_relationships_b", []),
        ]
        result: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for relationship in relationships:
            variant = (
                relationship.route_b
                if relationship.route_a_id == route.pk
                else relationship.route_a
            )
            if variant.pk in seen or not variant.is_public:
                continue
            seen.add(variant.pk)
            result.append(
                {
                    "id": variant.pk,
                    "slug": variant.slug,
                    "title": variant.effective_title,
                    "similarity_score": relationship.similarity_score,
                }
            )
        return cast(list[dict[str, Any]], VariantSerializer(result, many=True).data)

    @extend_schema_field(GEOJSON_GEOMETRY_SCHEMA | {"nullable": True})
    def get_geometry(self, route: Route) -> dict[str, Any] | None:
        if not self.context.get("include_geometry", False):
            return None
        version = route.current_approved_version
        if version is None:
            return None
        return _geojson(version.normalized_geometry)
