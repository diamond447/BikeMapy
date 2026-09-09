from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.db import transaction
from django.test import Client, override_settings
from django.utils import timezone

from apps.catalogue.models import ForumPost, ForumThread, Route, RouteSource
from apps.ingestion.crawler import (
    FetchedPage,
    PageFetcher,
    SourceChecker,
    SourceCheckResult,
    run_crawl,
)
from apps.ingestion.dispatch import dispatch_source_extraction, reconcile_extraction_queue
from apps.ingestion.gpx import extract_gpx
from apps.ingestion.models import ExtractionAttempt, ExtractionStatus
from apps.ingestion.tasks import extract_gpx_route

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
    GPX_DISPATCH_TIMEOUT=60,
)
def test_dispatch_claim_prevents_a_concurrent_sender_from_publishing_twice() -> None:
    source = make_source()
    attempt = ExtractionAttempt.objects.create(
        source=source,
        source_url=source.mapy_url,
        status=ExtractionStatus.QUEUED,
        dispatch_claim_token="already-claimed",
        dispatch_claimed_until=timezone.now() + timedelta(minutes=1),
    )
    with patch("apps.ingestion.tasks.extract_gpx_route.delay") as delay:
        from apps.ingestion.dispatch import _send_attempt

        _send_attempt(attempt.pk)
    delay.assert_not_called()


class SyntheticFetcher(PageFetcher):
    def __init__(self, body: str) -> None:
        self.body = body

    def fetch(self, url: str) -> FetchedPage:
        return FetchedPage(url=url, body=self.body)

    def close(self) -> None:
        return None


class SyntheticSourceChecker(SourceChecker):
    def check(self, url: str) -> SourceCheckResult:
        return SourceCheckResult(available=True, status_code=200)


@override_settings(
    GPX_EXTRACTION_ENABLED=True,
    GPX_PROVIDER_AUTHORIZED=True,
    GPX_LEGAL_APPROVED=True,
    BIKEFORUM_ALLOWED_ORIGINS=["https://bikeforum.example"],
    BIKEFORUM_DNS_CHECK=False,
)
def test_synthetic_crawl_extracts_and_publishes_a_public_api_route(
    django_capture_on_commit_callbacks: Any,
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "thread.html"
    source_url = "https://bikeforum.example/t/42"

    def dispatch(source_id: int, attempt_id: int) -> dict[str, Any]:
        return extract_gpx(source_id, attempt_id=attempt_id, adapter=lambda _: GPX)

    with override_settings(MEDIA_ROOT=tmp_path):
        with patch("apps.ingestion.tasks.extract_gpx_route.delay", side_effect=dispatch):
            with django_capture_on_commit_callbacks(execute=True):
                result = run_crawl(
                    start_url=source_url,
                    max_pages=1,
                    stream="synthetic-extraction-pipeline",
                    fetcher=SyntheticFetcher(fixture.read_text()),
                    source_checker=SyntheticSourceChecker(),
                )

        route_source = RouteSource.objects.get()
        attempt = ExtractionAttempt.objects.get()
        client = Client()
        response = client.get("/api/v1/routes/")

    assert result["sources"] == 1
    assert route_source.versions.count() == 1
    assert attempt.status == ExtractionStatus.SUCCEEDED
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["results"][0]["id"] == str(route_source.route_id)


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


@override_settings(
    GPX_EXTRACTION_ENABLED=True,
    GPX_PROVIDER_AUTHORIZED=False,
    GPX_LEGAL_APPROVED=True,
)
def test_execution_gate_blocks_queued_celery_work_without_provider_access() -> None:
    source = make_source()
    attempt = ExtractionAttempt.objects.create(
        source=source,
        source_url=source.mapy_url,
        status=ExtractionStatus.QUEUED,
    )
    with patch("apps.ingestion.gpx._export") as export:
        result = extract_gpx_route.apply(args=[source.pk, attempt.pk]).get()

    attempt.refresh_from_db()
    source.refresh_from_db()
    assert result["status"] == ExtractionStatus.BLOCKED
    assert attempt.status == ExtractionStatus.BLOCKED
    assert source.processing_status == "blocked"
    export.assert_not_called()


@override_settings(
    GPX_EXTRACTION_ENABLED=True,
    GPX_PROVIDER_AUTHORIZED=True,
    GPX_LEGAL_APPROVED=True,
)
def test_failed_extraction_is_reconciled_as_a_new_retry(
    django_capture_on_commit_callbacks: Any,
) -> None:
    source = make_source()
    source.processing_status = "failed"
    source.save(update_fields=["processing_status"])
    failed = ExtractionAttempt.objects.create(
        source=source,
        source_url=source.mapy_url,
        status=ExtractionStatus.FAILED,
        attempt_number=1,
        error="temporary provider failure",
        finished_at=timezone.now(),
    )
    with patch("apps.ingestion.tasks.extract_gpx_route.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            result = reconcile_extraction_queue(limit=1)

    retry = ExtractionAttempt.objects.exclude(pk=failed.pk).get()
    assert result["queued"] == 1
    assert retry.attempt_number == 2
    assert retry.status == ExtractionStatus.QUEUED
    delay.assert_called_once_with(source.pk, retry.pk)


@override_settings(
    GPX_EXTRACTION_ENABLED=True,
    GPX_PROVIDER_AUTHORIZED=True,
    GPX_LEGAL_APPROVED=True,
    GPX_PROCESSING_TIMEOUT=1,
)
def test_stale_expiration_fences_late_completion() -> None:
    source = make_source()
    attempt = ExtractionAttempt.objects.create(
        source=source,
        source_url=source.mapy_url,
        status=ExtractionStatus.QUEUED,
    )

    def late_worker(_url: str) -> bytes:
        ExtractionAttempt.objects.filter(pk=attempt.pk).update(
            started_at=timezone.now() - timedelta(minutes=5)
        )
        source.processing_status = "processing"
        source.save(update_fields=["processing_status"])
        reconcile_extraction_queue(limit=1)
        return GPX

    result = extract_gpx(source.pk, attempt_id=attempt.pk, adapter=late_worker)
    attempt.refresh_from_db()
    assert result["status"] == ExtractionStatus.FAILED
    assert attempt.status == ExtractionStatus.FAILED
    assert source.versions.count() == 0
