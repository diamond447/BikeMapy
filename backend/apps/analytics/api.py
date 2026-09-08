"""Stateless public endpoint for privacy-minimal aggregate counters."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

import re

from django.conf import settings
from django.db.models import F
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AnalyticsCounter
from .serializers import AnalyticsEventSerializer
from .throttling import AnalyticsEventThrottle


def origin_is_allowed(request: Request) -> bool:
    """Accept configured browser origins, plus requests with no Origin header.

    Same-origin beacon requests and non-browser operators may omit Origin. A
    supplied Origin must match the configured CORS allow-list (including the
    explicit preview regexes); this is origin separation, not authentication.
    """

    origin = request.headers.get("Origin")
    if not origin:
        return True
    if origin in getattr(settings, "CORS_ALLOWED_ORIGINS", ()):
        return True
    return any(
        re.match(str(pattern), origin) is not None
        for pattern in getattr(settings, "CORS_ALLOWED_ORIGIN_REGEXES", ())
    )


@method_decorator(csrf_exempt, name="dispatch")
class AnalyticsEventView(APIView):
    """Record one allow-listed event without retaining request identifiers.

    This endpoint is CSRF-exempt because it has no authenticated session and
    its only side effect is an anonymous product counter.  The dedicated
    global non-IP throttle still bounds accidental or automated bursts.
    """

    throttle_classes = [AnalyticsEventThrottle]

    @extend_schema(
        request=AnalyticsEventSerializer,
        responses={202: OpenApiResponse(description="Event accepted for aggregation.")},
        tags=["analytics"],
    )
    def post(self, request: Request) -> Response:
        if not origin_is_allowed(request):
            return Response({"detail": "Analytics origin is not allowed."}, status=403)
        serializer = AnalyticsEventSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        event = str(serializer.validated_data["event"])
        counter, _ = AnalyticsCounter.objects.get_or_create(
            day=timezone.localdate(), event=event, defaults={"count": 0}
        )
        AnalyticsCounter.objects.filter(pk=counter.pk).update(count=F("count") + 1)
        return Response(status=status.HTTP_202_ACCEPTED)
