from unittest.mock import patch

from django.test import Client

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
