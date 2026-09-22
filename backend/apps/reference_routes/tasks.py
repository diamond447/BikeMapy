"""Operator-triggered, bounded reference-route refresh task."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import Competition, Player

from .completion_services import calculate_completion
from .models import (
    ReferenceImport,
    ReferenceRouteVersion,
    RouteCompletion,
    RouteCompletionJob,
)
from .services import import_osm_snapshot


@shared_task(name="bikemapy.reference_routes.refresh")  # type: ignore[untyped-decorator]
def refresh_reference_routes(reference_import_id: int) -> dict[str, object]:
    """Process a stored snapshot by ID; raw bytes never travel through Celery."""

    source_import = ReferenceImport.objects.select_related("collection").get(pk=reference_import_id)
    if source_import.status != "discovered":
        return {"status": "unchanged", "import_id": source_import.pk}
    return import_osm_snapshot(
        collection=source_import.collection,
        payload=bytes(source_import.raw_response),
        endpoint=source_import.endpoint,
        query_text=source_import.query_text,
        retrieved_at=source_import.retrieved_at,
        response_metadata=source_import.response_metadata,
        stored_import_id=source_import.pk,
    )


@shared_task(name="bikemapy.reference_routes.calculate_completion")  # type: ignore[untyped-decorator]
def calculate_route_completion(job_id: int) -> dict[str, object]:
    """Claim and rebuild one route completion projection idempotently."""

    now = timezone.now()
    token = uuid4().hex
    route_version_id = subject_type = player_id = competition_id = None
    with transaction.atomic():
        try:
            job = RouteCompletionJob.objects.select_for_update().get(pk=job_id)
        except RouteCompletionJob.DoesNotExist:
            return {"status": "missing", "job_id": job_id}
        if job.status == RouteCompletionJob.Status.COMPLETE:
            return {"status": "complete", "job_id": job_id}
        if (
            job.status == RouteCompletionJob.Status.RUNNING
            and job.lease_until
            and job.lease_until > now
        ):
            return {"status": "in_progress", "job_id": job_id}
        if job.next_attempt_at > now:
            return {"status": "retry_scheduled", "job_id": job_id}
        route_version_id = job.route_version_id
        subject_type = job.subject_type
        player_id = job.player_id
        competition_id = job.competition_id
        job.status = RouteCompletionJob.Status.RUNNING
        job.attempts += 1
        job.lease_token = token
        job.lease_until = now + timedelta(
            seconds=int(getattr(settings, "ROUTE_COMPLETION_LEASE_SECONDS", 600))
        )
        job.dispatch_token = ""
        job.dispatch_lease_until = None
        job.save(
            update_fields=(
                "status",
                "attempts",
                "lease_token",
                "lease_until",
                "dispatch_token",
                "dispatch_lease_until",
            )
        )
    try:
        version = ReferenceRouteVersion.objects.get(pk=route_version_id)
        player = Player.objects.get(pk=player_id) if player_id is not None else None
        competition = (
            Competition.objects.get(pk=competition_id) if competition_id is not None else None
        )
        calculate_completion(
            version,
            subject_type,
            player=player,
            competition=competition,
            job_id=job_id,
            lease_token=token,
        )
        return {"status": "complete", "job_id": job_id}
    except Exception as exc:
        with transaction.atomic():
            try:
                locked = RouteCompletionJob.objects.select_for_update().get(pk=job_id)
            except RouteCompletionJob.DoesNotExist:
                return {"status": "missing", "job_id": job_id}
            if locked.lease_token != token:
                return {"status": "in_progress", "job_id": job_id}
            locked.status = RouteCompletionJob.Status.FAILED
            locked.error = str(exc)[:2000]
            locked.next_attempt_at = timezone.now() + timedelta(minutes=5)
            locked.lease_token = ""
            locked.lease_until = None
            locked.dispatch_token = ""
            locked.dispatch_lease_until = None
            locked.save(
                update_fields=(
                    "status",
                    "error",
                    "next_attempt_at",
                    "lease_token",
                    "lease_until",
                    "dispatch_token",
                    "dispatch_lease_until",
                )
            )
            failed_results = RouteCompletion.objects.filter(
                route_version_id=locked.route_version_id,
                subject_type=locked.subject_type,
            )
            failed_results = (
                failed_results.filter(player_id=locked.player_id)
                if locked.player_id is not None
                else failed_results.filter(player__isnull=True)
            )
            failed_results = (
                failed_results.filter(competition_id=locked.competition_id)
                if locked.competition_id is not None
                else failed_results.filter(competition__isnull=True)
            )
            failed_results.update(status="failed", error=str(exc)[:2000], updated_at=timezone.now())
        return {"status": "failed", "job_id": job_id, "error": str(exc)[:2000]}


def _claim_completion_dispatch() -> tuple[int, str] | None:
    now = timezone.now()
    with transaction.atomic():
        job = (
            RouteCompletionJob.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status=RouteCompletionJob.Status.PENDING)
                | Q(status=RouteCompletionJob.Status.FAILED),
                next_attempt_at__lte=now,
            )
            .filter(
                Q(dispatch_token="")
                | Q(dispatch_lease_until__isnull=True)
                | Q(dispatch_lease_until__lte=now)
            )
            .order_by("created_at", "pk")
            .first()
        )
        if job is None:
            return None
        token = uuid4().hex
        job.dispatch_token = token
        job.dispatch_lease_until = now + timedelta(
            seconds=int(getattr(settings, "ROUTE_COMPLETION_DISPATCH_LEASE_SECONDS", 60))
        )
        job.save(update_fields=("dispatch_token", "dispatch_lease_until"))
        return job.pk, token


def _publish_completion_dispatch(job_id: int, token: str) -> bool:
    try:
        calculate_route_completion.delay(job_id)
    except Exception as exc:
        with transaction.atomic():
            job = RouteCompletionJob.objects.select_for_update().filter(pk=job_id).first()
            if job is not None and job.dispatch_token == token:
                job.dispatch_token = ""
                job.dispatch_lease_until = None
                job.next_attempt_at = timezone.now()
                job.error = str(exc)[:2000]
                job.save(
                    update_fields=(
                        "dispatch_token",
                        "dispatch_lease_until",
                        "next_attempt_at",
                        "error",
                    )
                )
        return False
    with transaction.atomic():
        job = RouteCompletionJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None or job.dispatch_token != token:
            return False
        job.dispatched_at = timezone.now()
        # Retain the dispatch lease until the worker claims the row.  This
        # prevents a second dispatcher from publishing the same message
        # immediately, while an unconsumed broker message can be recovered
        # after the lease expires.
        job.save(update_fields=("dispatched_at",))
    return True


@shared_task(name="bikemapy.reference_routes.dispatch_completion_jobs")  # type: ignore[untyped-decorator]
def dispatch_completion_jobs(limit: int = 100) -> dict[str, int]:
    """Recover stale workers and publish each due row through a DB claim."""

    dispatched = 0
    now = timezone.now()
    stale_ids = (
        RouteCompletionJob.objects.filter(
            status=RouteCompletionJob.Status.RUNNING,
        )
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
        .values_list("pk", flat=True)[: max(1, limit)]
    )
    for stale_id in stale_ids:
        with transaction.atomic():
            try:
                stale = RouteCompletionJob.objects.select_for_update().get(pk=stale_id)
            except RouteCompletionJob.DoesNotExist:
                continue
            if stale.status != RouteCompletionJob.Status.RUNNING or (
                stale.lease_until is not None and stale.lease_until > now
            ):
                continue
            stale.status = RouteCompletionJob.Status.FAILED
            stale.error = "Worker lease expired."
            stale.next_attempt_at = now
            stale.lease_token = ""
            stale.lease_until = None
            stale.dispatch_token = ""
            stale.dispatch_lease_until = None
            stale.save(
                update_fields=(
                    "status",
                    "error",
                    "next_attempt_at",
                    "lease_token",
                    "lease_until",
                    "dispatch_token",
                    "dispatch_lease_until",
                )
            )
    for _ in range(max(1, limit)):
        claim = _claim_completion_dispatch()
        if claim is None:
            break
        job_id, token = claim
        if _publish_completion_dispatch(job_id, token):
            dispatched += 1
    return {"dispatched": dispatched}
