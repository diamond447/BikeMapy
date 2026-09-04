"""Conservative spatial similarity detection for route versions.

The scorer deliberately operates on plain GeoJSON-like coordinates.  This
keeps the quality suite usable with SQLite while using the same algorithm for
the PostGIS deployment.  Coordinates are resampled by travelled distance,
which makes the comparison tolerant of GPX point density and small GPS noise.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    LoopStatus,
    ModerationDecision,
    ProcessingStatus,
    RouteLifecycle,
    RouteVersion,
    SimilarityRelationship,
)

_EARTH_RADIUS_M = 6_371_008.8


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


@dataclass(frozen=True)
class SimilarityConfig:
    """Tunable, precision-oriented duplicate policy.

    ``duplicate_max_length_delta`` is intentionally 0.10 by default: routes
    with a meaningful extension or shortcut remain visible variants.  The
    distance thresholds are measured in metres after distance-based
    resampling, rather than depending on the number of GPX points.
    """

    sample_points: int = 64
    gps_tolerance_m: float = 30.0
    duplicate_max_length_delta: float = 0.10
    duplicate_max_mean_distance_m: float = 25.0
    duplicate_max_max_distance_m: float = 100.0
    variant_min_score: float = 0.55

    @classmethod
    def from_settings(cls) -> SimilarityConfig:
        return cls(
            sample_points=max(8, int(_setting("ROUTE_SIMILARITY_SAMPLE_POINTS", 64))),
            gps_tolerance_m=float(_setting("ROUTE_SIMILARITY_GPS_TOLERANCE_M", 30.0)),
            duplicate_max_length_delta=float(_setting("ROUTE_DUPLICATE_MAX_LENGTH_DELTA", 0.10)),
            duplicate_max_mean_distance_m=float(
                _setting("ROUTE_DUPLICATE_MAX_MEAN_DISTANCE_M", 25.0)
            ),
            duplicate_max_max_distance_m=float(
                _setting("ROUTE_DUPLICATE_MAX_MAX_DISTANCE_M", 100.0)
            ),
            variant_min_score=float(_setting("ROUTE_VARIANT_MIN_SCORE", 0.55)),
        )


@dataclass(frozen=True)
class SimilarityResult:
    """Auditable output of one pairwise comparison."""

    score: float
    mean_distance_m: float
    max_distance_m: float
    length_a_m: float
    length_b_m: float
    length_delta_ratio: float
    reversed_direction: bool
    start_offset_fraction: float
    duplicate: bool
    variant: bool
    evidence: dict[str, Any] = field(default_factory=dict)


def _coordinates(value: Any) -> list[tuple[float, float]]:
    """Extract finite longitude/latitude pairs from JSON or a GEOS value."""

    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if hasattr(value, "geojson"):
        try:
            value = json.loads(value.geojson)
        except (TypeError, ValueError):
            return []
    if isinstance(value, dict):
        value = value.get("coordinates", [])
    if not isinstance(value, list | tuple):
        return []
    result: list[tuple[float, float]] = []
    for pair in value:
        if not isinstance(pair, list | tuple) or len(pair) < 2:
            continue
        try:
            lon, lat = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(lon) and math.isfinite(lat):
            result.append((lon, lat))
    return result


def normalize_geometry(value: Any, *, decimals: int = 6) -> dict[str, Any]:
    """Return a stable, noise-tolerant GeoJSON LineString representation."""

    points = _clean(_coordinates(value))
    return {
        "type": "LineString",
        "coordinates": [[round(lon, decimals), round(lat, decimals)] for lon, lat in points],
    }


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    d_lat = lat2 - lat1
    d_lon = math.radians(b[0] - a[0])
    haversine = (
        math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, haversine)))


def _clean(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    for point in points:
        if not cleaned or _distance(cleaned[-1], point) > 0.01:
            cleaned.append(point)
    return cleaned


def _length(points: list[tuple[float, float]], *, closed: bool = False) -> float:
    if closed and len(points) > 2 and _distance(points[0], points[-1]) <= 0.01:
        points = points[:-1]
    if len(points) < 2:
        return 0.0
    result = sum(_distance(a, b) for a, b in zip(points, points[1:], strict=False))
    if closed:
        result += _distance(points[-1], points[0])
    return result


def _interpolate(
    a: tuple[float, float], b: tuple[float, float], fraction: float
) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction)


def _resample(
    points: list[tuple[float, float]], count: int, *, closed: bool
) -> list[tuple[float, float]]:
    """Resample a line at equal travelled-distance intervals."""

    points = _clean(points)
    if closed and len(points) > 2 and _distance(points[0], points[-1]) <= 0.01:
        points = points[:-1]
    if len(points) < 2:
        return points * count
    path = points + ([points[0]] if closed else [])
    segments = [_distance(a, b) for a, b in zip(path, path[1:], strict=False)]
    total = sum(segments)
    if total <= 0:
        return [points[0]] * count
    targets = (
        [total * index / count for index in range(count)]
        if closed
        else [total * index / (count - 1) for index in range(count)]
    )
    output: list[tuple[float, float]] = []
    segment_index = 0
    segment_start = 0.0
    for target in targets:
        while (
            segment_index < len(segments) - 1 and target > segment_start + segments[segment_index]
        ):
            segment_start += segments[segment_index]
            segment_index += 1
        segment_length = segments[segment_index]
        fraction = (target - segment_start) / segment_length if segment_length else 0.0
        output.append(_interpolate(path[segment_index], path[segment_index + 1], fraction))
    return output


def _pair_distance(
    a: list[tuple[float, float]], b: list[tuple[float, float]]
) -> tuple[float, float]:
    distances = [_distance(left, right) for left, right in zip(a, b, strict=True)]
    return sum(distances) / len(distances), max(distances)


def _best_alignment(
    points_a: list[tuple[float, float]], points_b: list[tuple[float, float]], *, loop: bool
) -> tuple[float, float, float, bool]:
    """Return mean/max distance, offset, and direction for the best alignment."""

    best = (float("inf"), float("inf"), 0.0, False)
    candidates = [(points_b, False), (list(reversed(points_b)), True)]
    count = len(points_a)
    shifts = range(count) if loop else range(1)
    for candidate, reversed_direction in candidates:
        for shift in shifts:
            aligned = candidate[shift:] + candidate[:shift] if loop and shift else candidate
            mean, maximum = _pair_distance(points_a, aligned)
            if (mean, maximum) < best[:2]:
                best = (mean, maximum, shift / count if loop else 0.0, reversed_direction)
    return best


def compare_geometries(
    geometry_a: Any,
    geometry_b: Any,
    *,
    loop_a: bool = False,
    loop_b: bool = False,
    config: SimilarityConfig | None = None,
) -> SimilarityResult:
    """Compare two route geometries while accounting for direction and loop starts."""

    policy = config or SimilarityConfig.from_settings()
    points_a, points_b = _clean(_coordinates(geometry_a)), _clean(_coordinates(geometry_b))
    if len(points_a) < 2 or len(points_b) < 2:
        raise ValidationError("Both route geometries require at least two coordinates.")
    loop = loop_a and loop_b
    length_a, length_b = _length(points_a, closed=loop_a), _length(points_b, closed=loop_b)
    sample_a = _resample(points_a, policy.sample_points, closed=loop)
    sample_b = _resample(points_b, policy.sample_points, closed=loop)
    mean, maximum, offset, reversed_direction = _best_alignment(sample_a, sample_b, loop=loop)
    longest = max(length_a, length_b, 1.0)
    length_delta = abs(length_a - length_b) / longest
    # A wider scoring window keeps visibly related variants linkable while
    # the absolute duplicate gates below remain strict.
    distance_score = math.exp(-mean / max(policy.gps_tolerance_m * 4, 0.001))
    length_score = max(0.0, 1.0 - length_delta)
    score = max(0.0, min(1.0, distance_score * length_score))
    duplicate = (
        length_delta <= policy.duplicate_max_length_delta
        and mean <= policy.duplicate_max_mean_distance_m
        and maximum <= policy.duplicate_max_max_distance_m
    )
    variant = not duplicate and score >= policy.variant_min_score
    evidence = {
        "algorithm": "distance-resampled-polyline-v1",
        "sample_points": policy.sample_points,
        "gps_tolerance_m": policy.gps_tolerance_m,
        "mean_distance_m": round(mean, 3),
        "max_distance_m": round(maximum, 3),
        "length_a_m": round(length_a, 2),
        "length_b_m": round(length_b, 2),
        "length_delta_ratio": round(length_delta, 5),
        "reversed_direction": reversed_direction,
        "start_offset_fraction": round(offset, 5),
        "duplicate_max_length_delta": policy.duplicate_max_length_delta,
        "duplicate_max_mean_distance_m": policy.duplicate_max_mean_distance_m,
        "duplicate_max_max_distance_m": policy.duplicate_max_max_distance_m,
        "variant_min_score": policy.variant_min_score,
        "duplicate_length_gate_passed": length_delta <= policy.duplicate_max_length_delta,
        "duplicate_mean_gate_passed": mean <= policy.duplicate_max_mean_distance_m,
        "duplicate_max_gate_passed": maximum <= policy.duplicate_max_max_distance_m,
    }
    return SimilarityResult(
        score=score,
        mean_distance_m=mean,
        max_distance_m=maximum,
        length_a_m=length_a,
        length_b_m=length_b,
        length_delta_ratio=length_delta,
        reversed_direction=reversed_direction,
        start_offset_fraction=offset,
        duplicate=duplicate,
        variant=variant,
        evidence=evidence,
    )


def compare_versions(
    version_a: RouteVersion,
    version_b: RouteVersion,
    *,
    config: SimilarityConfig | None = None,
) -> SimilarityResult:
    """Compare the normalized geometries of two immutable versions."""

    return compare_geometries(
        version_a.normalized_geometry or version_a.simplified_geometry,
        version_b.normalized_geometry or version_b.simplified_geometry,
        loop_a=version_a.loop_status == LoopStatus.LOOP,
        loop_b=version_b.loop_status == LoopStatus.LOOP,
        config=config,
    )


def _pair(route_a_id: Any, route_b_id: Any) -> tuple[Any, Any]:
    return (
        (route_a_id, route_b_id) if str(route_a_id) < str(route_b_id) else (route_b_id, route_a_id)
    )


@transaction.atomic
def link_similarity(
    version_a: RouteVersion,
    version_b: RouteVersion,
    result: SimilarityResult | None = None,
    *,
    actor: Any = None,
) -> SimilarityRelationship:
    """Persist similarity evidence idempotently, preserving moderation decisions."""

    if version_a.route_id == version_b.route_id:
        raise ValidationError("A route cannot be related to itself.")
    result = result or compare_versions(version_a, version_b)
    route_a, route_b = _pair(version_a.route_id, version_b.route_id)
    relationship_type = (
        SimilarityRelationship.RelationshipType.SUSPECTED_DUPLICATE
        if result.duplicate
        else SimilarityRelationship.RelationshipType.VARIANT
    )
    relationship, created = SimilarityRelationship.objects.get_or_create(
        route_a_id=route_a,
        route_b_id=route_b,
        defaults={
            "relationship_type": relationship_type,
            "similarity_score": round(result.score, 4),
            "evidence": result.evidence,
        },
    )
    # An explicit moderation decision owns the relationship classification.
    if (
        not created
        and not ModerationDecision.objects.filter(
            route_id__in=[route_a, route_b],
            action__in=[
                ModerationDecision.Action.KEEP_BOTH,
                ModerationDecision.Action.MERGE_SOURCES,
                ModerationDecision.Action.QUARANTINE,
                ModerationDecision.Action.RESTORE,
            ],
        ).exists()
    ):
        relationship.relationship_type = relationship_type
        relationship.similarity_score = round(result.score, 4)
        relationship.evidence = result.evidence
        relationship.save(update_fields=["relationship_type", "similarity_score", "evidence"])
    return relationship


def similar_versions(
    version: RouteVersion,
    *,
    config: SimilarityConfig | None = None,
) -> list[tuple[RouteVersion, SimilarityResult]]:
    """Find comparable valid versions on other non-removed routes."""

    candidates = (
        RouteVersion.objects.filter(
            technical_status=ProcessingStatus.VALID,
            normalized_geometry__isnull=False,
        )
        .exclude(source__route_id=version.route_id)
        .exclude(source__route__lifecycle=RouteLifecycle.SOFT_DELETED)
        .exclude(pk=version.pk)
        .select_related("source")
    )
    output: list[tuple[RouteVersion, SimilarityResult]] = []
    for candidate in candidates:
        try:
            result = compare_versions(version, candidate, config=config)
        except ValidationError:
            continue
        if result.duplicate or result.variant:
            output.append((candidate, result))
    return sorted(output, key=lambda item: item[1].score, reverse=True)


def classify_version(
    version: RouteVersion,
    *,
    config: SimilarityConfig | None = None,
    actor: Any = None,
) -> tuple[SimilarityRelationship | None, SimilarityResult | None]:
    """Link the strongest match and return its evidence for ingestion."""

    matches = similar_versions(version, config=config)
    if not matches:
        return None, None
    candidate, result = matches[0]
    return link_similarity(version, candidate, result, actor=actor), result


# Descriptive aliases make this boundary convenient for workers and callers.
score_geometries = compare_geometries
detect_similar_routes = similar_versions
