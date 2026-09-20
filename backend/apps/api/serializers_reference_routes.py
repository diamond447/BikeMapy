"""Authenticated representations of completion reference routes."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

import json
from typing import Any

from rest_framework import serializers

from apps.reference_routes.models import ReferenceCollection, ReferenceRoute


def geometry_json(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "geojson"):
        return json.loads(value.geojson)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


class ReferenceAttributionSerializer(serializers.ModelSerializer[ReferenceCollection]):
    class Meta:
        model = ReferenceCollection
        fields = (
            "attribution",
            "attribution_text",
            "attribution_url",
            "licence",
            "licence_uri",
            "derivative_offer_url",
            "rightsholder",
            "contact_url",
            "source_url",
        )


class ReferenceStageSerializer(serializers.ModelSerializer[ReferenceRoute]):
    attribution = serializers.SerializerMethodField()
    geometry = serializers.SerializerMethodField()

    class Meta:
        model = ReferenceRoute
        fields = ("id", "source_identifier", "route_number", "title", "geometry", "attribution")

    def get_attribution(self, route: ReferenceRoute) -> dict[str, Any]:
        return dict(ReferenceAttributionSerializer(route.collection).data)

    def get_geometry(self, route: ReferenceRoute) -> Any:
        version = route.current_version
        return geometry_json(version.normalized_geometry) if version else None


class ReferenceRouteListSerializer(serializers.ModelSerializer[ReferenceRoute]):
    attribution = serializers.SerializerMethodField()

    class Meta:
        model = ReferenceRoute
        fields = (
            "id",
            "source_identifier",
            "route_number",
            "title",
            "operator",
            "network",
            "publication_status",
            "attribution",
        )

    def get_attribution(self, route: ReferenceRoute) -> dict[str, Any]:
        return dict(ReferenceAttributionSerializer(route.collection).data)


class ReferenceRouteSerializer(ReferenceRouteListSerializer):
    version = serializers.IntegerField(source="current_version.version_number", read_only=True)
    version_attribution_metadata = serializers.SerializerMethodField()
    geometry = serializers.SerializerMethodField()
    stages = ReferenceStageSerializer(many=True, read_only=True)

    class Meta(ReferenceRouteListSerializer.Meta):
        fields = ReferenceRouteListSerializer.Meta.fields + (
            "version",
            "version_attribution_metadata",
            "geometry",
            "stages",
        )  # type: ignore[assignment]

    def get_version_attribution_metadata(self, route: ReferenceRoute) -> dict[str, Any]:
        return dict(route.current_version.attribution_metadata) if route.current_version else {}

    def get_geometry(self, route: ReferenceRoute) -> Any:
        version = route.current_version
        return geometry_json(version.normalized_geometry) if version else None
