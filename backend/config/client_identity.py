"""Trusted client identity resolution for proxy-aware protections."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from typing import Any

from django.conf import settings


def _parse_ip(value: Any) -> str | None:
    value = str(value or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip(request: Any) -> str:
    """Return the client address from the configured trusted boundary.

    The application never walks an ``X-Forwarded-For`` chain. In the direct
    deployment mode, ``REMOTE_ADDR`` is the client. In the Cloudflare Tunnel
    mode, only the direct Nginx peer may supply one single valid
    ``CF-Connecting-IP`` value. This makes the policy independent of proxy
    depth and prevents an arbitrary forwarded prefix from becoming an
    identity.
    """

    meta = getattr(request, "META", {})
    peer = _parse_ip(meta.get("REMOTE_ADDR"))
    if not peer:
        return "unknown"

    mode = str(getattr(settings, "REPORT_CLIENT_IP_MODE", "direct")).lower()
    if mode != "cloudflare":
        return peer

    try:
        networks = tuple(
            ipaddress.ip_network(str(cidr).strip(), strict=False)
            for cidr in getattr(settings, "REPORT_TRUSTED_PROXY_CIDRS", ())
            if str(cidr).strip()
        )
    except ValueError:
        networks = ()

    if not any(ipaddress.ip_address(peer) in network for network in networks):
        return peer

    header = meta.get("HTTP_CF_CONNECTING_IP")
    if header is None:
        return peer
    return _parse_ip(header) or "unknown"


def rate_limit_identifier(ip_address: str) -> str:
    """Return an HMAC identifier safe for short-lived throttle cache keys."""

    secret = str(
        getattr(settings, "RATE_LIMIT_HMAC_SECRET", "")
        or getattr(settings, "REPORT_RATE_LIMIT_HMAC_SECRET", "")
        or getattr(settings, "SECRET_KEY", "local-development-key")
    ).encode()
    return hmac.new(secret, ip_address.encode(), hashlib.sha256).hexdigest()
