"""Operator-triggered, bounded reference-route refresh task."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .completion_services import calculate_completion
from .models import (
    ReferenceImport,
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
    with transaction.atomic():
        try:
            job = (
                RouteCompletionJob.objects.select_for_update()
                .select_related("route_version", "player", "competition")
                .get(pk=job_id)
            )
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
        job.status = RouteCompletionJob.Status.RUNNING
        job.attempts += 1
        job.lease_token = token
        job.lease_until = now + timedelta(
            seconds=int(getattr(settings, "ROUTE_COMPLETION_LEASE_SECONDS", 600))
        )
        job.save(update_fields=("status", "attempts", "lease_token", "lease_until"))
    try:
        calculate_completion(
            job.route_version,
            job.subject_type,
            player=job.player,
            competition=job.competition,
        )
        with transaction.atomic():
            locked = RouteCompletionJob.objects.select_for_update().get(pk=job.pk)
            if locked.lease_token != token or locked.status != RouteCompletionJob.Status.RUNNING:
                return {"status": "in_progress", "job_id": job_id}
            locked.status = RouteCompletionJob.Status.COMPLETE
            locked.completed_at = timezone.now()
            locked.lease_token = ""
            locked.lease_until = None
            locked.error = ""
            locked.save(
                update_fields=("status", "completed_at", "lease_token", "lease_until", "error")
            )
        return {"status": "complete", "job_id": job_id}
    except Exception as exc:
        with transaction.atomic():
            locked = RouteCompletionJob.objects.select_for_update().get(pk=job.pk)
            if locked.lease_token != token:
                return {"status": "in_progress", "job_id": job_id}
            locked.status = RouteCompletionJob.Status.FAILED
            locked.error = str(exc)[:2000]
            locked.next_attempt_at = timezone.now() + timedelta(minutes=5)
            locked.lease_token = ""
            locked.lease_until = None
            locked.save(
                update_fields=("status", "error", "next_attempt_at", "lease_token", "lease_until")
            )
            RouteCompletion.objects.filter(
                route_version=locked.route_version,
                subject_type=locked.subject_type,
                player=locked.player,
                competition=locked.competition,
            ).update(status="failed", error=str(exc)[:2000], updated_at=timezone.now())
        return {"status": "failed", "job_id": job_id, "error": str(exc)[:2000]}


@shared_task(name="bikemapy.reference_routes.dispatch_completion_jobs")  # type: ignore[untyped-decorator]
def dispatch_completion_jobs(limit: int = 100) -> dict[str, int]:
    """Publish due route completion jobs using a short database claim."""

    dispatched = 0
    now = timezone.now()
    stale_ids = RouteCompletionJob.objects.filter(
        status=RouteCompletionJob.Status.RUNNING,
    ).filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now)).values_list("pk", flat=True)[
        : max(1, limit)
    ]
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
            stale.save(
                update_fields=("status", "error", "next_attempt_at", "lease_token", "lease_until")
            )
    for job_id in (
        RouteCompletionJob.objects.filter(
            Q(status=RouteCompletionJob.Status.PENDING)
            | Q(status=RouteCompletionJob.Status.FAILED),
            next_attempt_at__lte=now,
        )
        .order_by("created_at", "pk")
        .values_list("pk", flat=True)[: max(1, limit)]
    ):
        try:
            calculate_route_completion.delay(job_id)
            dispatched += 1
        except Exception:
            continue
    return {"dispatched": dispatched}
