"""Shared crawler response-cache retention policy."""

from __future__ import annotations

from django.conf import settings

MAX_BODY_RETENTION_SECONDS = 24 * 3600


def configured_body_retention_seconds() -> int:
    """Return the validated body-retention setting for application callers."""

    retention_seconds = int(getattr(settings, "BIKEFORUM_CACHE_BODY_RETENTION_SECONDS", 24 * 3600))
    maximum = MAX_BODY_RETENTION_SECONDS
    if retention_seconds < 1 or retention_seconds > maximum:
        raise ValueError(
            f"BIKEFORUM_CACHE_BODY_RETENTION_SECONDS must be between 1 and {maximum} seconds"
        )
    return retention_seconds
