"""Deterministic spatial completion calculations for immutable route versions."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Competition, ImportedActivity, Player

from .models import (
    CompletionStatus,
    CompletionSubject,
    ReferenceRouteVersion,
    RouteCompletion,
    RouteCompletionEvidence,
    RouteCompletionJob,
    RouteCompletionMonthly,
    allow_completion_evidence_append,
)

DEFAULT_TOLERANCE_METERS = 50
DEFAULT_MAX_ACTIVITIES = 10_000
METRIC_SRID = 5514
ALGORITHM_VERSION = "corridor-v1"
DECIMAL_QUANTUM = Decimal("0.001")


class CompletionCalculationError(RuntimeError):
    """A safe, retryable calculation error with no credential data."""


class CompletionLeaseLost(CompletionCalculationError):
    """The durable worker lease was replaced before projection commit."""


def _geometry(value: Any) -> Any:
    if value is None:
        return None
    try:
        from django.contrib.gis.geos import GEOSGeometry
    except ImportError:
        return None
    if isinstance(value, GEOSGeometry):
        return value.clone()
    if isinstance(value, str):
        try:
            return GEOSGeometry(value, srid=4326)
        except (ValueError, TypeError):
            try:
                parsed = json.loads(value)
            except ValueError:
                return None
            value = parsed
    if isinstance(value, dict):
        try:
            return GEOSGeometry(json.dumps(value), srid=4326)
        except (ValueError, TypeError):
            return None
    return None


def _metric(value: Any) -> Any:
    geometry = _geometry(value)
    if geometry is None or geometry.empty:
        return None
    if not geometry.valid:
        try:
            geometry = geometry.make_valid()
        except (AttributeError, ValueError):
            return None
    if geometry.srid != 4326:
        geometry.srid = 4326
    try:
        geometry.transform(METRIC_SRID)
    except (AttributeError, TypeError, ValueError):
        return None
    return geometry


def _geographic(value: Any) -> Any:
    if value is None:
        return None
    result = value.clone()
    result.transform(4326)
    return result


def _rounded(value: float | Decimal) -> Decimal:
    return Decimal(str(max(0.0, float(value)))).quantize(DECIMAL_QUANTUM, rounding=ROUND_HALF_UP)


def _activity_date(activity: ImportedActivity) -> date:
    return activity.calendar_date or timezone.localtime(activity.started_at).date()


def _subject_activities(
    subject_type: str, *, player: Player | None, competition: Competition | None
) -> list[ImportedActivity]:
    limit = int(getattr(settings, "ROUTE_COMPLETION_MAX_ACTIVITIES", DEFAULT_MAX_ACTIVITIES))
    if limit <= 0:
        raise CompletionCalculationError("Completion activity limit is invalid.")
    if subject_type == CompletionSubject.PLAYER:
        if player is None:
            raise CompletionCalculationError("Player completion subject is missing.")
        query = ImportedActivity.objects.filter(
            player_id=player.pk, removed_at__isnull=True
        ).exclude(geometry__isnull=True)
    else:
        if competition is None:
            raise CompletionCalculationError("Competition completion subject is missing.")
        member_ids = competition.memberships.values_list("player_id", flat=True)
        query = ImportedActivity.objects.filter(
            player_id__in=member_ids,
            removed_at__isnull=True,
        ).exclude(geometry__isnull=True)
    if query.count() > limit:
        raise CompletionCalculationError(
            "Completion activity workload exceeds the configured limit."
        )
    return list(query.order_by("calendar_date", "started_at", "pk"))


def _coverage_for_activity(route: Any, activity: Any, tolerance: float) -> Any:
    if activity is None or activity.empty or not activity.valid:
        return None
    try:
        corridor = activity.buffer(tolerance, quadsegs=8)
        coverage = route.intersection(corridor)
        return coverage if not coverage.empty else None
    except (TypeError, ValueError, RuntimeError):
        return None


def _union_coverage(
    route: Any, activities: list[ImportedActivity], tolerance: float
) -> tuple[Any, list[dict[str, Any]], dict[date, Any]]:
    """Return unique route coverage, evidence, and month-first coverage."""

    from django.contrib.gis.geos import GeometryCollection, LineString

    del LineString  # Keep the import explicit for GDAL's geometry loader.
    coverage_union: Any = GeometryCollection(srid=METRIC_SRID)
    evidence: list[dict[str, Any]] = []
    monthly_new: dict[date, Any] = {}
    for activity in activities:
        metric_activity = _metric(activity.geometry)
        coverage = _coverage_for_activity(route, metric_activity, tolerance)
        if coverage is None:
            evidence.append({"activity": activity, "coverage": None, "skipped": True})
            continue
        try:
            previous = coverage_union
            combined = coverage_union.union(coverage)
            new_coverage = combined.difference(previous) if not previous.empty else combined
            coverage_union = combined
        except (TypeError, ValueError, RuntimeError):
            evidence.append({"activity": activity, "coverage": None, "skipped": True})
            continue
        evidence.append(
            {"activity": activity, "coverage": coverage, "new": new_coverage, "skipped": False}
        )
        month = _activity_date(activity).replace(day=1)
        monthly_new[month] = (
            new_coverage if month not in monthly_new else monthly_new[month].union(new_coverage)
        )
    return coverage_union, evidence, monthly_new


def _completion_record(
    version: ReferenceRouteVersion,
    subject_type: str,
    *,
    player: Player | None,
    competition: Competition | None,
) -> RouteCompletion:
    lookup = {
        "route_version": version,
        "subject_type": subject_type,
        "player": player if subject_type == CompletionSubject.PLAYER else None,
        "competition": competition if subject_type == CompletionSubject.COMPETITION else None,
    }
    completion, _ = RouteCompletion.objects.get_or_create(defaults={}, **lookup)
    return completion


def calculate_completion(
    version: ReferenceRouteVersion,
    subject_type: str,
    *,
    player: Player | None = None,
    competition: Competition | None = None,
    tolerance_meters: float | None = None,
    job_id: int | None = None,
    lease_token: str | None = None,
) -> RouteCompletion:
    """Rebuild one projection atomically, optionally fenced by a worker lease."""

    if (job_id is None) != (lease_token is None):
        raise CompletionCalculationError("Completion lease arguments must be provided together.")

    if subject_type not in CompletionSubject.values:
        raise CompletionCalculationError("Unknown completion subject.")
    tolerance = float(
        tolerance_meters
        if tolerance_meters is not None
        else getattr(settings, "ROUTE_COMPLETION_TOLERANCE_METERS", DEFAULT_TOLERANCE_METERS)
    )
    if tolerance <= 0 or tolerance > 500:
        raise CompletionCalculationError("Completion tolerance is outside the safe range.")
    route = _metric(version.normalized_geometry)
    if route is None or route.empty or route.geom_type not in {"LineString", "MultiLineString"}:
        raise CompletionCalculationError("Reference route geometry is invalid.")
    activities = _subject_activities(subject_type, player=player, competition=competition)
    total = float(route.length)
    union, evidence, monthly = _union_coverage(route, activities, tolerance)
    covered = min(total, max(0.0, float(union.length)))
    percent = 0.0 if total <= 0 else min(100.0, covered / total * 100)
    with transaction.atomic():
        job = None
        if job_id is not None:
            job = RouteCompletionJob.objects.select_for_update().get(pk=job_id)
            if (
                job.status != RouteCompletionJob.Status.RUNNING
                or job.lease_token != lease_token
                or not job.lease_until
                or job.lease_until <= timezone.now()
            ):
                raise CompletionLeaseLost("Completion worker lease was replaced or expired.")
            job.lease_until = timezone.now() + timedelta(
                seconds=int(getattr(settings, "ROUTE_COMPLETION_LEASE_SECONDS", 600))
            )
            job.save(update_fields=("lease_until",))
        completion = _completion_record(
            version, subject_type, player=player, competition=competition
        )
        completion.evidence_generation = uuid4()
        completion.status = CompletionStatus.FRESH
        completion.tolerance_meters = _rounded(tolerance)
        completion.total_length_meters = _rounded(total)
        completion.covered_length_meters = _rounded(covered)
        completion.completion_percent = _rounded(percent)
        completion.calculated_at = timezone.now()
        completion.requested_at = completion.requested_at or timezone.now()
        completion.algorithm_version = ALGORITHM_VERSION
        completion.route_checksum = version.checksum
        completion.membership_revision = competition.revision if competition else None
        completion.error = ""
        digest_payload = [
            {
                "activity_id": item["activity"].provider_activity_id,
                "geometry_hash": item["activity"].geometry_hash,
                "skipped": item["skipped"],
            }
            for item in evidence
        ]
        completion.evidence_digest = hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        completion.save()
        RouteCompletionMonthly.objects.filter(
            route_version=version,
            subject_type=subject_type,
            player=player if subject_type == CompletionSubject.PLAYER else None,
            competition=competition if subject_type == CompletionSubject.COMPETITION else None,
        ).delete()
        with allow_completion_evidence_append():
            for item in evidence:
                activity = item["activity"]
                geometry = item.get("coverage")
                RouteCompletionEvidence.objects.create(
                    completion=completion,
                    evidence_generation=completion.evidence_generation,
                    activity=activity,
                    provider_activity_id=activity.provider_activity_id,
                    activity_player_id=activity.player_id,
                    covered_length_meters=_rounded(geometry.length if geometry else 0),
                    covered_geometry=_geographic(geometry) if geometry else None,
                    activity_geometry_hash=activity.geometry_hash,
                    membership_revision=competition.revision if competition else None,
                    evidence={"skipped": bool(item["skipped"]), "algorithm": ALGORITHM_VERSION},
                )
        for month, geometry in monthly.items():
            RouteCompletionMonthly.objects.create(
                route_version=version,
                subject_type=subject_type,
                player=player if subject_type == CompletionSubject.PLAYER else None,
                competition=competition if subject_type == CompletionSubject.COMPETITION else None,
                month=month,
                covered_length_meters=_rounded(geometry.length),
                covered_geometry=_geographic(geometry),
            )
        if job is not None:
            job.status = RouteCompletionJob.Status.COMPLETE
            job.completed_at = timezone.now()
            job.lease_token = ""
            job.lease_until = None
            job.error = ""
            job.save(
                update_fields=("status", "completed_at", "lease_token", "lease_until", "error")
            )
    return completion


def schedule_completion(
    version: ReferenceRouteVersion,
    subject_type: str,
    *,
    player: Player | None = None,
    competition: Competition | None = None,
    reason: str = "activity-change",
) -> RouteCompletionJob:
    """Mark a projection pending and upsert one durable idempotent job."""

    if subject_type not in CompletionSubject.values:
        raise CompletionCalculationError("Unknown completion subject.")
    if subject_type == CompletionSubject.PLAYER and player is None:
        raise CompletionCalculationError("Player completion subject is missing.")
    if subject_type == CompletionSubject.COMPETITION and competition is None:
        raise CompletionCalculationError("Competition completion subject is missing.")
    if subject_type == CompletionSubject.PLAYER:
        assert player is not None
        subject_id = str(player.pk)
    else:
        assert competition is not None
        subject_id = str(competition.pk)
    key = f"{version.pk}:{subject_type}:{subject_id}"
    with transaction.atomic():
        completion = _completion_record(
            version, subject_type, player=player, competition=competition
        )
        completion.status = CompletionStatus.PENDING
        completion.requested_at = timezone.now()
        completion.error = ""
        completion.save(update_fields=("status", "requested_at", "error", "updated_at"))
        job, _ = RouteCompletionJob.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "route_version": version,
                "subject_type": subject_type,
                "player": player if subject_type == CompletionSubject.PLAYER else None,
                "competition": competition
                if subject_type == CompletionSubject.COMPETITION
                else None,
                "reason": reason[:255],
            },
        )
        if job.status == RouteCompletionJob.Status.RUNNING:
            # A new activity or membership revision supersedes work already
            # in flight.  Fencing the worker makes its eventual write
            # harmless; the pending row will be dispatched again against the
            # newest committed source data.
            job.status = RouteCompletionJob.Status.PENDING
            job.lease_token = ""
            job.lease_until = None
            job.next_attempt_at = timezone.now()
            job.completed_at = None
            job.dispatch_token = ""
            job.dispatch_lease_until = None
            job.save(
                update_fields=(
                    "status",
                    "lease_token",
                    "lease_until",
                    "next_attempt_at",
                    "completed_at",
                    "dispatch_token",
                    "dispatch_lease_until",
                )
            )
        elif job.status in {RouteCompletionJob.Status.COMPLETE, RouteCompletionJob.Status.FAILED}:
            job.status = RouteCompletionJob.Status.PENDING
            job.error = ""
            job.next_attempt_at = timezone.now()
            job.completed_at = None
            job.dispatch_token = ""
            job.dispatch_lease_until = None
            job.save(
                update_fields=(
                    "status",
                    "error",
                    "next_attempt_at",
                    "completed_at",
                    "dispatch_token",
                    "dispatch_lease_until",
                )
            )
    return job


def schedule_version_completions(version: ReferenceRouteVersion, *, reason: str) -> int:
    count = 0
    for player_id in Player.objects.filter(lifecycle=Player.Lifecycle.CONNECTED).values_list(
        "pk", flat=True
    ):
        schedule_completion(
            version, CompletionSubject.PLAYER, player=Player(pk=player_id), reason=reason
        )
        count += 1
    for competition in Competition.objects.filter(is_active=True).order_by("pk"):
        schedule_completion(
            version, CompletionSubject.COMPETITION, competition=competition, reason=reason
        )
        count += 1
    return count


def active_versions() -> list[ReferenceRouteVersion]:
    from .models import ReferenceValidationStatus

    return list(
        ReferenceRouteVersion.objects.filter(
            active=True,
            validation_status=ReferenceValidationStatus.VALID,
            route__active=True,
        ).select_related("route")
    )


def schedule_player_completions(player: Player, *, reason: str) -> int:
    count = 0
    for version in active_versions():
        schedule_completion(version, CompletionSubject.PLAYER, player=player, reason=reason)
        count += 1
    return count


def schedule_competition_completions(competition: Competition, *, reason: str) -> int:
    count = 0
    for version in active_versions():
        schedule_completion(
            version,
            CompletionSubject.COMPETITION,
            competition=competition,
            reason=reason,
        )
        count += 1
    return count
