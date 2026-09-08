"""Deployment-boundary middleware for Cloudflare Pages previews."""

from __future__ import annotations

import re
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse


class PreviewReadOnlyMiddleware:
    """Reject state-changing requests originating from configured previews.

    Pages preview URLs are dynamic, so this check deliberately happens at the
    API boundary instead of relying on a frontend feature flag. The regex is a
    deployment setting and defaults to this project's Pages hostname; it must
    never be broadened to all of ``pages.dev``.
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
