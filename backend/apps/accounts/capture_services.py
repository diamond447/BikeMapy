"""Persistent chronological capture projections for private competitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from django.db import connection, transaction
from django.utils import timezone

from .models import (
    CaptureCalculation,
    CaptureFace,
    CaptureFaceOwner,
    CapturePlayerArea,
    Competition,
    ImportedActivity,
)

if TYPE_CHECKING:
    from .capture_validation import CaptureTrace, ValidationResult

CAPTURE_ALGORITHM_VERSION = "overlay-v2"
ALGORITHM_VERSION = CAPTURE_ALGORITHM_VERSION
AREA_QUANTUM = Decimal("0.001")
CAPTURE_RETRY_SECONDS = (30, 120, 600, 1800, 3600)
MAX_CAPTURE_ATTEMPTS = 5


class CaptureCalculationError(RuntimeError):
    """A capture rebuild failed without making the previous result unusable."""


def _decimal(value: float | Decimal) -> Decimal:
    return Decimal(str(max(0.0, float(value)))).quantize(AREA_QUANTUM, rounding=ROUND_HALF_UP)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return timezone.now()
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _line_coordinates(value: Any) -> list[tuple[tuple[float, float], ...]]:
    from django.contrib.gis.geos import GEOSGeometry

    if value is None:
        return []
    geometry = value.clone() if hasattr(value, "clone") else value
    if isinstance(geometry, str):
        try:
            geometry = GEOSGeometry(geometry, srid=4326)
        except (TypeError, ValueError) as exc:
            raise CaptureCalculationError("Activity geometry is invalid.") from exc
    if not hasattr(geometry, "geom_type") or geometry.empty:
        raise CaptureCalculationError("Activity geometry is empty.")
    if geometry.srid is None:
        geometry.srid = 4326
    if geometry.srid != 4326:
        geometry.transform(4326)
    if geometry.geom_type == "LineString":
        parts = [geometry.coords]
    elif geometry.geom_type == "MultiLineString":
        parts = [part.coords for part in geometry]
    else:
        raise CaptureCalculationError("Activity geometry must be a line.")
    result: list[tuple[tuple[float, float], ...]] = []
    for part in parts:
        coordinates = tuple((float(point[0]), float(point[1])) for point in part)
        if len(coordinates) >= 2:
            result.append(coordinates)
    if not result:
        raise CaptureCalculationError("Activity geometry has no usable line.")
    return result


def _activities_for_competition(competition: Competition) -> list[ImportedActivity]:
    member_ids = competition.memberships.values_list("player_id", flat=True)
    return list(
        ImportedActivity.objects.filter(
            player_id__in=member_ids,
            removed_at__isnull=True,
        )
        .exclude(geometry__isnull=True)
        .order_by("calendar_date", "started_at", "pk")
    )


def capture_traces(competition: Competition) -> tuple[CaptureTrace, ...]:
    """Build deterministic traces from the current eligible membership set."""

    from .capture_validation import CaptureTrace

    traces: list[CaptureTrace] = []
    for activity in _activities_for_competition(competition):
        parts = _line_coordinates(activity.geometry)
        recorded_at = _aware(activity.started_at or activity.imported_at)
        for part_index, coordinates in enumerate(parts):
            trace_id = f"{activity.pk}:{part_index}"
            traces.append(
                CaptureTrace(
                    trace_id=trace_id,
                    owner_id=str(activity.player_id),
                    recorded_at=recorded_at,
                    coordinates=coordinates,
                )
            )
    return tuple(traces)


def _input_digest(competition: Competition, traces: Iterable[CaptureTrace]) -> str:
    payload = {
        "algorithm": CAPTURE_ALGORITHM_VERSION,
        "competition": str(competition.pk),
        "revision": competition.revision,
        "members": sorted(
            str(value) for value in competition.memberships.values_list("player_id", flat=True)
        ),
        "traces": [
            {
                "id": trace.trace_id,
                "owner": trace.owner_id,
                "recorded_at": trace.recorded_at.isoformat(),
                "coordinates": trace.coordinates,
            }
            for trace in sorted(traces, key=lambda item: item.trace_id)
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def schedule_capture_calculation(
    competition: Competition, *, reason: str = "activity-change"
) -> CaptureCalculation:
    """Create or reopen the generation matching the competition revision."""

    del reason  # Kept in the API for audit callers; generation is the durable reason.
    with transaction.atomic():
        calculation, _ = CaptureCalculation.objects.get_or_create(
            competition=competition,
            generation=competition.revision,
            defaults={
                "algorithm_version": CAPTURE_ALGORITHM_VERSION,
                "status": CaptureCalculation.Status.PENDING,
            },
        )
        if calculation.is_current and calculation.status == CaptureCalculation.Status.FRESH:
            return calculation
        calculation.status = CaptureCalculation.Status.PENDING
        calculation.algorithm_version = CAPTURE_ALGORITHM_VERSION
        calculation.requested_at = timezone.now()
        calculation.error = ""
        calculation.next_attempt_at = timezone.now()
        calculation.lease_token = ""
        calculation.lease_until = None
        calculation.save(
            update_fields=(
                "status",
                "algorithm_version",
                "requested_at",
                "error",
                "next_attempt_at",
                "lease_token",
                "lease_until",
            )
        )
    return calculation


def schedule_algorithm_version_rebuilds() -> int:
    """Queue every active competition after a capture algorithm upgrade."""

    from .competition_services import schedule_recomputation

    count = 0
    for competition in Competition.objects.filter(is_active=True).order_by("pk"):
        schedule_recomputation(competition)
        count += 1
    return count


def _mark_failed(calculation_id: int, error: str) -> None:
    CaptureCalculation.objects.filter(pk=calculation_id, is_current=False).update(
        status=CaptureCalculation.Status.FAILED,
        error=error[:240],
        completed_at=timezone.now(),
    )


def _persist_faces(
    calculation: CaptureCalculation,
    result: ValidationResult,
    *,
    digest: str,
) -> CaptureCalculation:
    with transaction.atomic():
        calculation = CaptureCalculation.objects.select_for_update().get(pk=calculation.pk)
        current = (
            CaptureCalculation.objects.filter(competition=calculation.competition, is_current=True)
            .exclude(pk=calculation.pk)
            .order_by("-generation")
            .first()
        )
        calculation.faces.all().delete()
        calculation.player_areas.all().delete()
        area_totals: dict[int, Decimal] = {}
        member_ids = set(calculation.competition.memberships.values_list("player_id", flat=True))
        if result.faces:
            from django.contrib.gis.geos import GEOSGeometry

        for face in result.faces:
            geometry = GEOSGeometry(face.geometry_wkt, srid=4326)
            stored_face = CaptureFace.objects.create(
                calculation=calculation,
                face_id=face.face_id,
                geometry=geometry,
                area_m2=_decimal(face.area_m2),
                effective_date=face.effective_date,
            )
            owners = [int(owner_id) for owner_id in face.owner_ids if owner_id.isdigit()]
            owners = [owner_id for owner_id in owners if owner_id in member_ids]
            if not owners:
                continue
            shared_area = _decimal(face.area_m2 / len(owners))
            for owner_id in sorted(set(owners)):
                CaptureFaceOwner.objects.create(
                    face=stored_face, player_id=owner_id, shared_area_m2=shared_area
                )
                area_totals[owner_id] = area_totals.get(owner_id, Decimal("0")) + shared_area
        for player_id, area in sorted(area_totals.items()):
            CapturePlayerArea.objects.create(
                calculation=calculation,
                player_id=player_id,
                owned_area_m2=_decimal(area),
            )
        if current is None or current.generation <= calculation.generation:
            CaptureCalculation.objects.filter(
                competition=calculation.competition, is_current=True
            ).exclude(pk=calculation.pk).update(is_current=False)
            calculation.is_current = True
        calculation.status = CaptureCalculation.Status.FRESH
        calculation.algorithm_version = CAPTURE_ALGORITHM_VERSION
        calculation.input_digest = digest
        calculation.trace_count = result.trace_count
        calculation.coordinate_count = result.coordinate_count
        calculation.face_count = len(result.faces)
        calculation.error = ""
        calculation.completed_at = timezone.now()
        calculation.lease_token = ""
        calculation.lease_until = None
        calculation.save(
            update_fields=(
                "is_current",
                "status",
                "algorithm_version",
                "input_digest",
                "trace_count",
                "coordinate_count",
                "face_count",
                "error",
                "completed_at",
                "lease_token",
                "lease_until",
            )
        )
    return calculation


def calculate_capture(
    competition: Competition, *, generation: int | None = None
) -> CaptureCalculation:
    """Rebuild one immutable capture generation and publish it atomically."""

    generation = competition.revision if generation is None else generation
    calculation, _ = CaptureCalculation.objects.get_or_create(
        competition=competition,
        generation=generation,
        defaults={
            "algorithm_version": CAPTURE_ALGORITHM_VERSION,
            "status": CaptureCalculation.Status.PENDING,
        },
    )
    if calculation.status == CaptureCalculation.Status.FRESH and calculation.is_current:
        return calculation
    calculation.status = CaptureCalculation.Status.RUNNING
    calculation.algorithm_version = CAPTURE_ALGORITHM_VERSION
    calculation.started_at = timezone.now()
    calculation.error = ""
    calculation.save(update_fields=("status", "algorithm_version", "started_at", "error"))
    try:
        traces = capture_traces(competition)
        if traces and connection.vendor != "postgresql":
            raise CaptureCalculationError("Capture calculation requires PostGIS.")
        digest = _input_digest(competition, traces)
        from .capture_validation import validate_capture

        result = validate_capture(traces)
        return _persist_faces(calculation, result, digest=digest)
    except Exception as exc:
        _mark_failed(calculation.pk, str(exc))
        if isinstance(exc, CaptureCalculationError):
            raise
        raise CaptureCalculationError("Capture calculation failed safely.") from exc


def current_capture(competition: Competition) -> CaptureCalculation | None:
    return (
        CaptureCalculation.objects.filter(
            competition=competition,
            is_current=True,
            status=CaptureCalculation.Status.FRESH,
        )
        .prefetch_related("faces__owners", "player_areas")
        .first()
    )
