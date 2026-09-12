"""Tests for proxy-boundary normalization middleware."""

from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, override_settings

from config.client_identity import client_ip
from config.middleware import TrustedProxyClientIdentityMiddleware


def test_middleware_normalizes_trusted_cloudflare_identity_and_removes_xff() -> None:
    request = RequestFactory().get(
        "/",
        REMOTE_ADDR="172.30.0.2",
        HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.10",
        HTTP_CF_CONNECTING_IP="203.0.113.10",
    )

    def response(received: HttpRequest) -> HttpResponse:
        assert received.META["REMOTE_ADDR"] == "203.0.113.10"
        assert "HTTP_X_FORWARDED_FOR" not in received.META
        return HttpResponse()

    with override_settings(
        REPORT_CLIENT_IP_MODE="cloudflare", REPORT_TRUSTED_PROXY_CIDRS=("172.30.0.2/32",)
    ):
        TrustedProxyClientIdentityMiddleware(response)(request)


def test_direct_mode_ignores_malformed_forwarded_headers() -> None:
    request = RequestFactory().get(
        "/", REMOTE_ADDR="203.0.113.10", HTTP_X_FORWARDED_FOR="not-an-ip"
    )
    with override_settings(REPORT_CLIENT_IP_MODE="direct"):
        assert client_ip(request) == "203.0.113.10"
