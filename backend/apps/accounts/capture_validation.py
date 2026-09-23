"""Deterministic, bounded validation of activity capture topology.

This module deliberately has no persistence or game-facing API.  It is the
executable reference harness for the capture rules in ``docs/game-spec.md``.
PostGIS does the topology work because GEOS alone cannot provide the required
global equal-area and geography-area operations consistently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from django.contrib.gis.geos import GEOSException, LineString
from django.db import connection

PRAGUE: Final = ZoneInfo("Europe/Prague")
JOIN_TOLERANCE_M: Final = 50.0
SLIVER_AREA_M2: Final = 1.0
MAX_TRACES: Final = 2_000
MAX_COORDINATES: Final = 200_000
MAX_RETRIES: Final = 2
_SNAP_TOLERANCE_M: Final = 0.01


class CaptureValidationError(ValueError):
    """An input cannot be safely validated or exceeds the harness bounds."""


@dataclass(frozen=True, slots=True)
class CaptureTrace:
    """One activity geometry and the instant at which it was recorded."""

    trace_id: str
    owner_id: str
    recorded_at: datetime
    coordinates: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class CaptureFace:
    """A bounded polygon face and its winning owner(s)."""

    face_id: int
    area_m2: float
    owner_ids: tuple[str, ...]
    effective_date: date | None
    geometry_wkt: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Stable output suitable for fixture assertions and evidence reports."""

    faces: tuple[CaptureFace, ...]
    trace_count: int
    coordinate_count: int


@dataclass(frozen=True, slots=True)
class SafeValidationResult:
    """Result of a bounded attempt which never discards a valid prior result."""

    result: ValidationResult | None
    accepted: bool
    attempts: int
    error: str | None


def _wkt(trace: CaptureTrace) -> str:
    points = ", ".join(
        f"{longitude:.12f} {latitude:.12f}" for longitude, latitude in trace.coordinates
    )
    return f"LINESTRING ({points})"


def _validate_input(traces: tuple[CaptureTrace, ...]) -> int:
    if len(traces) > MAX_TRACES:
        raise CaptureValidationError(f"trace limit exceeded: {len(traces)} > {MAX_TRACES}")
    if len({trace.trace_id for trace in traces}) != len(traces):
        raise CaptureValidationError("trace_id values must be unique")
    coordinate_count = 0
    for trace in traces:
        if not trace.trace_id or not trace.owner_id:
            raise CaptureValidationError("trace_id and owner_id are required")
        if trace.recorded_at.tzinfo is None or trace.recorded_at.utcoffset() is None:
            raise CaptureValidationError(f"{trace.trace_id}: recorded_at must be timezone-aware")
        if len(trace.coordinates) < 2:
            raise CaptureValidationError(f"{trace.trace_id}: at least two coordinates are required")
        coordinate_count += len(trace.coordinates)
        if coordinate_count > MAX_COORDINATES:
            raise CaptureValidationError(
                f"coordinate limit exceeded: {coordinate_count} > {MAX_COORDINATES}"
            )
        for longitude, latitude in trace.coordinates:
            if not (math.isfinite(longitude) and math.isfinite(latitude)):
                raise CaptureValidationError(f"{trace.trace_id}: coordinates must be finite")
            if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                raise CaptureValidationError(f"{trace.trace_id}: coordinate outside WGS84 bounds")
        try:
            geometry = LineString(trace.coordinates, srid=4326)
        except (GEOSException, TypeError, ValueError) as exc:
            raise CaptureValidationError(f"{trace.trace_id}: invalid line geometry") from exc
        if geometry.empty or not geometry.valid:
            raise CaptureValidationError(f"{trace.trace_id}: invalid line geometry")
    return coordinate_count


def _ordered_traces(traces: tuple[CaptureTrace, ...]) -> tuple[CaptureTrace, ...]:
    return tuple(
        sorted(
            traces,
            key=lambda trace: (
                trace.owner_id,
                trace.trace_id,
                trace.recorded_at.astimezone(PRAGUE),
                trace.coordinates,
            ),
        )
    )


def validate_capture(traces: list[CaptureTrace] | tuple[CaptureTrace, ...]) -> ValidationResult:
    """Validate traces into deterministic owned faces using one PostGIS query.

    Input lines are transformed to EPSG:6933, a global metre/equal-area CRS.
    Endpoints and noisy vertices within 50 m are snapped before noding and
    polygonization.  A face is claimed only when one owner's traces from a
    common-or-newer Prague date cover its entire boundary.  The latest date
    for which that is true wins; ties retain all owners in sorted order.
    """

    ordered = _ordered_traces(tuple(traces))
    coordinate_count = _validate_input(ordered)
    if not ordered:
        return ValidationResult(faces=(), trace_count=0, coordinate_count=0)
    if connection.vendor != "postgresql" or "postgis" not in connection.settings_dict["ENGINE"]:
        raise CaptureValidationError("capture validation requires the PostGIS database backend")

    values: list[str] = []
    params: list[object] = []
    for trace in ordered:
        values.append("(%s, %s, %s::date, ST_Transform(ST_GeomFromText(%s, 4326), 6933))")
        params.extend(
            [
                trace.trace_id,
                trace.owner_id,
                trace.recorded_at.astimezone(PRAGUE).date(),
                _wkt(trace),
            ]
        )
    query = f"""
        WITH input(trace_id, owner_id, local_date, geom) AS (
            VALUES {", ".join(values)}
        ),
        settings AS (
            SELECT %s::double precision AS join_tolerance
        ),
        endpoints AS (
            SELECT trace_id, ST_StartPoint(geom) AS geom FROM input
            UNION ALL
            SELECT trace_id, ST_EndPoint(geom) AS geom FROM input
        ),
        snapped AS (
            SELECT i.trace_id, i.owner_id, i.local_date,
                   ST_SetPoint(
                       ST_SetPoint(
                               i.geom,
                               0,
                               CASE WHEN start_target.geom IS NOT NULL
                                    THEN start_target.geom
                                    ELSE ST_StartPoint(i.geom) END
                       ),
                       ST_NPoints(i.geom) - 1,
                       CASE WHEN end_target.geom IS NOT NULL
                            THEN end_target.geom
                            ELSE ST_EndPoint(i.geom) END
                   ) AS geom
            FROM input AS i CROSS JOIN settings
            LEFT JOIN LATERAL (
                SELECT endpoints.geom
                FROM endpoints
                WHERE ST_DWithin(ST_StartPoint(i.geom), endpoints.geom, settings.join_tolerance)
                ORDER BY ST_X(endpoints.geom), ST_Y(endpoints.geom)
                LIMIT 1
            ) AS start_target ON TRUE
            LEFT JOIN LATERAL (
                SELECT endpoints.geom
                FROM endpoints
                WHERE ST_DWithin(ST_EndPoint(i.geom), endpoints.geom, settings.join_tolerance)
                ORDER BY ST_X(endpoints.geom), ST_Y(endpoints.geom)
                LIMIT 1
            ) AS end_target ON TRUE
        ),
        network AS (
            SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom FROM snapped
        ),
        dumped_faces AS (
            SELECT (ST_Dump(ST_Polygonize(network.geom))).geom AS geom
            FROM network
        ),
        faces AS (
            SELECT row_number() OVER (ORDER BY ST_AsEWKB(geom))::integer AS face_id,
                   geom
            FROM dumped_faces
            WHERE ST_Area(geom) >= %s
        ),
        owner_dates AS (
            SELECT DISTINCT owner_id, local_date FROM snapped
        ),
        owner_networks AS (
            SELECT owner_dates.owner_id,
                   owner_dates.local_date,
                   ST_Buffer(ST_UnaryUnion(ST_Collect(snapped.geom)), %s) AS geom
            FROM owner_dates
            JOIN snapped
              ON snapped.owner_id = owner_dates.owner_id
             AND snapped.local_date >= owner_dates.local_date
            GROUP BY owner_dates.owner_id, owner_dates.local_date
        ),
        qualifying AS (
            SELECT faces.face_id, owner_networks.owner_id, owner_networks.local_date
            FROM faces CROSS JOIN owner_networks
            WHERE ST_CoveredBy(
                ST_Boundary(faces.geom),
                owner_networks.geom
            )
        ),
        best_dates AS (
            SELECT face_id, owner_id, max(local_date) AS effective_date
            FROM qualifying
            GROUP BY face_id, owner_id
        ),
        winners AS (
            SELECT face_id, max(effective_date) AS effective_date
            FROM best_dates GROUP BY face_id
        )
        SELECT faces.face_id,
               ST_AsText(ST_Transform(faces.geom, 4326)) AS geometry_wkt,
               ST_Area(ST_Transform(faces.geom, 4326)::geography) AS area_m2,
               winners.effective_date,
               COALESCE(
                   array_agg(best_dates.owner_id ORDER BY best_dates.owner_id)
                       FILTER (WHERE best_dates.effective_date = winners.effective_date),
                   ARRAY[]::text[]
               ) AS owner_ids
        FROM faces
        LEFT JOIN winners ON winners.face_id = faces.face_id
        LEFT JOIN best_dates ON best_dates.face_id = faces.face_id
        GROUP BY faces.face_id, faces.geom, winners.effective_date
        ORDER BY faces.face_id
    """
    # Only endpoints are joined: snapping every vertex would incorrectly join
    # nearby nested loops merely because their interiors happen to be close.
    params.extend([JOIN_TOLERANCE_M, _SNAP_TOLERANCE_M, SLIVER_AREA_M2])
    with connection.cursor() as cursor:
        try:
            cursor.execute(query, params)  # type: ignore[arg-type]
            rows = cursor.fetchall()
        except Exception as exc:
            raise CaptureValidationError("PostGIS capture validation failed") from exc
    faces = tuple(
        CaptureFace(
            face_id=int(face_id),
            area_m2=float(Decimal(str(area_m2))),
            owner_ids=tuple(sorted(owner_ids or ())),
            effective_date=effective_date,
            geometry_wkt=geometry_wkt,
        )
        for face_id, geometry_wkt, area_m2, effective_date, owner_ids in rows
    )
    return ValidationResult(
        faces=faces, trace_count=len(ordered), coordinate_count=coordinate_count
    )


def safe_validate(
    previous_result: ValidationResult | None,
    traces: list[CaptureTrace] | tuple[CaptureTrace, ...],
    *,
    max_retries: int = MAX_RETRIES,
) -> SafeValidationResult:
    """Attempt validation a bounded number of times and preserve last good data."""

    retries = max(0, min(max_retries, MAX_RETRIES))
    attempts = 0
    error: str | None = None
    for _ in range(retries + 1):
        attempts += 1
        try:
            return SafeValidationResult(validate_capture(traces), True, attempts, None)
        except CaptureValidationError as exc:
            error = str(exc)
    return SafeValidationResult(previous_result, False, attempts, error)


def postgis_available() -> bool:
    """Return whether this process is configured for the spatial harness."""

    return connection.vendor == "postgresql" and "postgis" in connection.settings_dict["ENGINE"]
