"""Authoritative competition-membership checks for private reference routes."""

from __future__ import annotations

from typing import Any

from apps.accounts.game_api import current_player
from apps.accounts.models import Competition, CompetitionMembership, Player


def active_competition_for_player(player: Player) -> Competition | None:
    """Return the active competition only while its membership is current.

    The database membership is authoritative. A stale active-competition
    pointer is cleared so a deleted or former membership cannot authorize a
    reference-route request.
    """

    active_id = player.active_competition_id
    if active_id is None:
        return None
    competition = (
        Competition.objects.filter(
            pk=active_id,
            is_active=True,
            memberships__player_id=player.pk,
        )
        .distinct()
        .first()
    )
    if competition is None:
        Player.objects.filter(pk=player.pk, active_competition_id=active_id).update(
            active_competition=None
        )
    return competition


def competition_membership_reference_authorizer(
    user: Any, competition_id: str, request: Any
) -> bool:
    """Authorize the current player only for their active membership."""

    player = current_player(request)
    if player is None or not getattr(user, "is_authenticated", False):
        return False
    if player.user_id != getattr(user, "pk", None):
        return False
    competition = active_competition_for_player(player)
    if competition is None or str(competition.pk) != competition_id:
        return False
    return CompetitionMembership.objects.filter(competition=competition, player=player).exists()


def default_reference_route_authorizer(user: Any, competition_id: str, request: Any) -> bool:
    return competition_membership_reference_authorizer(user, competition_id, request)


def allow_session_claim_for_tests(user: Any, competition_id: str, request: Any) -> bool:
    """Safe test-only checker; production settings must not select this hook."""
    return bool(
        getattr(user, "is_authenticated", False)
        and request.session.get("game_session", {}).get("test_authorized_competition")
        == competition_id
    )
