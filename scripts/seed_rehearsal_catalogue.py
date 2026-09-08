#!/usr/bin/env python3
"""Create public, synthetic catalogue rows for disposable launch rehearsals."""

from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from apps.catalogue.models import (  # noqa: E402
    ForumPost,
    ForumThread,
    ProcessingStatus,
    Route,
    RouteLifecycle,
    RouteSource,
    RouteVersion,
)


def ensure_synthetic_source() -> None:
    """Create one source-backed route when the disposable database is empty."""

    if RouteSource.objects.exists():
        return
    thread = ForumThread.objects.create(
        url="https://bikeforum.example/rehearsal-thread",
        external_id="rehearsal-thread",
        title="Launch rehearsal ridge",
    )
    post = ForumPost.objects.create(
        thread=thread,
        url="https://bikeforum.example/rehearsal-thread#post-1",
        external_id="rehearsal-post-1",
    )
    route = Route.objects.create(lifecycle=RouteLifecycle.PUBLISHED)
    source = RouteSource.objects.create(
        route=route,
        mapy_url="https://mapy.com/s/rehearsal-seeded",
        source_title="Seeded rehearsal source",
    )
    source.posts.add(post)


def main() -> None:
    """Approve every synthetic source, preserving existing versions."""

    now = datetime.now(UTC)
    ensure_synthetic_source()
    approved = 0
    for source in RouteSource.objects.select_related("route").order_by("pk"):
        route = source.route
        if route.current_approved_version_id:
            continue
        version = RouteVersion.objects.create(
            source=source,
            version_number=1,
            checksum=f"launch-rehearsal-{source.pk}",
            technical_status=ProcessingStatus.VALID,
            approved_at=now,
        )
        route.current_approved_version = version
        route.save(update_fields=["current_approved_version", "updated_at"])
        approved += 1
    print(f"approved_routes={approved} total_routes={RouteSource.objects.count()}")


if __name__ == "__main__":
    main()
