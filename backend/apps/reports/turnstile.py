"""Cloudflare Turnstile verification behind a small testable adapter."""

from __future__ import annotations

from typing import Any

import httpx
from django.conf import settings


class TurnstileVerifier:
    endpoint = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    def verify(self, token: str, *, remote_ip: str = "") -> bool:
        if not token.strip() or not getattr(settings, "REPORT_TURNSTILE_SECRET_KEY", ""):
            return False
        try:
            response = httpx.post(
                getattr(settings, "REPORT_TURNSTILE_VERIFY_URL", self.endpoint),
                data={
                    "secret": settings.REPORT_TURNSTILE_SECRET_KEY,
                    "response": token,
                    **({"remoteip": remote_ip} if remote_ip else {}),
                },
                timeout=float(getattr(settings, "REPORT_TURNSTILE_TIMEOUT", 5)),
            )
            response.raise_for_status()
            payload: Any = response.json()
        except Exception:
            # Verification is security-critical: provider/network failures
            # must reject the submission rather than degrade to an allow.
            return False
        return bool(isinstance(payload, dict) and payload.get("success") is True)
