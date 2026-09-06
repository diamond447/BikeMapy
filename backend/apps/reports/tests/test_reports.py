# mypy: disable-error-code="import-untyped"

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, cast
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.cache.backends.redis import RedisCache
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from apps.catalogue.models import Route
from apps.reports.models import Report, ReportAudit
from apps.reports.services import (
    DuplicateReport,
    HoneypotTriggered,
    RateLimited,
    TurnstileRejected,
    check_rate_limits,
    client_ip,
    decide_report,
    rate_limit_identifier,
    retain_closed_reports,
    submit_report,
)
from apps.reports.turnstile import TurnstileVerifier

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def clear_report_cache() -> None:
    cache.clear()


@pytest.fixture
def route() -> Route:
    return Route.objects.create(display_title="Test route")


def request(ip: str = "203.0.113.20", *, cloudflare_ip: str | None = None) -> Any:
    kwargs: dict[str, Any] = {"REMOTE_ADDR": ip}
    if cloudflare_ip is not None:
        kwargs["HTTP_CF_CONNECTING_IP"] = cloudflare_ip
    return RequestFactory().post("/", **kwargs)


def submit(route: Route, *, ip: str = "203.0.113.20", **kwargs: str) -> Report:
    with patch("apps.reports.services.verify_turnstile", return_value=True):
        return submit_report(
            route,
            reason=kwargs.pop("reason", Report.Reason.AUTHOR_REMOVAL),
            message=kwargs.pop("message", "Please review this route attribution."),
            contact_email=kwargs.pop("contact_email", "reporter@example.test"),
            turnstile_token="test-token",
            request=request(ip),
            **kwargs,
        )


def test_valid_report_is_queued_and_does_not_change_route(route: Route) -> None:
    before = (route.lifecycle, route.current_approved_version_id)
    report = submit(route)
    route.refresh_from_db()
    assert report.status == Report.Status.PENDING
    assert report.reason == Report.Reason.AUTHOR_REMOVAL
    assert before == (route.lifecycle, route.current_approved_version_id)
    assert not any(field.name in {"ip_address", "raw_ip"} for field in Report._meta.fields)
    assert ReportAudit.objects.get(report=report).event == "submitted"


def test_turnstile_honeypot_and_duplicate_are_independent(route: Route) -> None:
    with patch("apps.reports.services.verify_turnstile", return_value=False):
        with pytest.raises(TurnstileRejected):
            submit_report(
                route,
                reason="other",
                message="A sufficiently long report.",
                turnstile_token="bad",
                request=request(),
            )
    with patch("apps.reports.services.verify_turnstile") as verify:
        with pytest.raises(HoneypotTriggered):
            submit_report(
                route,
                reason="other",
                message="A sufficiently long report.",
                turnstile_token="ok",
                honeypot="bot",
                request=request("203.0.113.21"),
            )
        verify.assert_not_called()
    submit(route, ip="203.0.113.22", reason="other", message="A sufficiently long report.")
    with pytest.raises(DuplicateReport):
        submit(route, ip="203.0.113.23", reason="other", message="A sufficiently long report.")


def test_rate_limits_use_hmac_cache_keys_and_expire_windows(route: Route) -> None:
    cache.clear()
    with (
        override_settings(REPORT_RATE_LIMIT_HOURLY=2, REPORT_RATE_LIMIT_DAILY=4),
        patch.object(cache, "add", wraps=cache.add) as cache_add,
    ):
        assert check_rate_limits("203.0.113.30").allowed
        assert check_rate_limits("203.0.113.30").allowed
        limited = check_rate_limits("203.0.113.30")
    assert not limited.allowed
    assert rate_limit_identifier("203.0.113.30") not in "203.0.113.30"
    cache_keys = [str(call.args[0]) for call in cache_add.call_args_list]
    assert cache_keys
    assert all("203.0.113.30" not in key for key in cache_keys)
    assert all(rate_limit_identifier("203.0.113.30") in key for key in cache_keys)


def test_daily_limit_is_independent_from_hourly_limit() -> None:
    with override_settings(REPORT_RATE_LIMIT_HOURLY=10, REPORT_RATE_LIMIT_DAILY=2):
        assert check_rate_limits("203.0.113.31").allowed
        assert check_rate_limits("203.0.113.31").allowed
        assert not check_rate_limits("203.0.113.31").allowed


def test_turnstile_adapter_is_testable_and_validates_provider_response() -> None:
    response = Mock()
    response.json.return_value = {"success": True}
    with (
        override_settings(REPORT_TURNSTILE_SECRET_KEY="secret"),
        patch("apps.reports.turnstile.httpx.post", return_value=response) as post,
    ):
        assert TurnstileVerifier().verify("token", remote_ip="203.0.113.40")
    post.assert_called_once()
    assert post.call_args.kwargs["data"]["response"] == "token"
    assert post.call_args.kwargs["data"]["remoteip"] == "203.0.113.40"


def test_client_ip_requires_explicit_trusted_cloudflare_peer() -> None:
    with override_settings(
        REPORT_CLIENT_IP_MODE="cloudflare", REPORT_TRUSTED_PROXY_CIDRS=("192.0.2.0/24",)
    ):
        assert client_ip(request("192.0.2.10", cloudflare_ip="198.51.100.4")) == "198.51.100.4"
        assert client_ip(request("198.51.100.10", cloudflare_ip="198.51.100.4")) == "198.51.100.10"
        assert (
            client_ip(request("192.0.2.10", cloudflare_ip="198.51.100.4, 198.51.100.5"))
            == "unknown"
        )
        assert client_ip(request("192.0.2.10", cloudflare_ip="not-an-ip")) == "unknown"
    assert client_ip(request("2001:db8::10")) == "2001:db8::10"


def test_rate_limiter_fails_closed_when_cache_is_unavailable() -> None:
    with patch("apps.reports.services.cache.add", side_effect=RuntimeError("Redis unavailable")):
        with pytest.raises(RateLimited):
            check_rate_limits("203.0.113.50")


@pytest.mark.parametrize(
    ("secret", "token", "response", "exception"),
    [
        ("", "token", None, None),
        ("secret", "", None, None),
        ("secret", "token", {"success": False}, None),
        ("secret", "token", None, RuntimeError("network")),
    ],
)
def test_turnstile_adapter_fails_closed(
    secret: str, token: str, response: dict[str, Any] | None, exception: Exception | None
) -> None:
    with override_settings(REPORT_TURNSTILE_SECRET_KEY=secret):
        with patch("apps.reports.turnstile.httpx.post") as post:
            if exception:
                post.side_effect = exception
            else:
                provider_response = Mock()
                provider_response.json.return_value = response
                post.return_value = provider_response
            assert not TurnstileVerifier().verify(token)
            if not secret or not token:
                post.assert_not_called()


def test_turnstile_adapter_fails_closed_for_http_and_invalid_json() -> None:
    import httpx

    http_error = Mock()
    http_error.raise_for_status.side_effect = httpx.HTTPStatusError(
        "bad", request=Mock(), response=Mock()
    )
    invalid_json = Mock()
    invalid_json.json.side_effect = ValueError("invalid")
    with override_settings(REPORT_TURNSTILE_SECRET_KEY="secret"):
        with patch("apps.reports.turnstile.httpx.post", return_value=http_error):
            assert not TurnstileVerifier().verify("token")
        with patch("apps.reports.turnstile.httpx.post", return_value=invalid_json):
            assert not TurnstileVerifier().verify("token")


@pytest.mark.redis
@pytest.mark.skipif(os.getenv("RUN_REDIS_TEST") != "1", reason="opt-in Redis integration test")
def test_redis_limiter_is_atomic_and_keeps_a_24_hour_ttl() -> None:
    location = str(settings.CACHES["default"].get("LOCATION", ""))
    if not location.startswith(("redis://", "rediss://")):
        pytest.skip("Redis cache is not configured")
    first = RedisCache(location, {})
    second = RedisCache(location, {})
    ip = f"203.0.113.{uuid4().int % 200 + 1}"
    with override_settings(REPORT_RATE_LIMIT_HOURLY=100, REPORT_RATE_LIMIT_DAILY=100):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda backend: check_rate_limits(ip, cache_backend=backend),
                    [first, second] * 4,
                )
            )
    assert all(result.allowed for result in results)
    identifier = rate_limit_identifier(ip)
    hour_key = first.make_key(f"reports:rate:v1:{identifier}:hour")
    day_key = first.make_key(f"reports:rate:v1:{identifier}:day")
    client = cast(Any, first)._cache.get_client(write=True)
    assert int(client.get(hour_key)) == 8
    assert int(client.get(day_key)) == 8
    hour_ttl = int(client.ttl(hour_key))
    day_ttl = int(client.ttl(day_key))
    assert 0 < hour_ttl <= 3600
    assert 0 < day_ttl <= 86400
    client.delete(hour_key, day_key)


def test_decision_is_explicit_and_audited_without_route_action(route: Route) -> None:
    report = submit(route)
    user = get_user_model().objects.create_user(username="owner")
    decide_report(
        report, decision=Report.Decision.REJECT, reason="Evidence was not sufficient.", actor=user
    )
    report.refresh_from_db()
    route.refresh_from_db()
    assert report.status == Report.Status.REJECTED
    assert report.closed_at is not None
    assert route.lifecycle == "published"
    assert ReportAudit.objects.filter(report=report, event="decision", actor=user).exists()


def test_retention_is_idempotent_and_auditable(route: Route) -> None:
    report = submit(route)
    now = timezone.now()
    report.closed_at = now - timedelta(days=400)
    report.save(update_fields=["closed_at"])
    first = retain_closed_reports(now=now)
    second = retain_closed_reports(now=now)
    report.refresh_from_db()
    assert first == {"emails_removed": 1, "reports_anonymized": 1}
    assert second == {"emails_removed": 0, "reports_anonymized": 0}
    assert not report.contact_email
    assert report.message.startswith("[Personal details")
    assert (
        ReportAudit.objects.filter(report=report, event="personal_details_anonymized").count() == 1
    )


def test_retention_removes_email_after_90_days_but_keeps_message(route: Route) -> None:
    report = submit(route, contact_email="keep-message@example.test")
    now = timezone.now()
    report.closed_at = now - timedelta(days=100)
    report.save(update_fields=["closed_at"])
    retain_closed_reports(now=now)
    report.refresh_from_db()
    assert report.contact_email == ""
    assert report.message == "Please review this route attribution."
    assert report.personal_details_anonymized_at is None


def test_public_endpoint_validates_and_returns_protection_outcomes(route: Route) -> None:
    client = Client()
    payload = {
        "reason": "rights_holder",
        "message": "Please review these publication rights.",
        "turnstile_token": "token",
    }
    with (
        patch("apps.reports.api.public_route_queryset", return_value=Route.objects.all()),
        patch("apps.reports.services.verify_turnstile", return_value=True),
    ):
        response = client.post(f"/api/v1/routes/{route.pk}/reports/", payload)
    assert response.status_code == 201
    assert response.json()["status"] == "received"

    with (
        patch("apps.reports.api.public_route_queryset", return_value=Route.objects.all()),
        patch("apps.reports.services.verify_turnstile", return_value=True),
    ):
        duplicate = client.post(f"/api/v1/routes/{route.pk}/reports/", payload)
    assert duplicate.status_code == 409


def test_owner_report_admin_requires_github_and_can_decide(route: Route) -> None:
    report = submit(route)
    user = get_user_model().objects.create_user(username="owner")
    SocialAccount.objects.create(user=user, provider="github", uid="12345")
    client = Client()
    client.force_login(user)
    session = client.session
    session["account_authentication_methods"] = [
        {"method": "socialaccount", "provider": "github", "uid": "12345"}
    ]
    session.save()
    with override_settings(GITHUB_OWNER_IDS=frozenset({"12345"})):
        response = client.post(
            f"/admin/reports/report/{report.pk}/review/",
            {"decision": "reject", "reason": "Not supported by the source."},
        )
    assert response.status_code == 302
    assert Report.objects.get(pk=report.pk).status == Report.Status.REJECTED
