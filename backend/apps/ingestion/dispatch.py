"""Durable dispatch of GPX extraction for discovered sources.

Each try is an :class:`~apps.ingestion.models.ExtractionAttempt`, sent to the
broker only after its transaction commits.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import partial
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.catalogue.models import (
    ProcessingStatus,
    Route,
    RouteLifecycle,
    RouteSource,
    SourceDenylistEntry,
    SourceStatus,
)

from .models import ExtractionAttempt, ExtractionStatus

logger = logging.getLogger(__name__)


def _max_attempts() -> int:
    return max(1, int(getattr(settings, "GPX_MAX_ATTEMPTS", 3)))


def _dispatch_timeout() -> int:
    return max(1, int(getattr(settings, "GPX_DISPATCH_TIMEOUT", 900)))


def _processing_timeout() -> int:
    return max(1, int(getattr(settings, "GPX_PROCESSING_TIMEOUT", 1800)))


def _eligible(source: RouteSource) -> bool:
    return (
        source.route.lifecycle == RouteLifecycle.PUBLISHED
        and source.source_status != SourceStatus.UNAVAILABLE
        and not SourceDenylistEntry.objects.filter(source_url=source.mapy_url, active=True).exists()
    )


def _latest_attempt(source_id: int) -> ExtractionAttempt | None:
    return (
        ExtractionAttempt.objects.filter(source_id=source_id)
        .order_by("-attempt_number", "-pk")
        .first()
    )


def _provider_attempt_count(source_id: int) -> int:
    """Count attempts that reached the provider."""

    return ExtractionAttempt.objects.filter(
        source_id=source_id,
        status__in=[
            ExtractionStatus.PROCESSING,
            ExtractionStatus.SUCCEEDED,
            ExtractionStatus.FAILED,
            ExtractionStatus.SUPERSEDED,
        ],
    ).count()


def _send_attempt(attempt_id: int) -> None:
    """Send one committed attempt; on a broker error it stays queued for retry."""

    with transaction.atomic():
        attempt = ExtractionAttempt.objects.select_for_update().get(pk=attempt_id)
        if attempt.status != ExtractionStatus.QUEUED:
            return
        now = timezone.now()
        if attempt.dispatch_claimed_until and attempt.dispatch_claimed_until > now:
            return
        claim_token = uuid4().hex
        attempt.dispatch_attempts += 1
        attempt.dispatch_error = ""
        attempt.dispatch_claim_token = claim_token
        attempt.dispatch_claimed_until = now + timedelta(seconds=_dispatch_timeout())
        attempt.save(
            update_fields=[
                "dispatch_attempts",
                "dispatch_error",
                "dispatch_claim_token",
                "dispatch_claimed_until",
            ]
        )
        source_id = attempt.source_id

    try:
        from .tasks import extract_gpx_route

        result = extract_gpx_route.delay(source_id, attempt_id)
        task_id = str(getattr(result, "id", "") or "")
    except Exception as exc:  # broker outages are durable and retryable
        message = str(exc)[:4000]
        ExtractionAttempt.objects.filter(
            pk=attempt_id,
            status=ExtractionStatus.QUEUED,
            dispatch_claim_token=claim_token,
        ).update(
            dispatch_error=message,
            dispatch_claim_token="",
            dispatch_claimed_until=None,
        )
        logger.warning("Could not dispatch GPX extraction attempt %s", attempt_id, exc_info=True)
        return

    ExtractionAttempt.objects.filter(
        pk=attempt_id,
        status=ExtractionStatus.QUEUED,
        dispatch_claim_token=claim_token,
    ).update(
        dispatched_at=timezone.now(),
        dispatch_task_id=task_id,
        dispatch_error="",
        dispatch_claim_token="",
        dispatch_claimed_until=None,
    )


def dispatch_source_extraction(source_id: int, *, force: bool = False) -> ExtractionAttempt | None:
    """Queue one extraction attempt and send it after commit.

    Returns the open attempt if there is one; a successful source is never
    dispatched again. ``force`` still respects the attempt limit.
    """

    with transaction.atomic():
        source = RouteSource.objects.select_for_update().select_related("route").get(pk=source_id)
        if not _eligible(source):
            return None
        latest = _latest_attempt(source.pk)
        if latest is not None:
            if latest.status in {ExtractionStatus.QUEUED, ExtractionStatus.PROCESSING}:
                return latest
            if latest.status == ExtractionStatus.SUCCEEDED and not force:
                return latest
            if _provider_attempt_count(source.pk) >= _max_attempts():
                return latest
        attempt = ExtractionAttempt.objects.create(
            source=source,
            source_url=source.mapy_url,
            status=ExtractionStatus.QUEUED,
            attempt_number=(latest.attempt_number + 1) if latest else 1,
        )
        transaction.on_commit(partial(_send_attempt, attempt.pk))
        return attempt


def dispatch_sources(source_ids: set[int] | list[int]) -> dict[str, int]:
    """Queue a bounded set of sources discovered by one committed crawl page."""

    queued = 0
    skipped = 0
    for source_id in list(dict.fromkeys(source_ids)):
        before = ExtractionAttempt.objects.filter(source_id=source_id).count()
        attempt = dispatch_source_extraction(source_id)
        after = ExtractionAttempt.objects.filter(source_id=source_id).count()
        if attempt is not None and after > before:
            queued += 1
        else:
            skipped += 1
    return {"queued": queued, "skipped": skipped}


def reconcile_extraction_queue(*, limit: int = 100) -> dict[str, int | str]:
    """Retry bounded pending/failed dispatches without resetting history."""

    limit = max(1, int(limit))
    dispatched = 0
    queued = 0
    skipped = 0
    expired = 0
    stale_before = timezone.now() - timedelta(seconds=_dispatch_timeout())
    processing_before = timezone.now() - timedelta(seconds=_processing_timeout())

    # Fail attempts whose worker disappeared; recent ones keep running.
    stale_processing = ExtractionAttempt.objects.filter(
        status=ExtractionStatus.PROCESSING,
        started_at__lt=processing_before,
    ).order_by("started_at", "pk")[:limit]
    for stale_attempt in stale_processing:
        with transaction.atomic():
            source_id, route_id = (
                ExtractionAttempt.objects.filter(pk=stale_attempt.pk)
                .values_list("source_id", "source__route_id")
                .get()
            )
            Route.objects.select_for_update().get(pk=route_id)
            source = RouteSource.objects.select_for_update().get(pk=source_id)
            attempt = ExtractionAttempt.objects.select_for_update().get(pk=stale_attempt.pk)
            if attempt.status != ExtractionStatus.PROCESSING:
                continue
            message = "Extraction worker lease expired before completion."
            now = timezone.now()
            attempt.status = ExtractionStatus.FAILED
            attempt.error = message
            attempt.finished_at = now
            attempt.dispatch_claim_token = ""
            attempt.dispatch_claimed_until = None
            attempt.save(
                update_fields=[
                    "status",
                    "error",
                    "finished_at",
                    "dispatch_claim_token",
                    "dispatch_claimed_until",
                ]
            )
            if source.processing_status == ProcessingStatus.PROCESSING:
                source.processing_status = ProcessingStatus.FAILED
                source.processed_at = now
                source.last_error = message
                source.save(update_fields=["processing_status", "processed_at", "last_error"])
            expired += 1

    dispatch_budget = max(0, limit - expired)
    pending = list(
        ExtractionAttempt.objects.filter(status=ExtractionStatus.QUEUED)
        .filter(dispatched_at__isnull=True)
        .order_by("created_at", "pk")[:dispatch_budget]
    )
    stale = list(
        ExtractionAttempt.objects.filter(
            status=ExtractionStatus.QUEUED,
            dispatched_at__lt=stale_before,
        ).order_by("created_at", "pk")[: max(0, dispatch_budget - len(pending))]
    )
    attempt_ids = list(dict.fromkeys([attempt.pk for attempt in [*pending, *stale]]))
    for attempt_id in attempt_ids:
        _send_attempt(attempt_id)
        dispatched += 1

    remaining = max(0, dispatch_budget - dispatched)
    if remaining:
        candidates = (
            RouteSource.objects.select_related("route")
            .filter(
                processing_status__in=[
                    ProcessingStatus.DISCOVERED,
                    ProcessingStatus.FAILED,
                    ProcessingStatus.BLOCKED,
                ]
            )
            .exclude(extraction_attempts__status=ExtractionStatus.QUEUED)
            .exclude(extraction_attempts__status=ExtractionStatus.PROCESSING)
            .order_by("discovered_at", "pk")[:remaining]
        )
        for source in candidates:
            before = ExtractionAttempt.objects.filter(source_id=source.pk).count()
            candidate_attempt = dispatch_source_extraction(source.pk)
            after = ExtractionAttempt.objects.filter(source_id=source.pk).count()
            if candidate_attempt is not None and after > before:
                queued += 1
            else:
                skipped += 1
    return {
        "queued": queued,
        "dispatched": dispatched,
        "expired": expired,
        "skipped": skipped,
    }
