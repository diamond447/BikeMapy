from __future__ import annotations

from typing import Any

import pytest
from allauth.core import context
from allauth.socialaccount.internal.flows.login import complete_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError
from django.test import Client, override_settings
from django.urls import reverse

from apps.accounts.authorization import is_owner
from apps.accounts.models import Competition, CompetitionInviteRedemption, Player

pytestmark = pytest.mark.django_db


def _game_settings() -> dict[str, Any]:
    return {
        "GAME_ENABLED": True,
        "STRAVA_OAUTH_CLIENT_ID": "client-id",
        "STRAVA_OAUTH_CLIENT_SECRET": "client-secret",
        "STRAVA_TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "STRAVA_IDENTITY_GUARD_KEY": "strava-identity-guard-test-key-1234567890",
    }


def _competition() -> Competition:
    owner = get_user_model().objects.create_user(username="competition-owner")
    owner_player = Player.objects.create(user=owner, nickname="Owner")
    return Competition.objects.create(
        owner=owner_player, name="Invite ride", invite_code="RIDE-183"
    )


def _social_login(uid: str, username: str, email: str = "") -> SocialLogin:
    user = get_user_model()(
        username=username,
        email=email,
    )
    account = SocialAccount(provider="github", uid=uid)
    return SocialLogin(account=account, user=user)


def _request(client: Client) -> Any:
    request = client.get("/").wsgi_request
    request.session = client.session
    request.user = AnonymousUser()
    return request


def _complete_login(request: Any, login: SocialLogin) -> Any:
    with context.request_context(request):
        return complete_login(request, login)


@override_settings(**_game_settings(), PLAYER_ACCOUNTS_ENABLED=True)
def test_actual_allauth_signup_creates_player_membership_and_redemption() -> None:
    competition = _competition()
    client = Client()
    session = client.session
    session["account_invite_code"] = competition.invite_code
    session.save()
    request = _request(client)
    original_session_key = request.session.session_key
    login = _social_login("8800183", "new-github-rider", "rider@example.test")

    response = _complete_login(request, login)

    user = get_user_model().objects.get(username="new-github-rider")
    account = SocialAccount.objects.get(provider="github", uid="8800183")
    player = Player.objects.get(user=user)
    assert account.user_id == user.pk
    assert competition.memberships.filter(player=player).exists()
    assert (
        CompetitionInviteRedemption.objects.filter(competition=competition, player=player).count()
        == 1
    )
    assert "account_invite_code" not in request.session
    assert request.session["player_id"] == player.pk
    assert request.session["player_session_epoch"] == player.session_epoch
    assert request.session.session_key != original_session_key
    assert response.status_code == 302
    assert response["Location"] == "http://localhost:5173/game"
    request.session.save()
    client.cookies["sessionid"] = request.session.session_key
    session_response = client.get(reverse("game-player-session"))
    assert session_response.status_code == 200
    assert session_response.json()["player"]["nickname"] == player.nickname


@override_settings(PLAYER_ACCOUNTS_ENABLED=True)
def test_first_allowlisted_owner_bootstraps_without_invite_and_reaches_admin() -> None:
    previous_user = get_user_model().objects.create_user(username="previous-player")
    previous_player = Player.objects.create(user=previous_user, nickname="Previous")
    request = _request(Client())
    request.session["player_id"] = previous_player.pk
    request.session["player_session_epoch"] = previous_player.session_epoch
    with override_settings(GITHUB_OWNER_IDS=frozenset({"8800183"})):
        response = _complete_login(request, _social_login("8800183", "github-owner"))

    user = get_user_model().objects.get(username="github-owner")
    assert user.is_staff
    assert SocialAccount.objects.filter(user=user, provider="github", uid="8800183").exists()
    assert not Player.objects.filter(user=user).exists()
    assert "player_id" not in request.session
    assert "player_session_epoch" not in request.session
    assert response["Location"] == "/admin/"


@override_settings(PLAYER_ACCOUNTS_ENABLED=True)
def test_non_owner_signup_without_invite_is_rejected_without_persisting_user() -> None:
    _competition()
    request = _request(Client())

    response = _complete_login(request, _social_login("8800184", "no-invite"))

    assert response.status_code == 302
    assert response["Location"] == "http://localhost:5173/game?game_auth=invite_required"
    assert not get_user_model().objects.filter(username="no-invite").exists()
    assert not SocialAccount.objects.filter(provider="github", uid="8800184").exists()
    assert not CompetitionInviteRedemption.objects.exists()


@override_settings(PLAYER_ACCOUNTS_ENABLED=True)
def test_failed_membership_rolls_back_allauth_user_and_social_account(monkeypatch: Any) -> None:
    competition = _competition()
    client = Client()
    session = client.session
    session["account_invite_code"] = competition.invite_code
    session.save()
    request = _request(client)

    def fail_membership(*args: Any, **kwargs: Any) -> None:
        raise IntegrityError("membership failure")

    monkeypatch.setattr("apps.accounts.adapters.join_competition", fail_membership)
    with pytest.raises(IntegrityError, match="membership failure"):
        _complete_login(request, _social_login("8800185", "failed-rider"))

    assert not get_user_model().objects.filter(username="failed-rider").exists()
    assert not SocialAccount.objects.filter(provider="github", uid="8800185").exists()
    assert not competition.invite_redemptions.exists()


def test_github_oauth_url_is_resolved_against_backend_request_origin() -> None:
    _competition()
    client = Client()
    with override_settings(PLAYER_ACCOUNTS_ENABLED=True, ALLOWED_HOSTS=["api.example.test"]):
        response = client.post(
            reverse("game-github-onboard"),
            data={"invite_code": "RIDE-183"},
            content_type="application/json",
            HTTP_HOST="api.example.test",
        )

    assert response.status_code == 200
    assert response.json()["url"] == "http://api.example.test/accounts/github/login/"


def test_github_link_url_is_resolved_against_backend_request_origin() -> None:
    user = get_user_model().objects.create_user(username="link-rider")
    player = Player.objects.create(user=user, nickname="Link rider")
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    assert session.session_key is not None
    client.cookies["sessionid"] = session.session_key

    with override_settings(PLAYER_ACCOUNTS_ENABLED=True, ALLOWED_HOSTS=["api.example.test"]):
        response = client.post(
            reverse("game-account-github-link"),
            content_type="application/json",
            HTTP_HOST="api.example.test",
        )

    assert response.status_code == 200
    assert response.json()["url"] == (
        "http://api.example.test/accounts/github/login/?process=connect"
    )


@override_settings(
    GITHUB_OWNER_IDS=frozenset({"8800186"}),
    SOCIALACCOUNT_PROVIDERS={"github": {"APP": {"client_id": "client", "secret": "secret"}}},
)
def test_actual_allauth_owner_connect_promotes_admin_and_consumes_intent() -> None:
    user = get_user_model().objects.create_user(username="explicit-link-rider")
    player = Player.objects.create(user=user, nickname="Link rider")
    client = Client()
    request = _request(client)
    request.user = user
    request.session["player_id"] = player.pk
    request.session["player_session_epoch"] = player.session_epoch
    request.session["github_link_intent"] = "single-use-intent"
    request.session["github_link_player_id"] = str(player.pk)
    request.session["github_link_epoch"] = player.session_epoch
    sociallogin = _social_login("8800186", "provider-profile")
    sociallogin.state["process"] = "connect"

    response = _complete_login(request, sociallogin)

    assert SocialAccount.objects.get(provider="github", uid="8800186").user_id == user.pk
    user.refresh_from_db()
    assert user.is_staff
    assert is_owner(user, request)
    assert response["Location"] == "/admin/"
    assert request.session["player_id"] == player.pk
    assert request.session["player_session_epoch"] == player.session_epoch
    assert "github_link_intent" not in request.session
    assert "github_link_player_id" not in request.session
    assert "github_link_epoch" not in request.session


def test_authenticated_player_github_login_without_link_intent_cannot_merge() -> None:
    user = get_user_model().objects.create_user(username="implicit-link-rider")
    Player.objects.create(user=user, nickname="Link rider")
    request = _request(Client())
    request.user = user
    sociallogin = _social_login("8800187", "unlinked-profile")
    sociallogin.state["process"] = "connect"

    response = _complete_login(request, sociallogin)

    assert response.status_code == 302
    assert response["Location"] == "http://localhost:5173/game?game_auth=link_required"
    assert not SocialAccount.objects.filter(provider="github", uid="8800187").exists()


@override_settings(**_game_settings(), PLAYER_ACCOUNTS_ENABLED=True)
def test_authenticated_player_can_reauthenticate_its_exact_linked_github_account() -> None:
    user = get_user_model().objects.create_user(username="returning-linked-player")
    player = Player.objects.create(user=user, nickname="Linked rider")
    SocialAccount.objects.create(user=user, provider="github", uid="8800188")
    request = _request(Client())
    request.user = user

    response = _complete_login(request, _social_login("8800188", "provider-profile"))

    assert response.status_code == 302
    assert response["Location"] == "http://localhost:5173/game"
    assert request.session["player_id"] == player.pk
    assert request.session["player_session_epoch"] == player.session_epoch
