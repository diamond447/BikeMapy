"""Celery entry points for the bounded BikeForum crawl."""

from __future__ import annotations

from typing import Any, cast

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings

from .crawler import run_crawl
from .gpx import GpxExtractionTaskFailure, cleanup_orphan_payload
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
def extract_gpx(source_id: int) -> dict[str, Any]:
    """Extract one source in isolation; terminal failures are persisted then raised."""

    result = run_gpx_extraction(source_id)
    if result.get("status") == ExtractionStatus.FAILED:
        raise GpxExtractionTaskFailure(result.get("error", "GPX extraction failed"))
    return result


@shared_task(name="bikemapy.ingestion.extract_gpx_route")  # type: ignore[untyped-decorator]
def extract_gpx_route(source_id: int) -> dict[str, Any]:
    """Compatibility task name for dispatchers using the route terminology."""

    result = run_gpx_extraction(source_id)
    if result.get("status") == ExtractionStatus.FAILED:
        raise GpxExtractionTaskFailure(result.get("error", "GPX extraction failed"))
    return result


@shared_task(name="bikemapy.ingestion.cleanup_orphan_gpx")  # type: ignore[untyped-decorator]
def cleanup_orphan_gpx(orphan_id: int) -> dict[str, Any]:
    """Retry a durable payload cleanup item after storage outages."""

    return cleanup_orphan_payload(orphan_id)


# A concise entry point is useful to dispatch from admin/management commands.
extract_route = extract_gpx_route
