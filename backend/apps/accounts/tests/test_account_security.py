"""Security regression tests for account rate limits and reset delivery."""

# mypy: disable-error-code="import-untyped"

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIRequestFactory

from apps.accounts.account_api import AccountThrottle
from apps.accounts.account_tasks import send_password_reset_email_task
from apps.api.throttling import PlayerSessionThrottle

pytestmark = pytest.mark.django_db


def _request(*, ip: str, user: Any = None, session: dict[str, Any] | None = None) -> Any:
    request = APIRequestFactory().post("/api/v1/game/auth/local/login/")
    request.META["REMOTE_ADDR"] = ip
    request.user = user or SimpleNamespace(is_authenticated=False)
    request.session = session or {}
    return request


def test_account_throttle_uses_stable_private_identity_for_all_session_types() -> None:
    user = get_user_model().objects.create_user(username="signed-in", password="secret")
    user_request = _request(ip="192.0.2.11", user=user)
    player_session = {"player_id": "player-123", "player_session_epoch": 4}
    player_request = _request(ip="192.0.2.12", user=user, session=player_session)
    anon_request = _request(ip="192.0.2.13")
    throttle = AccountThrottle()

    keys = [
        throttle.get_cache_key(anon_request, None),
        throttle.get_cache_key(user_request, None),
        throttle.get_cache_key(player_request, None),
    ]
    assert all(keys)
    assert len(set(keys)) == 3
    assert throttle.get_cache_key(user_request, None) == throttle.get_cache_key(
        _request(ip="192.0.2.99", user=user), None
    )
    assert throttle.get_cache_key(player_request, None) == throttle.get_cache_key(
        _request(ip="192.0.2.98", user=user, session=player_session), None
    )
    assert all("192.0.2" not in str(key) and "signed-in" not in str(key) for key in keys)

    session_throttle = PlayerSessionThrottle()
    player_key = session_throttle.get_cache_key(player_request, None)
    assert player_key == session_throttle.get_cache_key(
        _request(ip="192.0.2.97", session=player_session), None
    )
    assert "player-123" not in str(player_key)


@override_settings(
    REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": {"player_accounts": "1/minute"}},
    RATE_LIMIT_HMAC_SECRET="unit-test-throttle-secret",
)
@pytest.mark.parametrize("identity", ["anonymous", "django", "player"])
def test_account_throttle_limits_anonymous_and_authenticated_callers(identity: str) -> None:
    cache.clear()
    user = get_user_model().objects.create_user(username=f"{identity}-user", password="secret")
    if identity == "anonymous":
        request = _request(ip="192.0.2.20")
    elif identity == "django":
        request = _request(ip="192.0.2.21", user=user)
    else:
        request = _request(
            ip="192.0.2.22",
            user=user,
            session={"player_id": "player-456", "player_session_epoch": 2},
        )

    class OnePerMinuteAccountThrottle(AccountThrottle):
        def get_rate(self) -> str:
            return "1/minute"

    throttle = OnePerMinuteAccountThrottle()

    assert throttle.allow_request(request, None)
    assert not OnePerMinuteAccountThrottle().allow_request(request, None)


def test_reset_transport_failure_is_logged_without_account_data(caplog: Any) -> None:
    user = get_user_model().objects.create_user(
        username="mail-user", email="private-address@example.com", password="secret"
    )
    with patch(
        "apps.accounts.account_tasks.send_mail",
        side_effect=RuntimeError("SMTP failed for private-address@example.com and token-secret"),
    ):
        result = send_password_reset_email_task.run(str(user.pk), "uid-secret", "token-secret")

    assert result == {"sent": False}
    assert "password reset email delivery failed" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "private-address@example.com" not in caplog.text
    assert "token-secret" not in caplog.text
