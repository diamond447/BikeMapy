"""Geometry field with a GeoDjango production implementation and test fallback."""

# The runtime base class is selected according to whether GDAL is available;
# django-stubs cannot model that import-time selection.
# mypy: ignore-errors

from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

_USE_GIS_BACKEND = "postgis" in settings.DATABASES["default"]["ENGINE"]

if _USE_GIS_BACKEND:
    try:
        from django.contrib.gis.db.models import GeometryField as _GeoGeometryField
        from django.contrib.gis.db.models import LineStringField as _GeoLineStringField
        from django.contrib.gis.db.models import PolygonField as _GeoPolygonField
    except (ImportError, ImproperlyConfigured) as exc:  # pragma: no cover
        raise ImproperlyConfigured("PostGIS requires GeoDjango and GDAL.") from exc
else:
    _GeoGeometryField = None  # type: ignore[assignment]
    _GeoLineStringField = None  # type: ignore[assignment]
    _GeoPolygonField = None  # type: ignore[assignment]


_GIS_AVAILABLE = _USE_GIS_BACKEND
_GeometryBase = _GeoLineStringField or models.TextField
_AnyGeometryBase = _GeoGeometryField or models.TextField
_PolygonBase = _GeoPolygonField or models.TextField


class RouteGeometryField(_GeometryBase):  # type: ignore[misc, type-arg]
    """WGS84 LineString field.

    In the PostGIS service this is a real GeoDjango ``LineStringField`` with
    SRID 4326, enabling spatial lookups and GeoDjango geometry adaptation. The
    SQLite quality suite uses a text representation because GDAL is not part of
    the lightweight host environment.
    """

    description = "WGS84 route LineString"

    def __init__(self, *args: Any, srid: int = 4326, **kwargs: Any) -> None:
        if _GIS_AVAILABLE:
            kwargs["srid"] = srid
        else:
            kwargs.pop("spatial_index", None)
        self.srid = srid
        super().__init__(*args, **kwargs)

    def db_type(self, connection: Any) -> str | None:
        if _GIS_AVAILABLE:
            return super().db_type(connection)
        return super().db_type(connection)

    def get_prep_value(self, value: Any) -> Any:
        if isinstance(value, dict | list | tuple):
            return json.dumps(value, separators=(",", ":"))
        return super().get_prep_value(value)


class RouteCoverageGeometryField(_AnyGeometryBase):  # type: ignore[misc, type-arg]
    """WGS84 geometry field for disjoint route-coverage components.

    Intersecting one route with multiple activity corridors naturally yields a
    ``MultiLineString``.  Completion evidence must preserve those components
    instead of coercing them into a single line and losing covered length.
    """

    description = "WGS84 route coverage geometry"

    def __init__(self, *args: Any, srid: int = 4326, **kwargs: Any) -> None:
        if _GIS_AVAILABLE:
            kwargs["srid"] = srid
        else:
            kwargs.pop("spatial_index", None)
        self.srid = srid
        super().__init__(*args, **kwargs)

    def get_prep_value(self, value: Any) -> Any:
        if isinstance(value, dict | list | tuple):
            return json.dumps(value, separators=(",", ":"))
        return super().get_prep_value(value)


class RoutePolygonField(_PolygonBase):  # type: ignore[misc, type-arg]
    """WGS84 polygon field used for persisted heatmap cell boundaries."""

    description = "WGS84 spatial polygon"

    def __init__(self, *args: Any, srid: int = 4326, **kwargs: Any) -> None:
        if _GIS_AVAILABLE:
            kwargs["srid"] = srid
        else:
            kwargs.pop("spatial_index", None)
        self.srid = srid
        super().__init__(*args, **kwargs)

    def get_prep_value(self, value: Any) -> Any:
        if isinstance(value, dict | list | tuple):
            return json.dumps(value, separators=(",", ":"))
        return super().get_prep_value(value)
