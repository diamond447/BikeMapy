"""Tests for the API throttle's trusted proxy boundary."""

# mypy: disable-error-code="import-untyped,no-untyped-call"

from typing import Any, cast

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, override_settings

from apps.api.throttling import ApiAnonRateThrottle


def throttle_key(*, peer: str, forwarded: str | None = None, cloudflare: str | None = None) -> str:
    meta: dict[str, str] = {"REMOTE_ADDR": peer}
    if forwarded is not None:
        meta["HTTP_X_FORWARDED_FOR"] = forwarded
    if cloudflare is not None:
        meta["HTTP_CF_CONNECTING_IP"] = cloudflare
    request = cast(Any, RequestFactory().get)("/", **meta)
    request.user = AnonymousUser()
    key = ApiAnonRateThrottle().get_cache_key(request, None)
    assert key is not None
    return str(key)


def test_direct_clients_cannot_change_identity_with_forwarded_prefix() -> None:
    with override_settings(REPORT_CLIENT_IP_MODE="direct"):
        first = throttle_key(peer="203.0.113.10", forwarded="198.51.100.1, 203.0.113.10")
        second = throttle_key(peer="203.0.113.10", forwarded="192.0.2.99, 203.0.113.10")
    assert first == second
    assert "203.0.113.10" not in first


def test_trusted_cloudflare_peer_uses_single_client_identity() -> None:
    with override_settings(
        REPORT_CLIENT_IP_MODE="cloudflare", REPORT_TRUSTED_PROXY_CIDRS=("172.30.0.2/32",)
    ):
        first = throttle_key(
            peer="172.30.0.2",
            forwarded="198.51.100.1, 203.0.113.10",
            cloudflare="203.0.113.10",
        )
        second = throttle_key(
            peer="172.30.0.2",
            forwarded="192.0.2.99, 203.0.113.10",
            cloudflare="203.0.113.10",
        )
        other_client = throttle_key(peer="172.30.0.2", cloudflare="203.0.113.11")
    assert first == second
    assert first != other_client
    assert "203.0.113" not in first


def test_untrusted_and_malformed_forwarded_headers_do_not_impersonate_clients() -> None:
    with override_settings(
        REPORT_CLIENT_IP_MODE="cloudflare", REPORT_TRUSTED_PROXY_CIDRS=("172.30.0.2/32",)
    ):
        untrusted = throttle_key(peer="198.51.100.10", cloudflare="203.0.113.10")
        malformed = throttle_key(peer="172.30.0.2", cloudflare="203.0.113.10, 192.0.2.1")
        unknown = throttle_key(peer="172.30.0.2", cloudflare="not-an-ip")
    assert untrusted != throttle_key(peer="203.0.113.10")
    assert malformed == unknown
