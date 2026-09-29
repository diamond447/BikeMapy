"""django-allauth hooks for the single owner-admin boundary."""

# django-allauth does not currently ship type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponseRedirect

from .account_services import AccountError, validate_invite_code
from .authorization import is_owner_github_id, sociallogin_github_id
from .competition_services import join_competition
from .models import CompetitionInviteRedemption, Player


def _game_redirect(result: str) -> HttpResponseRedirect:
    base = str(getattr(settings, "GAME_FRONTEND_URL", "http://localhost:5173/game"))
    return HttpResponseRedirect(f"{base}?{urlencode({'game_auth': result})}")


class OwnerAccountAdapter(DefaultAccountAdapter):
    """Attach game sessions and route successful logins by account role."""

    def login(self, request: Any, user: Any) -> None:
        # django-allauth calls this account adapter hook (the social-account
        # adapter's similarly named method is never part of the login flow).
        super().login(request, user)
        player = Player.objects.filter(user=user, lifecycle=Player.Lifecycle.CONNECTED).first()
        if player is not None:
            request.session["player_id"] = player.pk
            request.session["player_session_epoch"] = player.session_epoch
        else:
            request.session.pop("player_id", None)
            request.session.pop("player_session_epoch", None)
        request.session.save()

    def _redirect_for_user(self, user: Any) -> str:
        from allauth.socialaccount.models import SocialAccount

        github_ids = SocialAccount.objects.filter(user=user, provider="github").values_list(
            "uid", flat=True
        )
        if any(is_owner_github_id(uid) for uid in github_ids):
            return "/admin/"
        return str(getattr(settings, "GAME_FRONTEND_URL", "http://localhost:5173/game"))

    def get_login_redirect_url(self, request: Any) -> str:
        return self._redirect_for_user(request.user)

    def get_signup_redirect_url(self, request: Any) -> str:
        return self._redirect_for_user(request.user)


class OwnerSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Keep staff status aligned with the immutable GitHub allowlist."""

    def pre_social_login(self, request: Any, sociallogin: Any) -> None:
        super().pre_social_login(request, sociallogin)
        session = request.session
        link_intent = session.get("github_link_intent")
        link_player_id = session.get("github_link_player_id")
        link_epoch = session.get("github_link_epoch")
        local_player = None
        if link_intent and link_player_id and link_epoch is not None:
            if getattr(sociallogin, "state", {}).get("process") != "connect":
                raise ImmediateHttpResponse(_game_redirect("link_required"))
            local_player = (
                Player.objects.filter(
                    pk=link_player_id,
                    lifecycle=Player.Lifecycle.CONNECTED,
                    session_epoch=link_epoch,
                )
                .select_related("user")
                .first()
            )
            if local_player is None or not getattr(request.user, "is_authenticated", False):
                raise ImmediateHttpResponse(_game_redirect("error"))
            if request.user.pk != local_player.user_id:
                raise ImmediateHttpResponse(_game_redirect("error"))
        elif (
            getattr(request.user, "is_authenticated", False)
            and Player.objects.filter(user_id=getattr(request.user, "pk", None)).exists()
        ):
            existing_account = getattr(sociallogin, "account", None)
            same_linked_identity = bool(
                getattr(sociallogin, "is_existing", False)
                and getattr(sociallogin.user, "pk", None) == request.user.pk
                and getattr(existing_account, "provider", None) == "github"
                and SocialAccount.objects.filter(
                    user_id=request.user.pk,
                    provider="github",
                    uid=getattr(existing_account, "uid", None),
                ).exists()
            )
            if not same_linked_identity:
                # A local player can reauthenticate a GitHub account already
                # linked to this exact user. Other identities still require an
                # explicit, session-bound connect intent.
                raise ImmediateHttpResponse(_game_redirect("link_required"))
        # Test doubles and older allauth versions do not expose ``is_existing``;
        # preserve their established owner-admin behavior while real new
        # social logins remain invite-gated.
        sociallogin_is_existing = getattr(sociallogin, "is_existing", True)
        existing_user = getattr(request, "user", None)
        if getattr(existing_user, "is_authenticated", False) and sociallogin_is_existing:
            if getattr(sociallogin.user, "pk", None) != getattr(existing_user, "pk", None):
                raise ImmediateHttpResponse(_game_redirect("error"))
        is_authenticated_player = bool(
            getattr(existing_user, "is_authenticated", False)
            and Player.objects.filter(user_id=getattr(existing_user, "pk", None)).exists()
        )
        is_allowlisted_owner = is_owner_github_id(sociallogin_github_id(sociallogin))
        if (
            not sociallogin_is_existing
            and local_player is None
            and not is_authenticated_player
            and not is_allowlisted_owner
        ):
            code = request.session.get("account_invite_code")
            try:
                validate_invite_code(code)
            except AccountError as exc:
                raise ImmediateHttpResponse(_game_redirect("invite_required")) from exc
            email = str(getattr(sociallogin.user, "email", "") or "").strip().lower()
            if email and get_user_model().objects.filter(email__iexact=email).exists():
                raise ImmediateHttpResponse(_game_redirect("error"))
        user = sociallogin.user
        if getattr(user, "pk", None):
            user.is_staff = is_owner_github_id(sociallogin_github_id(sociallogin))
            user.save(update_fields=["is_staff"])

    def get_connect_redirect_url(self, request: Any, socialaccount: Any) -> str:
        for key in (
            "github_link_intent",
            "github_link_player_id",
            "github_link_epoch",
        ):
            request.session.pop(key, None)
        linked_to_current_user = SocialAccount.objects.filter(
            user_id=request.user.pk,
            provider="github",
            uid=getattr(socialaccount, "uid", None),
        ).exists()
        if linked_to_current_user:
            has_allowlisted_identity = any(
                is_owner_github_id(uid)
                for uid in SocialAccount.objects.filter(
                    user_id=request.user.pk, provider="github"
                ).values_list("uid", flat=True)
            )
            request.user.is_staff = has_allowlisted_identity
            request.user.save(update_fields=["is_staff"])
            if is_owner_github_id(getattr(socialaccount, "uid", None)):
                from allauth.account.internal.flows.login import record_authentication

                record_authentication(
                    request,
                    request.user,
                    "socialaccount",
                    provider="github",
                    uid=socialaccount.uid,
                )
        request.session.save()
        if linked_to_current_user and has_allowlisted_identity:
            return "/admin/"
        return str(getattr(settings, "GAME_FRONTEND_URL", "http://localhost:5173/game"))

    def save_user(self, request: Any, sociallogin: Any, form: Any = None) -> Any:
        with transaction.atomic():
            is_new_signup = not getattr(sociallogin, "is_existing", True)
            user = sociallogin.user
            user.set_unusable_password()
            user.is_staff = is_owner_github_id(sociallogin_github_id(sociallogin))
            if form:
                from allauth.account.adapter import get_adapter as get_account_adapter

                get_account_adapter().save_user(request, user, form)
            else:
                from allauth.account.adapter import get_adapter as get_account_adapter

                get_account_adapter().populate_username(request, user)
                user.save()
            # This persists the user, SocialAccount and verified email records.
            # Keep it in the same transaction as the player and invite lifecycle
            # so any failure rolls the complete first signup back.
            sociallogin.save(request)
            if is_new_signup and not is_owner_github_id(sociallogin_github_id(sociallogin)):
                competition = validate_invite_code(request.session.get("account_invite_code"))
                competition = type(competition).objects.select_for_update().get(pk=competition.pk)
                from .account_services import invite_digest

                digest = invite_digest(competition.invite_code)
                if CompetitionInviteRedemption.objects.filter(
                    competition=competition, code_digest=digest
                ).exists():
                    raise ImmediateHttpResponse(_game_redirect("invite_required"))
                player = Player.objects.create(
                    user=user,
                    strava_athlete_id=None,
                    strava_display_name=user.username,
                    nickname=user.username,
                )
                join_competition(player, invite_code=competition.invite_code)
                CompetitionInviteRedemption.objects.create(
                    competition=competition,
                    player=player,
                    code_digest=digest,
                )
                request.session.pop("account_invite_code", None)
        return user
