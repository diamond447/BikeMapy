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

from .activity_services import process_sync_job
from .models import StravaSyncJob


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

    dispatched = 0
    now = timezone.now()
    claim_seconds = max(30, int(getattr(settings, "STRAVA_SYNC_DISPATCH_LEASE_SECONDS", 60)))
    for _ in range(max(1, limit)):
        with transaction.atomic():
            job = (
                StravaSyncJob.objects.select_for_update()
                .filter(
                    status__in=(StravaSyncJob.Status.PENDING, StravaSyncJob.Status.FAILED),
                    next_attempt_at__lte=now,
                    player__lifecycle=Player.Lifecycle.CONNECTED,
                )
                .filter(
                    Q(dispatch_lease_until__isnull=True) | Q(dispatch_lease_until__lte=now),
                )
                .order_by("created_at", "pk")
                .first()
            )
            if job is None:
                break
            token = uuid4().hex
            job.dispatch_token = token
            job.dispatch_lease_until = now + timedelta(seconds=claim_seconds)
            job.save(update_fields=("dispatch_token", "dispatch_lease_until", "updated_at"))
        try:
            sync_strava_activities_task.apply_async(args=(job.pk,))
        except Exception:
            StravaSyncJob.objects.filter(pk=job.pk, dispatch_token=token).update(
                dispatch_token="", dispatch_lease_until=None, updated_at=timezone.now()
            )
            continue
        dispatched += 1
    return {"dispatched": dispatched}
