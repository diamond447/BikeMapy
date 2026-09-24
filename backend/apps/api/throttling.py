"""API throttles with trusted, non-identifying client cache keys."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any

from django.conf import settings
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle

from config.client_identity import client_ip, rate_limit_identifier


class TrustedClientThrottleMixin:
    """Use the deployment-boundary identity policy for throttle keys."""

    def get_ident(self, request: Any) -> str:
        return rate_limit_identifier(client_ip(request))


class ApiAnonRateThrottle(TrustedClientThrottleMixin, AnonRateThrottle):
    """Throttle anonymous API clients without storing raw addresses in keys."""


class ApiUserRateThrottle(TrustedClientThrottleMixin, UserRateThrottle):
    """Throttle authenticated API clients using the same boundary policy."""


class PlayerSessionThrottle(TrustedClientThrottleMixin, AnonRateThrottle):
    """Throttle session-authenticated players by player and epoch."""

    scope = "game_player"

    def get_rate(self) -> str | None:
        return str(getattr(settings, "GAME_PLAYER_RATE", "600/minute"))

    def get_ident(self, request: Any) -> str:
        player_id = request.session.get("player_id")
        epoch = request.session.get("player_session_epoch")
        if player_id and epoch is not None:
            return rate_limit_identifier(f"player-session:{player_id}:{epoch}")
        return TrustedClientThrottleMixin.get_ident(self, request)


class CompetitionInviteThrottle(TrustedClientThrottleMixin, AnonRateThrottle):
    """Keep invite-code guessing bound to the stricter client-IP bucket."""

    scope = "competition_invites"

    def get_rate(self) -> str | None:
        return str(getattr(settings, "COMPETITION_INVITE_RATE", "10/minute"))
