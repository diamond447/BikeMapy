"""Authenticated representations of completion reference routes."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.reference_routes.models import ReferenceRoute


def geometry_json(value: Any) -> Any:
    if isinstance(value, str):
        import json

        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


class ReferenceStageSerializer(serializers.ModelSerializer[ReferenceRoute]):
    geometry = serializers.SerializerMethodField()
    attribution = serializers.CharField(source="current_version.attribution", read_only=True)

    class Meta:
        model = ReferenceRoute
        fields = ("id", "source_identifier", "route_number", "title", "geometry", "attribution")

    def get_geometry(self, route: ReferenceRoute) -> Any:
        version = route.current_version
        return geometry_json(version.normalized_geometry) if version else None


class ReferenceRouteSerializer(serializers.ModelSerializer[ReferenceRoute]):
    geometry = serializers.SerializerMethodField()
    attribution = serializers.CharField(source="current_version.attribution", read_only=True)
    source = serializers.CharField(source="collection.source_kind", read_only=True)
    stages = ReferenceStageSerializer(many=True, read_only=True)
    version = serializers.IntegerField(source="current_version.version_number", read_only=True)

    class Meta:
        model = ReferenceRoute
        fields = (
            "id",
            "source_identifier",
            "route_number",
            "title",
            "operator",
            "network",
            "source",
            "version",
            "geometry",
            "attribution",
            "stages",
        )

    def get_geometry(self, route: ReferenceRoute) -> Any:
        if not self.context.get("include_geometry", False):
            return None
        version = route.current_version
        return geometry_json(version.normalized_geometry) if version else None
