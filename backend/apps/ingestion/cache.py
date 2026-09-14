"""Retention controls for the crawler's short-lived response bodies."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from .cache_policy import configured_body_retention_seconds
from .models import CrawlResponseCache

logger = logging.getLogger(__name__)


def cleanup_expired_crawl_response_bodies(
    *, now: datetime | None = None, limit: int | None = None
) -> dict[str, int]:
    """Clear expired HTML bodies while retaining conditional-request metadata.

    IDs are selected and updated in one bounded batch using the partial expiry
    index. The second body and expiry predicates make the update safe when a
    fetch or another cleanup worker changes a row between the two queries.
    ``remaining`` uses the same indexed eligibility predicate, making cleanup
    backlog observable without scanning an unbounded metadata table.
    """

    retention_seconds = configured_body_retention_seconds()
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
        CrawlResponseCache.objects.filter(
            body__gt="", body_expires_at__isnull=False, body_expires_at__lte=current_time
        )
        .order_by("body_expires_at", "pk")
        .values_list("pk", flat=True)[:batch_size]
    )
    cleared = CrawlResponseCache.objects.filter(
        pk__in=candidate_ids,
        body__gt="",
        body_expires_at__isnull=False,
        body_expires_at__lte=current_time,
    ).update(body="", body_expires_at=None)
    remaining = CrawlResponseCache.objects.filter(
        body__gt="", body_expires_at__isnull=False, body_expires_at__lte=current_time
    ).exists()
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
