"""Celery entry points for the bounded BikeForum crawl."""

from __future__ import annotations

from typing import Any, cast

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from apps.catalogue.services import process_payload_deletion, retry_payload_deletions

from .cache import cleanup_expired_crawl_response_bodies
from .crawler import run_crawl
from .dispatch import block_extraction_attempt, extraction_gate_reason, reconcile_extraction_queue
from .gpx import (
    GpxExtractionTaskFailure,
    cleanup_orphan_payload,
    reconcile_orphan_payloads,
)
from .gpx import extract_gpx as run_gpx_extraction
from .models import CrawlTask, ExtractionStatus


@shared_task(name="bikemapy.ingestion.worker_smoke")  # type: ignore[untyped-decorator]
def worker_smoke() -> str:
    """Return a value that confirms a Celery worker can execute tasks."""

    return "worker-ready"


@shared_task(name="bikemapy.ingestion.crawl_bikeforum")  # type: ignore[untyped-decorator]
def crawl_bikeforum(
    start_url: str | None = None,
    *,
    max_pages: int | None = None,
    kind: str = CrawlTask.Kind.INCREMENTAL,
    stream: str | None = None,
    task_id: int | None = None,
) -> dict[str, Any]:
    """Run a bounded crawl; PostgreSQL state makes retries resumable."""

    if not all(
        getattr(settings, setting, False)
        for setting in (
            "BIKEFORUM_CRAWL_ENABLED",
            "BIKEFORUM_PROVIDER_AUTHORIZED",
            "BIKEFORUM_OPERATOR_APPROVED",
        )
    ):
        return {
            "status": "disabled",
            "reason": (
                "Real-source BikeForum crawling is disabled until provider and operator "
                "decisions are recorded"
            ),
        }

    return run_crawl(
        start_url=start_url or settings.BIKEFORUM_INCREMENTAL_URL,
        max_pages=max_pages or 10,
        kind=kind,
        stream=stream,
        task_id=task_id,
    )


@shared_task(name="bikemapy.ingestion.incremental_bikeforum_crawl")  # type: ignore[untyped-decorator]
def incremental_bikeforum_crawl() -> dict[str, Any]:
    """Scheduled daily incremental crawl entry point."""

    return cast(
        dict[str, Any],
        crawl_bikeforum(
            start_url=settings.BIKEFORUM_INCREMENTAL_URL,
            max_pages=10,
            kind=CrawlTask.Kind.INCREMENTAL,
            stream="incremental",
        ),
    )


@shared_task(name="bikemapy.ingestion.cleanup_crawl_response_cache")  # type: ignore[untyped-decorator]
def cleanup_crawl_response_cache(limit: int | None = None) -> dict[str, int]:
    """Clear one bounded batch of expired raw crawler response bodies."""

    return cleanup_expired_crawl_response_bodies(limit=limit)


@shared_task(name="bikemapy.ingestion.reconcile_extractions")  # type: ignore[untyped-decorator]
def reconcile_extractions(limit: int = 100) -> dict[str, Any]:
    """Dispatch bounded pending/failed source work after broker interruptions."""

    return reconcile_extraction_queue(limit=max(1, limit))


@shared_task(name="bikemapy.ingestion.backfill_bikeforum")  # type: ignore[untyped-decorator]
def backfill_bikeforum(start_url: str, max_pages: int = 10) -> dict[str, Any]:
    """Explicitly bounded historical backfill; no unbounded archive walk."""

    return cast(
        dict[str, Any],
        crawl_bikeforum(
            start_url=start_url,
            max_pages=max_pages,
            kind=CrawlTask.Kind.BACKFILL,
            stream=f"backfill:{start_url}",
        ),
    )


@shared_task(name="bikemapy.ingestion.extract_gpx")  # type: ignore[untyped-decorator]
def extract_gpx(source_id: int, attempt_id: int | None = None) -> dict[str, Any]:
    """Extract one source in isolation; terminal failures are persisted then raised."""

    gate_reason = extraction_gate_reason()
    if gate_reason is not None:
        try:
            return block_extraction_attempt(source_id, attempt_id=attempt_id, reason=gate_reason)
        except ObjectDoesNotExist as exc:
            raise GpxExtractionTaskFailure(gate_reason) from exc
    result = (
        run_gpx_extraction(source_id, attempt_id=attempt_id)
        if attempt_id is not None
        else run_gpx_extraction(source_id)
    )
    if result.get("status") == ExtractionStatus.FAILED:
        raise GpxExtractionTaskFailure(result.get("error", "GPX extraction failed"))
    return result


@shared_task(name="bikemapy.ingestion.extract_gpx_route")  # type: ignore[untyped-decorator]
def extract_gpx_route(source_id: int, attempt_id: int | None = None) -> dict[str, Any]:
    """Compatibility task name for dispatchers using the route terminology."""

    gate_reason = extraction_gate_reason()
    if gate_reason is not None:
        try:
            return block_extraction_attempt(source_id, attempt_id=attempt_id, reason=gate_reason)
        except ObjectDoesNotExist as exc:
            raise GpxExtractionTaskFailure(gate_reason) from exc
    result = (
        run_gpx_extraction(source_id, attempt_id=attempt_id)
        if attempt_id is not None
        else run_gpx_extraction(source_id)
    )
    if result.get("status") == ExtractionStatus.FAILED:
        raise GpxExtractionTaskFailure(result.get("error", "GPX extraction failed"))
    return result


@shared_task(name="bikemapy.ingestion.cleanup_orphan_gpx")  # type: ignore[untyped-decorator]
def cleanup_orphan_gpx(orphan_id: int) -> dict[str, Any]:
    """Retry a durable payload cleanup item after storage outages."""

    return cleanup_orphan_payload(orphan_id)


@shared_task(name="bikemapy.ingestion.reconcile_orphan_gpx")  # type: ignore[untyped-decorator]
def reconcile_orphan_gpx(limit: int = 100) -> dict[str, Any]:
    """Process a bounded batch of pending and failed orphan cleanups."""

    return reconcile_orphan_payloads(limit=max(1, limit))


@shared_task(name="bikemapy.ingestion.process_payload_deletion")  # type: ignore[untyped-decorator]
def process_payload_deletion_task(request_id: int) -> dict[str, Any]:
    """Remove one quarantined payload and finalize its audit metadata."""

    request = process_payload_deletion(request_id)
    return {"request_id": request.pk, "status": request.status, "attempts": request.attempts}


@shared_task(name="bikemapy.ingestion.retry_payload_deletions")  # type: ignore[untyped-decorator]
def retry_payload_deletions_task(limit: int = 100) -> dict[str, Any]:
    """Retry pending and failed deletion work after broker or storage outages."""

    requests = retry_payload_deletions(limit=max(1, limit))
    return {
        "processed": len(requests),
        "completed": sum(request.status == "completed" for request in requests),
    }


# A concise entry point is useful to dispatch from admin/management commands.
extract_route = extract_gpx_route
