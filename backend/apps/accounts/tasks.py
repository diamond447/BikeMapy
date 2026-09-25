"""Scheduled lifecycle maintenance for private player accounts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .competition_services import (
    DISPATCH_RETRY_SECONDS,
    MAX_DISPATCH_ATTEMPTS,
    authorized_activity_queryset,
)
from .models import (
    Competition,
    CompetitionRecomputation,
    CompetitionResult,
    CompetitionSharingConsentAudit,
    ImportedActivity,
)
from .services import cleanup_identity_guards, purge_expired_players, retry_revocations


@shared_task(name="bikemapy.accounts.cleanup_identity_guards")  # type: ignore[untyped-decorator]
def cleanup_identity_guards_task(limit: int = 100) -> dict[str, Any]:
    return cleanup_identity_guards(limit=max(1, limit))


@shared_task(name="bikemapy.accounts.purge_expired_players")  # type: ignore[untyped-decorator]
def purge_expired_players_task(limit: int = 100) -> dict[str, Any]:
    return purge_expired_players(limit=max(1, limit))


@shared_task(name="bikemapy.accounts.retry_revocations")  # type: ignore[untyped-decorator]
def retry_revocations_task(limit: int = 100) -> dict[str, Any]:
    return retry_revocations(limit=max(1, limit))


@shared_task(name="bikemapy.accounts.purge_expired_consent_audits")  # type: ignore[untyped-decorator]
def purge_expired_consent_audits_task(limit: int = 1000) -> dict[str, Any]:
    expired = CompetitionSharingConsentAudit.objects.filter(
        retention_until__lte=timezone.now()
    ).order_by("retention_until", "pk")[: max(1, limit)]
    ids = list(expired.values_list("pk", flat=True))
    deleted, _ = CompetitionSharingConsentAudit.objects.filter(pk__in=ids).delete()
    return {"purged": deleted}


@shared_task(name="bikemapy.accounts.recompute_competition_results")  # type: ignore[untyped-decorator]
def recompute_competition_results_task(job_id: int) -> dict[str, Any]:
    """Apply one durable generation in stable order.

    Score calculation is intentionally kept behind this boundary while game
    scoring is being built.  The generation marker means retries are
    idempotent and membership removal can never leave stale projections.
    """

    lease_token = ""
    try:
        # Read the foreign key before taking locks; all locked paths then use
        # the global Competition -> Job order.
        job_ref = CompetitionRecomputation.objects.get(pk=job_id)
        now = timezone.now()
        with transaction.atomic():
            competition = Competition.objects.select_for_update().get(pk=job_ref.competition_id)
            job = CompetitionRecomputation.objects.select_for_update().get(pk=job_id)
            if job.status == CompetitionRecomputation.Status.COMPLETED:
                return {"status": "completed", "job_id": job_id, "updated": 0}
            if job.status == CompetitionRecomputation.Status.RUNNING:
                if job.lease_until is not None and job.lease_until > now:
                    return {"status": "in_progress", "job_id": job_id, "updated": 0}
            if job.attempts >= MAX_DISPATCH_ATTEMPTS:
                job.status = CompetitionRecomputation.Status.FAILED
                job.error = job.error or "Retry limit exhausted."
                job.lease_token = ""
                job.lease_until = None
                job.save(update_fields=("status", "error", "lease_token", "lease_until"))
                return {"status": "failed", "job_id": job_id, "error": job.error}
            if job.status != CompetitionRecomputation.Status.RUNNING and job.next_attempt_at > now:
                return {"status": "retry_scheduled", "job_id": job_id, "updated": 0}
            lease_token = uuid4().hex
            job.status = CompetitionRecomputation.Status.RUNNING
            job.attempts += 1
            job.started_at = now
            job.lease_token = lease_token
            job.lease_until = now + timedelta(seconds=settings.GAME_RECOMPUTATION_LEASE_SECONDS)
            job.save(
                update_fields=("status", "attempts", "started_at", "lease_token", "lease_until")
            )
        with transaction.atomic():
            competition = Competition.objects.select_for_update().get(pk=job_ref.competition_id)
            job = CompetitionRecomputation.objects.select_for_update().get(pk=job_id)
            if job.status == CompetitionRecomputation.Status.COMPLETED:
                return {"status": "completed", "job_id": job_id, "updated": 0}
            if (
                job.status != CompetitionRecomputation.Status.RUNNING
                or job.lease_token != lease_token
            ):
                return {"status": "in_progress", "job_id": job_id, "updated": 0}
            # Heartbeat the lease immediately before the potentially expensive
            # projection transaction.
            job.lease_until = timezone.now() + timedelta(
                seconds=settings.GAME_RECOMPUTATION_LEASE_SECONDS
            )
            job.save(update_fields=("lease_until",))
            active_memberships = list(
                competition.memberships.filter(sharing_consent_at__isnull=False).exclude(
                    sharing_scope="none"
                )
            )
            active_players = {membership.player_id for membership in active_memberships}
            activities = list(
                authorized_activity_queryset(
                    ImportedActivity.objects.all(), active_memberships
                ).order_by("pk")
            )
            activity_ids = {activity.pk for activity in activities}
            CompetitionResult.objects.filter(competition=competition).exclude(
                player_id__in=active_players
            ).delete()
            CompetitionResult.objects.filter(competition=competition).exclude(
                activity_id__in=activity_ids
            ).exclude(activity__isnull=True).delete()
            existing_activity_ids = set(
                CompetitionResult.objects.filter(competition=competition).values_list(
                    "activity_id", flat=True
                )
            )
            for activity in activities:
                if activity.pk not in existing_activity_ids:
                    CompetitionResult.objects.create(
                        competition=competition,
                        player_id=activity.player_id,
                        activity=activity,
                        points=0,
                        computed_revision=job.generation,
                    )
            updated = 0
            for result in CompetitionResult.objects.filter(competition=competition).order_by(
                "player_id", "pk"
            ):
                next_revision = max(result.computed_revision, job.generation)
                if result.computed_revision != next_revision:
                    result.computed_revision = next_revision
                    result.save(update_fields=("computed_revision", "updated_at"))
                    updated += 1
            job.status = CompetitionRecomputation.Status.COMPLETED
            job.completed_at = timezone.now()
            job.lease_token = ""
            job.lease_until = None
            job.save(update_fields=("status", "completed_at", "lease_token", "lease_until"))
        return {"status": "completed", "job_id": job_id, "updated": updated}
    except Exception as exc:
        with transaction.atomic():
            try:
                job_ref = CompetitionRecomputation.objects.get(pk=job_id)
                Competition.objects.select_for_update().get(pk=job_ref.competition_id)
                job = CompetitionRecomputation.objects.select_for_update().get(pk=job_id)
            except CompetitionRecomputation.DoesNotExist:
                return {"status": "missing", "job_id": job_id}
            if not lease_token or job.lease_token != lease_token:
                if job.status == CompetitionRecomputation.Status.COMPLETED:
                    return {"status": "completed", "job_id": job_id, "updated": 0}
                return {"status": "in_progress", "job_id": job_id, "updated": 0}
            job.status = CompetitionRecomputation.Status.FAILED
            job.error = str(exc)[:240]
            retry_index = min(max(job.attempts - 1, 0), len(DISPATCH_RETRY_SECONDS) - 1)
            job.next_attempt_at = timezone.now() + timedelta(
                seconds=DISPATCH_RETRY_SECONDS[retry_index]
            )
            job.dispatched_at = None
            job.lease_token = ""
            job.lease_until = None
            job.save(
                update_fields=(
                    "status",
                    "error",
                    "next_attempt_at",
                    "dispatched_at",
                    "lease_token",
                    "lease_until",
                )
            )
        return {"status": "failed", "job_id": job_id, "error": str(exc)[:240]}


@shared_task(name="bikemapy.accounts.dispatch_competition_recomputations")  # type: ignore[untyped-decorator]
def dispatch_competition_recomputations_task(limit: int = 100) -> dict[str, Any]:
    """Retry durable outbox rows when broker publication or workers failed."""

    now = timezone.now()
    stale_running = (
        CompetitionRecomputation.objects.filter(status=CompetitionRecomputation.Status.RUNNING)
        .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
        .order_by("created_at", "pk")[: max(1, limit)]
    )
    for stale_job in stale_running:
        with transaction.atomic():
            try:
                job_ref = CompetitionRecomputation.objects.get(pk=stale_job.pk)
                Competition.objects.select_for_update().get(pk=job_ref.competition_id)
                job = CompetitionRecomputation.objects.select_for_update().get(pk=stale_job.pk)
            except (Competition.DoesNotExist, CompetitionRecomputation.DoesNotExist):
                continue
            if job.status != CompetitionRecomputation.Status.RUNNING or (
                job.lease_until is not None and job.lease_until > now
            ):
                continue
            retry_index = min(max(job.attempts - 1, 0), len(DISPATCH_RETRY_SECONDS) - 1)
            job.status = CompetitionRecomputation.Status.FAILED
            job.error = "Worker lease expired."
            job.next_attempt_at = now + timedelta(seconds=DISPATCH_RETRY_SECONDS[retry_index])
            job.dispatched_at = None
            job.lease_token = ""
            job.lease_until = None
            job.save(
                update_fields=(
                    "status",
                    "error",
                    "next_attempt_at",
                    "dispatched_at",
                    "lease_token",
                    "lease_until",
                )
            )
    dispatched = 0
    from .competition_services import _claim_recomputation_dispatch, _dispatch_recomputation

    for _ in range(max(1, limit)):
        claim = _claim_recomputation_dispatch()
        if claim is None:
            break
        job_id, token = claim
        if _dispatch_recomputation(job_id, dispatch_token=token):
            dispatched += 1
    return {"dispatched": dispatched}


# Keep the activity task module discoverable through Celery's conventional
# ``accounts.tasks`` autodiscovery while keeping the sync implementation
# separate from competition maintenance.
from .activity_tasks import (  # noqa: E402,F401
    dispatch_strava_sync_task,
    sync_strava_activities_task,
)
