"""Non-identifying burst protection for public analytics events."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class AnalyticsEventThrottle(SimpleRateThrottle):
    """Use one deliberately coarse bucket instead of a request-IP key.

    The counters are intentionally directional and spoofable. This throttle
    only protects the write path from a burst; it is not an identity or
    uniqueness mechanism.
    """

    scope = "analytics_events"

    def get_rate(self) -> str | None:
        return str(getattr(settings, "ANALYTICS_EVENT_RATE", "600/minute"))

    def get_cache_key(self, request, view):  # type: ignore[no-untyped-def]
        del request, view
        return self.cache_format % {"scope": self.scope, "ident": "global"}
