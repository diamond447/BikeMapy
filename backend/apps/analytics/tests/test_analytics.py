"""Tests for the intentionally small anonymous analytics contract."""

# mypy: disable-error-code="import-untyped,no-untyped-call"

from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from apps.analytics.models import AnalyticsCounter
from apps.analytics.throttling import AnalyticsEventThrottle

pytestmark = pytest.mark.django_db


def test_allow_listed_event_increments_only_day_bucket() -> None:
    response = Client().post(
        "/api/v1/analytics/events/",
        data={"event": AnalyticsCounter.Event.ROUTE_DETAIL_VIEW},
        content_type="application/json",
    )

    assert response.status_code == 202
    assert not response.content
    assert list(AnalyticsCounter.objects.values("day", "event", "count")) == [
        {
            "day": timezone.localdate(),
            "event": AnalyticsCounter.Event.ROUTE_DETAIL_VIEW,
            "count": 1,
        }
    ]

    Client().post(
        "/api/v1/analytics/events/",
        data={"event": AnalyticsCounter.Event.ROUTE_DETAIL_VIEW},
        content_type="application/json",
    )
    assert AnalyticsCounter.objects.get().count == 2


@pytest.mark.parametrize("event", [choice for choice, _ in AnalyticsCounter.Event.choices])
def test_each_public_event_is_supported(event: str) -> None:
    response = Client().post(
        "/api/v1/analytics/events/",
        data={"event": event},
        content_type="application/json",
    )

    assert response.status_code == 202
    assert AnalyticsCounter.objects.filter(event=event, day=timezone.localdate()).exists()


def test_url_encoded_beacon_payload_is_supported() -> None:
    response = Client().post(
        "/api/v1/analytics/events/",
        data="event=original_source_click",
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 202
    assert AnalyticsCounter.objects.get().event == AnalyticsCounter.Event.ORIGINAL_SOURCE_CLICK


def test_unknown_event_and_metadata_are_rejected_without_writing() -> None:
    response = Client().post(
        "/api/v1/analytics/events/",
        data={"event": "visitor_id", "route_id": "sensitive-route"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert AnalyticsCounter.objects.count() == 0


@pytest.mark.parametrize("payload", [[], "route_detail_view", None])
def test_non_object_payload_is_rejected_without_server_error(payload: object) -> None:
    response = Client().post(
        "/api/v1/analytics/events/",
        data=payload,
        content_type="application/json",
    )

    assert response.status_code == 400
    assert AnalyticsCounter.objects.count() == 0


def test_configured_origins_are_required_when_origin_is_supplied() -> None:
    client = Client()
    with override_settings(
        CORS_ALLOWED_ORIGINS=["https://bikemapy.example"], CORS_ALLOWED_ORIGIN_REGEXES=[]
    ):
        allowed = client.post(
            "/api/v1/analytics/events/",
            data={"event": AnalyticsCounter.Event.ROUTE_DETAIL_VIEW},
            content_type="application/json",
            HTTP_ORIGIN="https://bikemapy.example",
        )
        rejected = client.post(
            "/api/v1/analytics/events/",
            data={"event": AnalyticsCounter.Event.ROUTE_DETAIL_VIEW},
            content_type="application/json",
            HTTP_ORIGIN="https://attacker.example",
        )
    assert allowed.status_code == 202
    assert rejected.status_code == 403
    assert AnalyticsCounter.objects.get().count == 1


def test_missing_origin_is_allowed_for_same_origin_and_non_browser_calls() -> None:
    response = Client().post(
        "/api/v1/analytics/events/",
        data={"event": AnalyticsCounter.Event.ORIGINAL_SOURCE_CLICK},
        content_type="application/json",
    )

    assert response.status_code == 202


def test_throttle_cache_key_contains_no_request_ip() -> None:
    throttle = AnalyticsEventThrottle()
    request = RequestFactory().get("/")
    request.META["REMOTE_ADDR"] = "203.0.113.99"
    key = throttle.get_cache_key(request, None)
    assert key is not None
    assert "203.0.113.99" not in key
    assert key.endswith("global")
    with override_settings(ANALYTICS_EVENT_RATE="1/minute"):
        cache.clear()
        client = Client()
        with patch.object(cache, "set", wraps=cache.set) as cache_set:
            first = client.post(
                "/api/v1/analytics/events/",
                data={"event": AnalyticsCounter.Event.ROUTE_DETAIL_VIEW},
                content_type="application/json",
                REMOTE_ADDR="203.0.113.99",
            )
            second = client.post(
                "/api/v1/analytics/events/",
                data={"event": AnalyticsCounter.Event.ROUTE_DETAIL_VIEW},
                content_type="application/json",
                REMOTE_ADDR="198.51.100.20",
            )
    assert first.status_code == 202
    assert second.status_code == 429
    cache_keys = [str(call.args[0]) for call in cache_set.call_args_list]
    assert cache_keys
    assert all("203.0.113.99" not in key for key in cache_keys)
    assert all("198.51.100.20" not in key for key in cache_keys)


def test_known_event_with_metadata_is_rejected_without_writing() -> None:
    response = Client().post(
        "/api/v1/analytics/events/",
        data={"event": AnalyticsCounter.Event.ROUTE_DETAIL_VIEW, "route_id": "not-collected"},
        content_type="application/json",
    )

    assert response.status_code == 400
    assert AnalyticsCounter.objects.count() == 0


def test_event_endpoint_is_csrf_independent_but_does_not_expose_counts() -> None:
    response = Client(enforce_csrf_checks=True).post(
        "/api/v1/analytics/events/",
        data={"event": AnalyticsCounter.Event.ORIGINAL_SOURCE_CLICK},
        content_type="application/json",
    )

    assert response.status_code == 202
    assert not response.content
