from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import override_settings

from apps.catalogue.models import ForumPost, ForumThread, Route, RouteSource
from apps.ingestion.dispatch import dispatch_source_extraction, reconcile_extraction_queue
from apps.ingestion.gpx import extract_gpx
from apps.ingestion.models import ExtractionAttempt, ExtractionStatus

pytestmark = pytest.mark.django_db

GPX = b"""<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>
<trkpt lat="49.0" lon="16.0"/><trkpt lat="49.001" lon="16.001"/>
</trkseg></trk></gpx>"""


def make_source() -> RouteSource:
    thread = ForumThread.objects.create(url="https://forum.test/thread", title="Ride")
    post = ForumPost.objects.create(thread=thread, url="https://forum.test/thread#1")
    route = Route.objects.create()
    source = RouteSource.objects.create(route=route, mapy_url="https://mapy.com/s/dispatch")
    source.posts.add(post)
    return source


@override_settings(
    GPX_EXTRACTION_ENABLED=True,
    GPX_PROVIDER_AUTHORIZED=True,
    GPX_LEGAL_APPROVED=True,
)
def test_dispatch_is_created_after_commit_and_repeated_calls_are_idempotent(
    django_capture_on_commit_callbacks: Any,
) -> None:
    source = make_source()
    with patch("apps.ingestion.tasks.extract_gpx_route.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with transaction.atomic():
                first = dispatch_source_extraction(source.pk)
                assert first is not None
                assert ExtractionAttempt.objects.get().dispatched_at is None
        assert len(callbacks) == 1
        second = dispatch_source_extraction(source.pk)

    assert first is not None and second is not None
    assert first.pk == second.pk
    assert ExtractionAttempt.objects.count() == 1
    delay.assert_called_once_with(source.pk, first.pk)
    assert first.__class__.objects.get(pk=first.pk).dispatched_at is not None


@override_settings(
    GPX_EXTRACTION_ENABLED=True,
    GPX_PROVIDER_AUTHORIZED=True,
    GPX_LEGAL_APPROVED=True,
)
def test_rollback_cannot_leave_a_dispatched_attempt() -> None:
    source = make_source()
    with patch("apps.ingestion.tasks.extract_gpx_route.delay") as delay:
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                dispatch_source_extraction(source.pk)
                raise RuntimeError("crawl page rolled back")

    assert not ExtractionAttempt.objects.exists()
    delay.assert_not_called()


@override_settings(
    GPX_EXTRACTION_ENABLED=True,
    GPX_PROVIDER_AUTHORIZED=True,
    GPX_LEGAL_APPROVED=True,
)
def test_reconciliation_dispatches_an_undispatched_attempt_once() -> None:
    source = make_source()
    with patch("apps.ingestion.tasks.extract_gpx_route.delay") as delay:
        with transaction.atomic():
            attempt = ExtractionAttempt.objects.create(
                source=source,
                source_url=source.mapy_url,
                status=ExtractionStatus.QUEUED,
            )
        result = reconcile_extraction_queue(limit=1)
        repeated = reconcile_extraction_queue(limit=1)

    assert result["dispatched"] == 1
    assert repeated["dispatched"] == 0
    delay.assert_called_once_with(source.pk, attempt.pk)


@override_settings(
    GPX_EXTRACTION_ENABLED=True,
    GPX_PROVIDER_AUTHORIZED=True,
    GPX_LEGAL_APPROVED=True,
)
def test_queued_attempt_reuses_extraction_retry_history() -> None:
    source = make_source()
    attempt = ExtractionAttempt.objects.create(
        source=source,
        source_url=source.mapy_url,
        status=ExtractionStatus.QUEUED,
        attempt_number=2,
    )
    result = extract_gpx(source.pk, attempt_id=attempt.pk, adapter=lambda _: GPX)
    attempt.refresh_from_db()

    assert result["status"] == ExtractionStatus.SUCCEEDED
    assert result["attempt_id"] == attempt.pk
    assert attempt.status == ExtractionStatus.SUCCEEDED
    assert ExtractionAttempt.objects.values_list("attempt_number", flat=True).get() == 2
