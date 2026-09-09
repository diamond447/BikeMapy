"""Durable, gated dispatch for discovered route sources.

Source discovery and GPX extraction deliberately have different transactions.
An :class:`~apps.ingestion.models.ExtractionAttempt` is the durable hand-off:
it is created as ``queued`` and the broker is contacted only after its
transaction commits.  This keeps a rolled-back crawl from advertising work
that never became visible in the database, while retaining every extraction
retry as a separate attempt.
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


def extraction_gate_reason() -> str | None:
    """Return the first disabled activation gate, or ``None`` when enabled.

    The defaults are intentionally all off.  Technical readiness, provider
    authorization, and legal approval are separate deployment decisions, so
    enabling one can never silently activate real Mapy processing.
    """

    gates = (
        ("GPX_EXTRACTION_ENABLED", "route extraction is disabled"),
        ("GPX_PROVIDER_AUTHORIZED", "the GPX provider is not authorized"),
        ("GPX_LEGAL_APPROVED", "the legal extraction gate is not approved"),
    )
    for setting_name, reason in gates:
        if not bool(getattr(settings, setting_name, False)):
            return reason
    return None


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


def _send_attempt(attempt_id: int) -> None:
    """Submit one committed attempt and record broker metadata.

    A broker error leaves the row queued, making it visible to the bounded
    reconciler.  The conditional update also prevents a fast eager worker
    from having its terminal status overwritten by the sender.
    """

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


def block_extraction_attempt(
    source_id: int, *, attempt_id: int | None, reason: str
) -> dict[str, int | str | None]:
    """Fence queued work when execution gates are disabled.

    A blocked attempt remains durable and is picked up by reconciliation once
    the gates are enabled.  A completed source is left untouched when a
    manually invoked task is rejected by the gate.
    """

    with transaction.atomic():
        route_id = RouteSource.objects.values_list("route_id", flat=True).get(pk=source_id)
        Route.objects.select_for_update().get(pk=route_id)
        source = RouteSource.objects.select_for_update().select_related("route").get(pk=source_id)
        attempt = None
        if attempt_id is not None:
            attempt = ExtractionAttempt.objects.select_for_update().get(pk=attempt_id)
            if attempt.source_id != source.pk:
                raise ValueError("Extraction attempt belongs to another source")
        else:
            latest = _latest_attempt(source.pk)
            if latest is not None and latest.status == ExtractionStatus.SUCCEEDED:
                return {
                    "status": ExtractionStatus.BLOCKED,
                    "source_id": source_id,
                    "attempt_id": latest.pk,
                    "error": reason,
                }
            attempt = latest
        if attempt is None:
            attempt = ExtractionAttempt.objects.create(
                source=source,
                source_url=source.mapy_url,
                status=ExtractionStatus.BLOCKED,
                attempt_number=1,
                diagnostics={"activation_gate": reason},
                error=reason,
                finished_at=timezone.now(),
            )
        elif attempt.status in {ExtractionStatus.QUEUED, ExtractionStatus.PROCESSING}:
            attempt.status = ExtractionStatus.BLOCKED
            attempt.error = reason
            attempt.diagnostics = {**attempt.diagnostics, "activation_gate": reason}
            attempt.finished_at = timezone.now()
            attempt.dispatch_claim_token = ""
            attempt.dispatch_claimed_until = None
            attempt.save(
                update_fields=[
                    "status",
                    "error",
                    "diagnostics",
                    "finished_at",
                    "dispatch_claim_token",
                    "dispatch_claimed_until",
                ]
            )
        if source.processing_status in {ProcessingStatus.DISCOVERED, ProcessingStatus.PROCESSING}:
            source.processing_status = ProcessingStatus.BLOCKED
            source.last_error = reason
            source.save(update_fields=["processing_status", "last_error"])
        return {
            "status": ExtractionStatus.BLOCKED,
            "source_id": source_id,
            "attempt_id": attempt.pk,
            "error": reason,
        }


def dispatch_source_extraction(source_id: int, *, force: bool = False) -> ExtractionAttempt | None:
    """Create one queued extraction attempt and dispatch it after commit.

    Repeated calls while an attempt is queued/processing return that attempt;
    a successful source is never dispatched again.  ``force`` is reserved for
    an operator/reconciliation action and still respects the attempt limit.
    """

    if extraction_gate_reason() is not None:
        return None
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
            if latest.attempt_number >= _max_attempts():
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
    gate_reason = extraction_gate_reason()
    if gate_reason is not None:
        return {
            "queued": 0,
            "dispatched": 0,
            "expired": 0,
            "skipped": 0,
            "blocked": gate_reason,
        }

    dispatched = 0
    queued = 0
    skipped = 0
    expired = 0
    stale_before = timezone.now() - timedelta(seconds=_dispatch_timeout())
    processing_before = timezone.now() - timedelta(seconds=_processing_timeout())

    # A worker can disappear after claiming an attempt.  Convert only stale
    # processing rows to a retryable terminal failure; a recent worker keeps
    # its lease and is never duplicated by reconciliation.
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
