"""API throttles with trusted, non-identifying client cache keys."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from config.client_identity import client_ip, rate_limit_identifier


class TrustedClientThrottleMixin:
    """Use the deployment-boundary identity policy for throttle keys."""

    def get_ident(self, request):  # type: ignore[no-untyped-def]
        return rate_limit_identifier(client_ip(request))


class ApiAnonRateThrottle(TrustedClientThrottleMixin, AnonRateThrottle):
    """Throttle anonymous API clients without storing raw addresses in keys."""


class ApiUserRateThrottle(TrustedClientThrottleMixin, UserRateThrottle):
    """Throttle authenticated API clients using the same boundary policy."""
