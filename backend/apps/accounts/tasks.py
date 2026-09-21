"""Scheduled lifecycle maintenance for private player accounts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .competition_services import DISPATCH_RETRY_SECONDS, MAX_DISPATCH_ATTEMPTS
from .models import Competition, CompetitionRecomputation, CompetitionResult
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


@shared_task(name="bikemapy.accounts.recompute_competition_results")  # type: ignore[untyped-decorator]
def recompute_competition_results_task(job_id: int) -> dict[str, Any]:
    """Apply one durable generation in stable order.

    Score calculation is intentionally kept behind this boundary while game
    scoring is being built.  The generation marker means retries are
    idempotent and membership removal can never leave stale projections.
    """

    try:
        with transaction.atomic():
            job = CompetitionRecomputation.objects.select_for_update().get(pk=job_id)
            if job.status == CompetitionRecomputation.Status.COMPLETED:
                return {"status": "completed", "job_id": job_id, "updated": 0}
            competition = Competition.objects.select_for_update().get(pk=job.competition_id)
            job.status = CompetitionRecomputation.Status.RUNNING
            job.attempts += 1
            job.started_at = timezone.now()
            job.save(update_fields=("status", "attempts", "started_at"))
            active_players = set(competition.memberships.values_list("player_id", flat=True))
            CompetitionResult.objects.filter(competition=competition).exclude(
                player_id__in=active_players
            ).delete()
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
            job.save(update_fields=("status", "completed_at"))
        return {"status": "completed", "job_id": job_id, "updated": updated}
    except Exception as exc:
        with transaction.atomic():
            try:
                job = CompetitionRecomputation.objects.select_for_update().get(pk=job_id)
            except CompetitionRecomputation.DoesNotExist:
                return {"status": "missing", "job_id": job_id}
            job.status = CompetitionRecomputation.Status.FAILED
            job.error = str(exc)[:240]
            retry_index = min(max(job.attempts - 1, 0), len(DISPATCH_RETRY_SECONDS) - 1)
            job.next_attempt_at = timezone.now() + timedelta(
                seconds=DISPATCH_RETRY_SECONDS[retry_index]
            )
            job.dispatched_at = None
            job.save(update_fields=("status", "error", "next_attempt_at", "dispatched_at"))
        return {"status": "failed", "job_id": job_id, "error": str(exc)[:240]}


@shared_task(name="bikemapy.accounts.dispatch_competition_recomputations")  # type: ignore[untyped-decorator]
def dispatch_competition_recomputations_task(limit: int = 100) -> dict[str, Any]:
    """Retry durable outbox rows when broker publication or workers failed."""

    now = timezone.now()
    stale_dispatch = now - timedelta(minutes=10)
    jobs = (
        CompetitionRecomputation.objects.filter(
            Q(status=CompetitionRecomputation.Status.PENDING)
            | Q(status=CompetitionRecomputation.Status.FAILED),
            attempts__lt=MAX_DISPATCH_ATTEMPTS,
            next_attempt_at__lte=now,
        )
        .filter(Q(dispatched_at__isnull=True) | Q(dispatched_at__lt=stale_dispatch))
        .order_by("created_at", "pk")[: max(1, limit)]
    )
    dispatched = 0
    from .competition_services import _dispatch_recomputation

    for job in jobs:
        _dispatch_recomputation(job.pk)
        dispatched += 1
    return {"dispatched": dispatched}
