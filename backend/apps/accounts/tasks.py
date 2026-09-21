"""Scheduled lifecycle maintenance for private player accounts."""

from __future__ import annotations

from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.db import transaction
from django.utils import timezone

from .models import CompetitionRecomputation, CompetitionResult
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

    with transaction.atomic():
        try:
            job = (
                CompetitionRecomputation.objects.select_for_update()
                .select_related("competition")
                .get(pk=job_id)
            )
        except CompetitionRecomputation.DoesNotExist:
            return {"status": "missing", "job_id": job_id}
        if job.status == CompetitionRecomputation.Status.COMPLETED:
            return {"status": "completed", "job_id": job_id, "updated": 0}
        job.status = CompetitionRecomputation.Status.RUNNING
        job.started_at = timezone.now()
        job.save(update_fields=("status", "started_at"))
        active_players = set(job.competition.memberships.values_list("player_id", flat=True))
        stale = CompetitionResult.objects.filter(competition=job.competition).exclude(
            player_id__in=active_players
        )
        stale.delete()
        updated = 0
        for result in CompetitionResult.objects.filter(competition=job.competition).order_by(
            "player_id", "pk"
        ):
            if result.computed_revision != job.generation:
                result.computed_revision = job.generation
                result.save(update_fields=("computed_revision", "updated_at"))
                updated += 1
        job.status = CompetitionRecomputation.Status.COMPLETED
        job.completed_at = timezone.now()
        job.save(update_fields=("status", "completed_at"))
    return {"status": "completed", "job_id": job_id, "updated": updated}
