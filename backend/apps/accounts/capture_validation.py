"""Deterministic, bounded validation of activity capture topology.

This module deliberately has no persistence or game-facing API.  It is the
executable reference harness for the capture rules in ``docs/game-spec.md``.
PostGIS does the topology work because GEOS alone cannot provide the required
global equal-area and geography-area operations consistently.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

from django.db import DatabaseError, InterfaceError, OperationalError, connection, transaction
from psycopg.errors import QueryCanceled

PRAGUE: Final = ZoneInfo("Europe/Prague")
JOIN_TOLERANCE_M: Final = 50.0
SLIVER_AREA_M2: Final = 1.0
BOUNDARY_COVERAGE_TOLERANCE_M: Final = 0.01
MAX_TRACES: Final = 300
MAX_COORDINATES: Final = 30_000
MAX_FACES: Final = 200
MAX_RESULT_BYTES: Final = 8_000_000
STATEMENT_TIMEOUT_MS: Final = 5_000
MAX_RETRIES: Final = 2
_EXACT_ENDPOINT_TOLERANCE_M: Final = 0.1


class CaptureValidationError(ValueError):
    """An input cannot be safely validated or exceeds the harness bounds."""


class CaptureValidationTransientError(CaptureValidationError):
    """A database/network failure may succeed on a bounded retry."""


@dataclass(frozen=True, slots=True)
class CaptureTrace:
    """One activity geometry and the instant at which it was recorded."""

    trace_id: str
    owner_id: str
    recorded_at: datetime
    coordinates: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class CaptureBatch:
    """A bounded replay delta for the full-rebuild incremental baseline."""

    additions: tuple[CaptureTrace, ...] = ()
    removals: tuple[str, ...] = ()


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
    if not traces:
        return 0
    # Keep module importable for the lightweight SQLite suite, whose runners
    # intentionally do not install the optional GDAL runtime. Geometry
    # validation still uses Django's GEOS wrapper when this harness executes.
    from django.contrib.gis.geos import GEOSException, LineString

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

    EPSG:6933 is used for topology and equal-area output, while WGS84
    geography is used for the 50 m endpoint policy. Already-connected endpoint
    anchors are never pulled toward nearby nested loops. A face is claimed only
    when one owner's cumulative network covers its entire boundary.
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
        values.append("(%s, %s, %s::date, ST_GeomFromText(%s, 4326))")
        params.extend(
            [
                trace.trace_id,
                trace.owner_id,
                trace.recorded_at.astimezone(PRAGUE).date(),
                _wkt(trace),
            ]
        )
    query = f"""
        WITH input_raw(trace_id, owner_id, local_date, geom_wgs) AS (
            VALUES {", ".join(values)}
        ),
        settings AS (
            SELECT %s::double precision AS join_tolerance,
                   %s::double precision AS sliver_area,
                   %s::double precision AS boundary_tolerance,
                   %s::integer AS max_faces
        ),
        input AS (
            SELECT input_raw.*,
                   ST_Transform(input_raw.geom_wgs, 6933) AS geom
            FROM input_raw
        ),
        raw_endpoints AS (
            SELECT trace_id, owner_id, 0 AS endpoint_no,
                   ST_StartPoint(geom_wgs) AS geom_wgs
            FROM input
            UNION ALL
            SELECT trace_id, owner_id, 1 AS endpoint_no,
                   ST_EndPoint(geom_wgs) AS geom_wgs
            FROM input
        ),
        endpoints AS (
            SELECT endpoint.*,
                   EXISTS (
                       SELECT 1
                       FROM raw_endpoints AS other
                       WHERE (other.trace_id <> endpoint.trace_id
                              OR other.owner_id <> endpoint.owner_id
                              OR other.endpoint_no <> endpoint.endpoint_no)
                         AND ST_DWithin(
                             endpoint.geom_wgs::geography,
                             other.geom_wgs::geography,
                             %s
                         )
                   ) AS is_anchor
            FROM raw_endpoints AS endpoint
        ),
        snapped AS (
            SELECT i.trace_id, i.owner_id, i.local_date,
                   ST_SetPoint(
                       ST_SetPoint(
                           i.geom,
                           0,
                           CASE WHEN NOT start_endpoint.is_anchor
                                     AND start_target.geom_wgs IS NOT NULL
                                THEN ST_Transform(start_target.geom_wgs, 6933)
                                ELSE ST_StartPoint(i.geom) END
                       ),
                       ST_NPoints(i.geom) - 1,
                       CASE WHEN NOT end_endpoint.is_anchor
                                  AND end_target.geom_wgs IS NOT NULL
                            THEN ST_Transform(end_target.geom_wgs, 6933)
                            ELSE ST_EndPoint(i.geom) END
                   ) AS geom
            FROM input AS i
            CROSS JOIN settings
            JOIN endpoints AS start_endpoint
              ON start_endpoint.trace_id = i.trace_id
             AND start_endpoint.owner_id = i.owner_id
             AND start_endpoint.endpoint_no = 0
            JOIN endpoints AS end_endpoint
              ON end_endpoint.trace_id = i.trace_id
             AND end_endpoint.owner_id = i.owner_id
             AND end_endpoint.endpoint_no = 1
            LEFT JOIN LATERAL (
                SELECT endpoints.geom_wgs
                FROM endpoints
                WHERE ST_DWithin(
                          ST_StartPoint(i.geom_wgs)::geography,
                          endpoints.geom_wgs::geography,
                          settings.join_tolerance
                      )
                  AND endpoints.owner_id = i.owner_id
                ORDER BY endpoints.is_anchor DESC,
                         ST_X(endpoints.geom_wgs),
                         ST_Y(endpoints.geom_wgs)
                LIMIT 1
            ) AS start_target ON TRUE
            LEFT JOIN LATERAL (
                SELECT endpoints.geom_wgs
                FROM endpoints
                WHERE ST_DWithin(
                          ST_EndPoint(i.geom_wgs)::geography,
                          endpoints.geom_wgs::geography,
                          settings.join_tolerance
                      )
                  AND endpoints.owner_id = i.owner_id
                ORDER BY endpoints.is_anchor DESC,
                         ST_X(endpoints.geom_wgs),
                         ST_Y(endpoints.geom_wgs)
                LIMIT 1
            ) AS end_target ON TRUE
        ),
        owner_dates AS (
            SELECT DISTINCT owner_id, local_date FROM snapped
        ),
        owner_networks AS MATERIALIZED (
            SELECT owner_dates.owner_id,
                   owner_dates.local_date,
                   ST_UnaryUnion(ST_Collect(snapped.geom)) AS geom
            FROM owner_dates
            JOIN snapped
              ON snapped.owner_id = owner_dates.owner_id
             AND snapped.local_date >= owner_dates.local_date
            GROUP BY owner_dates.owner_id, owner_dates.local_date
        ),
        owner_polygon_collections AS MATERIALIZED (
            SELECT owner_networks.owner_id,
                   owner_networks.local_date,
                   ST_Polygonize(owner_networks.geom) AS polygons
            FROM owner_networks
            GROUP BY owner_networks.owner_id, owner_networks.local_date
        ),
        owner_claims AS (
            SELECT owner_networks.owner_id,
                   owner_networks.local_date,
                   (ST_Dump(owner_networks.polygons)).geom AS geom
            FROM owner_polygon_collections AS owner_networks
        ),
        claims AS (
            SELECT owner_claims.owner_id,
                   owner_claims.local_date,
                   owner_claims.geom
            FROM owner_claims
            CROSS JOIN settings
            WHERE ST_Area(owner_claims.geom) >= settings.sliver_area
        ),
        claim_boundaries AS (
            SELECT ST_UnaryUnion(ST_Collect(ST_Boundary(geom))) AS geom
            FROM claims
        ),
        face_polygon_collections AS MATERIALIZED (
            SELECT ST_Polygonize(claim_boundaries.geom) AS polygons
            FROM claim_boundaries
        ),
        dumped_faces AS (
            SELECT (ST_Dump(face_polygon_collections.polygons)).geom AS geom
            FROM face_polygon_collections
        ),
        candidate_faces AS (
            SELECT dumped_faces.geom
            FROM dumped_faces
            CROSS JOIN settings
            WHERE ST_Area(dumped_faces.geom) >= settings.sliver_area
        ),
        ranked_faces AS (
            SELECT row_number() OVER (ORDER BY ST_AsEWKB(geom))::integer AS face_id,
                   geom
            FROM candidate_faces
        ),
        faces AS (
            SELECT ranked_faces.*
            FROM ranked_faces CROSS JOIN settings
            WHERE ranked_faces.face_id <= settings.max_faces
        ),
        face_count AS (
            SELECT count(*)::integer AS generated_face_count
            FROM ranked_faces
        ),
        qualifying AS (
            SELECT faces.face_id, claims.owner_id, claims.local_date
            FROM faces CROSS JOIN claims
            WHERE ST_CoveredBy(
                faces.geom,
                ST_Buffer(claims.geom, (SELECT max(boundary_tolerance) FROM settings))
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
               face_count.generated_face_count,
               COALESCE(
                   array_agg(best_dates.owner_id ORDER BY best_dates.owner_id)
                       FILTER (WHERE best_dates.effective_date = winners.effective_date),
                   ARRAY[]::text[]
               ) AS owner_ids
        FROM faces
        LEFT JOIN winners ON winners.face_id = faces.face_id
        LEFT JOIN best_dates ON best_dates.face_id = faces.face_id
        CROSS JOIN face_count
        GROUP BY faces.face_id, faces.geom, winners.effective_date,
                 face_count.generated_face_count
        ORDER BY faces.face_id
    """
    # This order mirrors ``settings`` and then the exact endpoint guard.
    params.extend(
        [
            JOIN_TOLERANCE_M,
            SLIVER_AREA_M2,
            BOUNDARY_COVERAGE_TOLERANCE_M,
            MAX_FACES,
            _EXACT_ENDPOINT_TOLERANCE_M,
        ]
    )
    try:
        # A savepoint makes a canceled statement safe to retry inside Django's
        # surrounding transaction. SET LOCAL also cannot leak the timeout.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = %s", [STATEMENT_TIMEOUT_MS])
            cursor.execute(query, params)  # type: ignore[arg-type]
            rows = cursor.fetchall()
    except (OperationalError, InterfaceError) as exc:
        raise CaptureValidationTransientError("PostGIS capture validation can be retried") from exc
    except DatabaseError as exc:
        if isinstance(exc.__cause__, QueryCanceled) or "statement timeout" in str(exc).lower():
            raise CaptureValidationTransientError("PostGIS capture validation timed out") from exc
        raise CaptureValidationError("PostGIS capture validation failed") from exc
    if rows and rows[0][4] > MAX_FACES:
        raise CaptureValidationError(f"generated face limit exceeded: {rows[0][4]} > {MAX_FACES}")
    faces = tuple(
        CaptureFace(
            face_id=int(face_id),
            area_m2=float(Decimal(str(area_m2))),
            owner_ids=tuple(sorted(owner_ids or ())),
            effective_date=effective_date,
            geometry_wkt=geometry_wkt,
        )
        for face_id, geometry_wkt, area_m2, effective_date, _, owner_ids in rows
    )
    response_bytes = sum(len(face.geometry_wkt.encode("utf-8")) for face in faces)
    if response_bytes > MAX_RESULT_BYTES:
        raise CaptureValidationError(
            f"validation response limit exceeded: {response_bytes} > {MAX_RESULT_BYTES}"
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
        except CaptureValidationTransientError as exc:
            error = str(exc)
        except CaptureValidationError as exc:
            return SafeValidationResult(previous_result, False, attempts, str(exc))
    return SafeValidationResult(previous_result, False, attempts, error)


def validate_capture_incremental(
    batches: Iterable[CaptureBatch],
) -> ValidationResult:
    """Reference incremental replay used to prove full/incremental equivalence.

    The harness deliberately applies additions/removals and recomputes from the
    accumulated trace set after each batch. A later production cache may
    optimize this, but this baseline ensures that reordered delivery batches
    cannot change the authoritative result.
    """

    # Removal delivery is an authoritative tombstone.  Keeping it separate
    # from the current additions makes replay commutative: a delete received
    # before a delayed create still wins, and duplicate deliveries cannot
    # resurrect a trace.  For the impossible-but-defensive case of two
    # different payloads sharing an ID, choose one canonical payload rather
    # than letting delivery order decide the result.
    accumulated: dict[str, CaptureTrace] = {}
    removed: set[str] = set()
    result = ValidationResult(faces=(), trace_count=0, coordinate_count=0)
    for batch in batches:
        for trace_id in batch.removals:
            removed.add(trace_id)
            accumulated.pop(trace_id, None)
        for trace in batch.additions:
            if trace.trace_id in removed:
                continue
            previous = accumulated.get(trace.trace_id)
            if previous is None or (
                trace.owner_id,
                trace.recorded_at,
                trace.coordinates,
            ) < (
                previous.owner_id,
                previous.recorded_at,
                previous.coordinates,
            ):
                accumulated[trace.trace_id] = trace
        result = validate_capture(tuple(accumulated.values()))
    return result


def postgis_available() -> bool:
    """Return whether this process is configured for the spatial harness."""

    return connection.vendor == "postgresql" and "postgis" in connection.settings_dict["ENGINE"]
