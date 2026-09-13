"""Retention controls for the crawler's short-lived response bodies."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .models import CrawlResponseCache

logger = logging.getLogger(__name__)


def cleanup_expired_crawl_response_bodies(
    *, now: datetime | None = None, limit: int | None = None
) -> dict[str, int]:
    """Clear expired HTML bodies while retaining conditional-request metadata.

    IDs are selected and updated in one bounded batch. The second body
    predicate makes the update safe when another worker changes a row between
    the two queries. ``remaining`` makes cleanup backlog observable without
    scanning or deleting an unbounded number of rows in one invocation.
    """

    retention_seconds = max(
        1, int(getattr(settings, "BIKEFORUM_CACHE_BODY_RETENTION_SECONDS", 24 * 3600))
    )
    batch_size = max(
        1,
        int(
            limit
            if limit is not None
            else getattr(settings, "BIKEFORUM_CACHE_CLEANUP_BATCH_SIZE", 500)
        ),
    )
    current_time = now or timezone.now()
    cutoff = current_time - timedelta(seconds=retention_seconds)
    candidate_ids = list(
        CrawlResponseCache.objects.filter(body__gt="", fetched_at__lt=cutoff)
        .order_by("fetched_at", "pk")
        .values_list("pk", flat=True)[:batch_size]
    )
    cleared = CrawlResponseCache.objects.filter(
        pk__in=candidate_ids, body__gt="", fetched_at__lt=cutoff
    ).update(body="")
    remaining = CrawlResponseCache.objects.filter(body__gt="", fetched_at__lt=cutoff).exists()
    result = {
        "cleared": cleared,
        "remaining": int(remaining),
        "retention_seconds": retention_seconds,
        "batch_size": batch_size,
    }
    logger.info(
        "crawler response-cache body cleanup completed",
        extra={
            "cache_bodies_cleared": cleared,
            "cache_bodies_remaining": int(remaining),
            "cache_body_retention_seconds": retention_seconds,
            "cache_cleanup_batch_size": batch_size,
            "cache_cleanup_cutoff": cutoff.isoformat(),
        },
    )
    return result
