"""Celery entry points for bounded Strava synchronization work."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .activity_services import process_sync_job, purge_expired_webhook_events
from .models import StravaSyncJob, StravaSyncState


@shared_task(name="bikemapy.accounts.sync_strava_activities")  # type: ignore[untyped-decorator]
def sync_strava_activities_task(job_id: int) -> dict[str, Any]:
    result = process_sync_job(job_id)
    if result == "more":
        sync_strava_activities_task.apply_async(args=(job_id,))
    return {"status": result, "job_id": job_id}


@shared_task(name="bikemapy.accounts.dispatch_strava_sync")  # type: ignore[untyped-decorator]
def dispatch_strava_sync_task(limit: int = 100) -> dict[str, int]:
    """Publish due sync jobs with a short database-backed dispatch claim.

    The claim closes the race between two beat workers selecting the same
    pending row.  A broker outage leaves the claim to expire, after which a
    later dispatch can safely publish the row again.
    """

    from .models import Player

    purge_expired_webhook_events(limit=100)
    dispatched = 0
    now = timezone.now()
    claim_seconds = max(30, int(getattr(settings, "STRAVA_SYNC_DISPATCH_LEASE_SECONDS", 60)))
    # A worker can disappear after claiming a job. Requeue only expired
    # leases; a fresh RUNNING lease remains untouched.
    stale_jobs = (
        StravaSyncJob.objects.filter(
            status=StravaSyncJob.Status.RUNNING,
        )
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
        .order_by("updated_at", "pk")[: max(1, limit)]
    )
    for stale in stale_jobs:
        with transaction.atomic():
            # Keep the same State -> Job order as queueing and workers. The
            # state is locked even though recovery only mutates the job, so a
            # recovery cannot deadlock with a concurrent queue reset.
            StravaSyncState.objects.select_for_update().filter(player_id=stale.player_id).first()
            stale_job = StravaSyncJob.objects.select_for_update().get(pk=stale.pk)
            if stale_job.status != StravaSyncJob.Status.RUNNING or (
                stale_job.lease_until is not None and stale_job.lease_until > now
            ):
                continue
            stale_job.status = StravaSyncJob.Status.FAILED
            stale_job.last_error = "Worker lease expired."
            stale_job.next_attempt_at = now
            stale_job.lease_token = ""
            stale_job.lease_until = None
            stale_job.dispatch_token = ""
            stale_job.dispatch_lease_until = None
            stale_job.save(
                update_fields=(
                    "status",
                    "last_error",
                    "next_attempt_at",
                    "lease_token",
                    "lease_until",
                    "dispatch_token",
                    "dispatch_lease_until",
                    "updated_at",
                )
            )
    dispatch_limit = min(
        max(1, limit),
        max(1, int(getattr(settings, "STRAVA_SYNC_MAX_DISPATCH_PER_RUN", 10))),
    )
    for _ in range(dispatch_limit):
        candidate_ref = (
            StravaSyncJob.objects.filter(
                status__in=(StravaSyncJob.Status.PENDING, StravaSyncJob.Status.FAILED),
                retryable=True,
                next_attempt_at__lte=now,
                player__lifecycle=Player.Lifecycle.CONNECTED,
            )
            .filter(
                Q(dispatch_lease_until__isnull=True) | Q(dispatch_lease_until__lte=now),
            )
            .order_by("created_at", "pk")
            .values("pk", "player_id")
            .first()
        )
        if candidate_ref is None:
            break
        with transaction.atomic():
            StravaSyncState.objects.select_for_update().filter(
                player_id=candidate_ref["player_id"]
            ).first()
            candidate_job = (
                StravaSyncJob.objects.select_for_update()
                .filter(
                    pk=candidate_ref["pk"],
                    status__in=(StravaSyncJob.Status.PENDING, StravaSyncJob.Status.FAILED),
                    next_attempt_at__lte=now,
                    player__lifecycle=Player.Lifecycle.CONNECTED,
                )
                .filter(
                    Q(dispatch_lease_until__isnull=True) | Q(dispatch_lease_until__lte=now),
                )
                .first()
            )
            if candidate_job is None:
                continue
            token = uuid4().hex
            candidate_job.dispatch_token = token
            candidate_job.dispatch_lease_until = now + timedelta(seconds=claim_seconds)
            candidate_job.save(
                update_fields=("dispatch_token", "dispatch_lease_until", "updated_at")
            )
        try:
            sync_strava_activities_task.apply_async(args=(candidate_job.pk,))
        except Exception:
            StravaSyncJob.objects.filter(pk=candidate_job.pk, dispatch_token=token).update(
                dispatch_token="", dispatch_lease_until=None, updated_at=timezone.now()
            )
            continue
        dispatched += 1
    return {"dispatched": dispatched}
