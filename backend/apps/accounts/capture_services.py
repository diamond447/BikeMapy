"""Persistent chronological capture projections for private competitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .models import (
    CaptureCalculation,
    CaptureFace,
    CaptureFaceOwner,
    CapturePlayerArea,
    Competition,
    CompetitionRecomputation,
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


class CaptureCalculationBusy(CaptureCalculationError):
    """Another worker currently owns the generation lease."""


def _decimal(value: float | Decimal) -> Decimal:
    return Decimal(str(max(0.0, float(value)))).quantize(AREA_QUANTUM, rounding=ROUND_HALF_UP)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return timezone.now()
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _line_coordinates(
    value: Any, *, max_coordinates: int, max_parts: int
) -> list[tuple[tuple[float, float], ...]]:
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
    if getattr(geometry, "num_coords", 0) > max_coordinates:
        raise CaptureCalculationError(f"coordinate limit exceeded: {max_coordinates}")
    if geometry.geom_type == "LineString":
        parts = [geometry.coords]
    elif geometry.geom_type == "MultiLineString":
        parts = [part.coords for part in geometry]
    else:
        raise CaptureCalculationError("Activity geometry must be a line.")
    result: list[tuple[tuple[float, float], ...]] = []
    for part in parts:
        if len(result) >= max_parts:
            raise CaptureCalculationError(f"trace limit exceeded: {max_parts}")
        coordinates = tuple((float(point[0]), float(point[1])) for point in part)
        if len(coordinates) > max_coordinates - sum(len(item) for item in result):
            raise CaptureCalculationError(f"coordinate limit exceeded: {max_coordinates}")
        if len(coordinates) >= 2:
            result.append(coordinates)
    if not result:
        raise CaptureCalculationError("Activity geometry has no usable line.")
    return result


def _activities_for_competition(competition: Competition) -> Any:
    """Stream only activities the competition is currently allowed to use."""

    from .competition_services import authorized_activity_queryset

    memberships = competition.memberships.filter(sharing_consent_at__isnull=False).exclude(
        sharing_scope="none"
    )
    return (
        authorized_activity_queryset(ImportedActivity.objects.all(), memberships)
        .order_by("calendar_date", "started_at", "pk")
        .iterator(chunk_size=32)
    )


def capture_traces(competition: Competition) -> tuple[CaptureTrace, ...]:
    """Build deterministic traces from the current eligible membership set."""

    from .capture_validation import MAX_COORDINATES, MAX_TRACES, CaptureTrace

    traces: list[CaptureTrace] = []
    coordinates_seen = 0
    for activity in _activities_for_competition(competition):
        remaining_traces = MAX_TRACES - len(traces)
        if remaining_traces <= 0:
            raise CaptureCalculationError(f"trace limit exceeded: {MAX_TRACES}")
        parts = _line_coordinates(
            activity.geometry,
            max_coordinates=MAX_COORDINATES - coordinates_seen,
            max_parts=remaining_traces,
        )
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
            coordinates_seen += len(coordinates)
        if len(traces) > MAX_TRACES:
            raise CaptureCalculationError(f"trace limit exceeded: {MAX_TRACES}")
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
    competition: Competition,
    *,
    reason: str = "activity-change",
    force_new_generation: bool = False,
) -> CaptureCalculation:
    """Create or reopen the generation matching the competition revision."""

    del reason  # Kept in the API for audit callers; generation is the durable reason.
    with transaction.atomic():
        competition = Competition.objects.select_for_update().get(pk=competition.pk)
        generation = competition.capture_revision
        if force_new_generation:
            generation += 1
            competition.capture_revision = generation
            competition.save(update_fields=("capture_revision", "updated_at"))
        calculation, _ = CaptureCalculation.objects.get_or_create(
            competition=competition,
            generation=generation,
            defaults={
                "algorithm_version": CAPTURE_ALGORITHM_VERSION,
                "status": CaptureCalculation.Status.PENDING,
            },
        )
        if (
            not force_new_generation
            and calculation.is_current
            and calculation.status == CaptureCalculation.Status.FRESH
        ):
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


def schedule_algorithm_version_rebuilds(*, limit: int | None = None) -> int:
    """Queue every active competition after a capture algorithm upgrade."""

    from .competition_services import schedule_recomputation

    count = 0
    competitions = Competition.objects.filter(is_active=True).order_by("pk")
    for competition in competitions:
        current = current_capture(competition)
        if current is not None and current.algorithm_version == CAPTURE_ALGORITHM_VERSION:
            continue
        if CompetitionRecomputation.objects.filter(
            competition=competition,
            generation=competition.revision,
            status__in=(
                CompetitionRecomputation.Status.PENDING,
                CompetitionRecomputation.Status.RUNNING,
            ),
        ).exists():
            continue
        schedule_recomputation(competition)
        count += 1
        if limit is not None and count >= max(1, limit):
            break
    return count


def _lease_seconds() -> int:
    try:
        configured = int(settings.GAME_RECOMPUTATION_LEASE_SECONDS)
    except (AttributeError, TypeError, ValueError):
        configured = 600
    return max(30, configured)


def claim_capture_calculation(
    competition: Competition, *, generation: int | None = None
) -> tuple[CaptureCalculation, str | None]:
    """Atomically claim a generation before doing expensive spatial work."""

    now = timezone.now()
    with transaction.atomic():
        if generation is None:
            competition = Competition.objects.get(pk=competition.pk)
            generation = competition.capture_revision
        calculation, _ = CaptureCalculation.objects.get_or_create(
            competition=competition,
            generation=generation,
            defaults={
                "algorithm_version": CAPTURE_ALGORITHM_VERSION,
                "status": CaptureCalculation.Status.PENDING,
            },
        )
        calculation = CaptureCalculation.objects.select_for_update().get(pk=calculation.pk)
        # Every successfully persisted generation is immutable. A completed
        # stale generation remains a valid historical snapshot and must not be
        # reclaimed merely because a newer revision became current.
        if calculation.status == CaptureCalculation.Status.FRESH:
            return calculation, None
        if (
            calculation.status == CaptureCalculation.Status.RUNNING
            and calculation.lease_until is not None
            and calculation.lease_until > now
        ):
            raise CaptureCalculationBusy("Capture generation is already being calculated.")
        if calculation.attempts >= MAX_CAPTURE_ATTEMPTS:
            raise CaptureCalculationError("Capture calculation retry limit exhausted.")
        token = uuid4().hex
        calculation.status = CaptureCalculation.Status.RUNNING
        calculation.attempts += 1
        calculation.started_at = now
        calculation.error = ""
        calculation.lease_token = token
        calculation.lease_until = now + timedelta(seconds=_lease_seconds())
        calculation.save(
            update_fields=(
                "status",
                "attempts",
                "started_at",
                "error",
                "lease_token",
                "lease_until",
            )
        )
    return calculation, token


def _mark_failed(calculation_id: int, error: str, lease_token: str) -> None:
    CaptureCalculation.objects.filter(
        pk=calculation_id,
        is_current=False,
        status=CaptureCalculation.Status.RUNNING,
        lease_token=lease_token,
    ).update(
        status=CaptureCalculation.Status.FAILED,
        error=error[:240],
        completed_at=timezone.now(),
        next_attempt_at=timezone.now(),
        lease_token="",
        lease_until=None,
    )


def _persist_faces(
    calculation: CaptureCalculation,
    result: ValidationResult,
    *,
    digest: str,
    lease_token: str,
) -> CaptureCalculation:
    with transaction.atomic():
        competition = Competition.objects.select_for_update().get(pk=calculation.competition_id)
        calculation = CaptureCalculation.objects.select_for_update().get(pk=calculation.pk)
        if (
            calculation.status != CaptureCalculation.Status.RUNNING
            or calculation.lease_token != lease_token
            or calculation.lease_until is None
            or calculation.lease_until <= timezone.now()
        ):
            raise CaptureCalculationBusy("Capture generation lease was replaced or expired.")
        current = (
            CaptureCalculation.objects.filter(competition=competition, is_current=True)
            .exclude(pk=calculation.pk)
            .order_by("-generation")
            .first()
        )
        calculation.faces.all().delete()
        calculation.player_areas.all().delete()
        area_totals: dict[int, Decimal] = {}
        member_ids = set(competition.memberships.values_list("player_id", flat=True))
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
        if calculation.generation == competition.capture_revision and (
            current is None or current.generation <= calculation.generation
        ):
            CaptureCalculation.objects.filter(competition=competition, is_current=True).exclude(
                pk=calculation.pk
            ).update(is_current=False)
            calculation.is_current = True
        else:
            calculation.is_current = False
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
    competition: Competition,
    *,
    generation: int | None = None,
    lease_token: str | None = None,
) -> CaptureCalculation:
    """Rebuild one immutable capture generation and publish it atomically."""

    if generation is None:
        competition.refresh_from_db(fields=("capture_revision",))
        generation = competition.capture_revision
    if lease_token is None:
        calculation, lease_token = claim_capture_calculation(competition, generation=generation)
    else:
        calculation = CaptureCalculation.objects.get(competition=competition, generation=generation)
    if calculation.status == CaptureCalculation.Status.FRESH:
        return calculation
    assert lease_token is not None
    try:
        if (
            calculation.status != CaptureCalculation.Status.RUNNING
            or calculation.lease_token != lease_token
            or calculation.lease_until is None
            or calculation.lease_until <= timezone.now()
        ):
            raise CaptureCalculationBusy("Capture generation lease was replaced or expired.")
        traces = capture_traces(competition)
        if traces and connection.vendor != "postgresql":
            raise CaptureCalculationError("Capture calculation requires PostGIS.")
        digest = _input_digest(competition, traces)
        from .capture_validation import validate_capture

        result = validate_capture(traces)
        return _persist_faces(calculation, result, digest=digest, lease_token=lease_token)
    except Exception as exc:
        _mark_failed(calculation.pk, str(exc), lease_token)
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
