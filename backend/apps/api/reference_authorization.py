"""Pluggable authoritative game-membership contract for private references.

The competition application must replace ``REFERENCE_ROUTE_AUTHORIZER`` with
an implementation that queries current membership on every request. The
default is deliberately deny-all; a signed/session claim is never sufficient.
"""

from __future__ import annotations

from typing import Any


def default_reference_route_authorizer(user: Any, competition_id: str, request: Any) -> bool:
    del user, competition_id, request
    return False


def allow_session_claim_for_tests(user: Any, competition_id: str, request: Any) -> bool:
    """Safe test-only checker; production settings must not select this hook."""
    return bool(
        getattr(user, "is_authenticated", False)
        and request.session.get("game_session", {}).get("test_authorized_competition")
        == competition_id
    )
