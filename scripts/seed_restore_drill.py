#!/usr/bin/env python3
"""Seed a representative, storage-backed application snapshot for recovery drills."""

from __future__ import annotations

import os
from datetime import UTC, datetime

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.contrib.gis.geos import GEOSGeometry  # noqa: E402
from django.db import transaction  # noqa: E402

from apps.analytics.models import AnalyticsCounter  # noqa: E402
from apps.catalogue.models import (  # noqa: E402
    Category,
    ForumAuthor,
    ForumPost,
    ForumThread,
    LoopStatus,
    ModerationDecision,
    ProcessingStatus,
    Route,
    RouteCategory,
    RouteLifecycle,
    RouteSource,
    RouteSourcePost,
    RouteVersion,
    SourceStatus,
    TitleProvenance,
)
from apps.ingestion.models import CrawlCheckpoint, CrawlTask, CrawlTaskStatus  # noqa: E402


def main() -> None:
    """Create the rows exercised by the post-restore API checks."""

    checksum = os.environ.get("GPX_SHA256", "")
    storage_key = os.environ.get("GPX_STORAGE_KEY", "gpx/routes/restore-drill.gpx")
    historical_checksum = os.environ.get("GPX_HISTORY_SHA256", "")
    historical_storage_key = os.environ.get(
        "GPX_HISTORY_STORAGE_KEY", "gpx/routes/restore-drill-history.gpx"
    )
    if len(checksum) != 64 or any(character not in "0123456789abcdef" for character in checksum):
        raise SystemExit("GPX_SHA256 must be a lowercase SHA-256 digest")
    if not storage_key.startswith("gpx/") or ".." in storage_key.split("/"):
        raise SystemExit("GPX_STORAGE_KEY must be a relative gpx/ storage key")
    if len(historical_checksum) != 64 or any(
        character not in "0123456789abcdef" for character in historical_checksum
    ):
        raise SystemExit("GPX_HISTORY_SHA256 must be a lowercase SHA-256 digest")
    if not historical_storage_key.startswith("gpx/") or ".." in historical_storage_key.split("/"):
        raise SystemExit("GPX_HISTORY_STORAGE_KEY must be a relative gpx/ storage key")

    now = datetime.now(UTC)
    with transaction.atomic():
        author = ForumAuthor.objects.create(
            username="restore-drill-rider",
            external_id="restore-drill-author",
            profile_url="https://bikeforum.example/users/restore-drill-rider",
        )
        thread = ForumThread.objects.create(
            external_id="restore-drill-thread",
            url="https://bikeforum.example/threads/restore-drill",
            title="Restore drill ridge loop",
            locality="Brno",
        )
        post = ForumPost.objects.create(
            thread=thread,
            author=author,
            external_id="restore-drill-post",
            url="https://bikeforum.example/threads/restore-drill#post-1",
            posted_at=now,
        )
        route = Route.objects.create(
            slug="restore-drill-ridge-loop",
            lifecycle=RouteLifecycle.PUBLISHED,
            original_source_title="Restore drill ridge loop",
            thread_title=thread.title,
            generated_title="Restore drill ridge loop",
            display_title="Restore drill ridge loop",
            title_provenance=TitleProvenance.GEOGRAPHIC,
        )
        source = RouteSource.objects.create(
            route=route,
            mapy_url="https://mapy.com/s/restore-drill-ridge-loop",
            source_title="Restore drill source",
            processing_status=ProcessingStatus.VALID,
            source_status=SourceStatus.AVAILABLE,
            processed_at=now,
        )
        RouteSourcePost.objects.create(source=source, post=post, is_primary=True)
        RouteVersion.objects.create(
            source=source,
            version_number=1,
            checksum=historical_checksum,
            original_gpx_storage_key=historical_storage_key,
            normalized_geometry=GEOSGeometry(
                "LINESTRING (16.5700 49.1700, 16.5800 49.1750)", srid=4326
            ),
            simplified_geometry=GEOSGeometry(
                "LINESTRING (16.5700 49.1700, 16.5800 49.1750)", srid=4326
            ),
            distance_m="1100.00",
            ascent_m="30.00",
            descent_m="10.00",
            elevation_profile=[{"distance_m": 0.0, "elevation_m": 180.0}],
            loop_status=LoopStatus.POINT_TO_POINT,
            technical_status=ProcessingStatus.VALID,
        )
        version = RouteVersion.objects.create(
            source=source,
            version_number=2,
            checksum=checksum,
            original_gpx_storage_key=storage_key,
            normalized_geometry=GEOSGeometry(
                "LINESTRING (16.6000 49.1900, 16.6200 49.2000)", srid=4326
            ),
            simplified_geometry=GEOSGeometry(
                "LINESTRING (16.6000 49.1900, 16.6200 49.2000)", srid=4326
            ),
            distance_m="2400.00",
            ascent_m="80.00",
            descent_m="75.00",
            elevation_profile=[{"distance_m": 0.0, "elevation_m": 240.0}],
            loop_status=LoopStatus.LOOP,
            technical_status=ProcessingStatus.VALID,
            approved_at=now,
        )
        route.current_approved_version = version
        route.save(update_fields=["current_approved_version", "updated_at"])

        category = Category.objects.create(
            name="Restore drill routes",
            slug="restore-drill-routes",
            description="Synthetic recovery-drill catalogue data.",
        )
        RouteCategory.objects.create(route=route, category=category)
        ModerationDecision.objects.create(
            route=route,
            version=version,
            action=ModerationDecision.Action.REVIEW,
            reason="Synthetic recovery-drill fixture",
            metadata={
                "technical_validity": True,
                "source_context": True,
                "content_suitability": True,
            },
        )
        CrawlTask.objects.create(
            kind=CrawlTask.Kind.INCREMENTAL,
            status=CrawlTaskStatus.COMPLETED,
            start_url="https://bikeforum.example/threads/restore-drill",
            stream="incremental",
            max_pages=1,
            pages_processed=1,
            items_imported=1,
            started_at=now,
            finished_at=now,
        )
        CrawlCheckpoint.objects.create(
            stream="incremental",
            next_url="https://bikeforum.example/threads/restore-drill",
            page_number=1,
            last_successful_at=now,
        )
        # Include durable ingestion state so the snapshot exercises more than
        # the public catalogue tables.
        AnalyticsCounter.objects.create(
            day=now.date(), event=AnalyticsCounter.Event.ROUTE_DETAIL_VIEW, count=1
        )

    print(f"restore_drill_route={route.pk} gpx_key={storage_key} gpx_sha256={checksum}")


if __name__ == "__main__":
    main()
