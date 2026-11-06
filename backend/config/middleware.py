"""Deployment-boundary middleware for Cloudflare Pages previews."""

from __future__ import annotations

import re
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse

from .client_identity import client_ip


class TrustedProxyClientIdentityMiddleware:
    """Set ``REMOTE_ADDR`` from the trusted proxy header and drop forwarded chains."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.META["REMOTE_ADDR"] = client_ip(request)
        request.META.pop("HTTP_X_FORWARDED_FOR", None)
        return self.get_response(request)


class PreviewReadOnlyMiddleware:
    """Reject state-changing requests from Pages preview origins.

    The regex must never be broadened to all of ``pages.dev``.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response
        self.origin_pattern = re.compile(settings.READ_ONLY_PREVIEW_ORIGIN_REGEX)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        origin = request.headers.get("Origin", "").rstrip("/")
        is_preview = bool(origin and self.origin_pattern.fullmatch(origin))
        admin_request = request.path == "/admin" or request.path.startswith("/admin/")
        unsafe_request = request.method not in {"GET", "HEAD", "OPTIONS"}
        if is_preview and (admin_request or unsafe_request):
            return JsonResponse(
                {"detail": "This preview is read-only."},
                status=403,
            )
        return self.get_response(request)
