from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from apps.ingestion.models import CrawlCheckpoint, CrawlTask, CrawlTaskStatus
from apps.ingestion.tasks import worker_smoke


def test_live_endpoint() -> None:
    response = Client().get("/health/live/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_worker_smoke_task() -> None:
    assert worker_smoke.apply().get() == "worker-ready"


def test_api_root() -> None:
    response = Client().get("/api/v1/")
    assert response.status_code == 200
    assert response.json()["version"] == "v1"


def test_ready_endpoint_with_database() -> None:
    with patch("apps.api.views.connection.cursor") as cursor:
        response = Client().get("/health/ready/")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    cursor.assert_called_once_with()


def test_ready_endpoint_without_database() -> None:
    with patch("apps.api.views.connection.cursor", side_effect=RuntimeError("offline")):
        response = Client().get("/health/ready/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


def test_live_endpoint_does_not_check_dependencies() -> None:
    with patch("apps.api.views.connection.cursor", side_effect=AssertionError):
        response = Client().get("/health/live/")
    assert response.status_code == 200


def test_ready_endpoint_requires_cache() -> None:
    with patch("apps.api.views.cache.set", side_effect=RuntimeError("offline")):
        response = Client().get("/health/ready/")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.django_db
def test_crawler_health_reports_freshness() -> None:
    CrawlCheckpoint.objects.create(
        stream="incremental", last_successful_at=timezone.now() - timedelta(minutes=5)
    )
    with override_settings(CRAWLER_FRESHNESS_MAX_AGE=600):
        response = Client().get("/health/crawler/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.django_db
def test_crawler_health_surfaces_missing_checkpoint() -> None:
    response = Client().get("/health/crawler/")
    assert response.status_code == 503
    assert response.json() == {"status": "stale", "stream": "incremental"}


@pytest.mark.django_db
def test_crawler_health_surfaces_failed_latest_run() -> None:
    CrawlCheckpoint.objects.create(stream="incremental", last_successful_at=timezone.now())
    CrawlTask.objects.create(
        kind=CrawlTask.Kind.INCREMENTAL,
        status=CrawlTaskStatus.FAILED,
        start_url="https://www.bike-forum.cz/forum/",
    )
    response = Client().get("/health/crawler/")
    assert response.status_code == 503
    assert response.json()["latest_task_status"] == CrawlTaskStatus.FAILED


@pytest.mark.django_db
def test_crawler_health_treats_bounded_pause_as_healthy() -> None:
    CrawlCheckpoint.objects.create(stream="incremental", last_successful_at=timezone.now())
    CrawlTask.objects.create(
        kind=CrawlTask.Kind.INCREMENTAL,
        status=CrawlTaskStatus.PAUSED,
        start_url="https://www.bike-forum.cz/forum/",
    )
    response = Client().get("/health/crawler/")
    assert response.status_code == 200
    assert response.json()["latest_task_status"] == CrawlTaskStatus.PAUSED
