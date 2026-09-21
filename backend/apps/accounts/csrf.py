"""JSON CSRF failures for session-backed game endpoints."""

from __future__ import annotations

from typing import Any

from django.http import JsonResponse


def csrf_failure(request: Any, reason: str = "") -> JsonResponse:
    response = JsonResponse(
        {"detail": "CSRF validation failed.", "code": "csrf_failed"}, status=403
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response
