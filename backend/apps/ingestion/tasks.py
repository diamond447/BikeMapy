"""Celery entry points for the bounded BikeForum crawl."""

from __future__ import annotations

from typing import Any, cast

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings

from .crawler import run_crawl
from .models import CrawlTask


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
