"""django-allauth hooks for the single owner-admin boundary."""

# django-allauth does not currently ship type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponseRedirect

from .account_services import AccountError, validate_invite_code
from .authorization import is_owner_github_id, sociallogin_github_id
from .competition_services import join_competition
from .models import CompetitionInviteRedemption, Player


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
            local_player = Player.objects.filter(
                pk=link_player_id,
                lifecycle=Player.Lifecycle.CONNECTED,
                session_epoch=link_epoch,
            ).select_related("user").first()
            if local_player is None or not getattr(request.user, "is_authenticated", False):
                raise ImmediateHttpResponse(HttpResponseRedirect("/game?game_auth=error"))
            if request.user.pk != local_player.user_id:
                raise ImmediateHttpResponse(HttpResponseRedirect("/game?game_auth=error"))
        elif getattr(request.user, "is_authenticated", False) and Player.objects.filter(
            user_id=getattr(request.user, "pk", None)
        ).exists():
            # A custom local session must explicitly opt into linking.  Do not
            # let a normal social login silently replace or merge identities.
            raise ImmediateHttpResponse(HttpResponseRedirect("/game?game_auth=link_required"))
        # Test doubles and older allauth versions do not expose ``is_existing``;
        # preserve their established owner-admin behavior while real new
        # social logins remain invite-gated.
        sociallogin_is_existing = getattr(sociallogin, "is_existing", True)
        existing_user = getattr(request, "user", None)
        if getattr(existing_user, "is_authenticated", False) and sociallogin_is_existing:
            if getattr(sociallogin.user, "pk", None) != getattr(existing_user, "pk", None):
                raise ImmediateHttpResponse(HttpResponseRedirect("/game?game_auth=error"))
        is_authenticated_player = bool(
            getattr(existing_user, "is_authenticated", False)
            and Player.objects.filter(user_id=getattr(existing_user, "pk", None)).exists()
        )
        if not sociallogin_is_existing and local_player is None and not is_authenticated_player:
            code = request.session.get("account_invite_code")
            try:
                validate_invite_code(code)
            except AccountError as exc:
                raise ImmediateHttpResponse(
                    HttpResponseRedirect("/game?game_auth=invite_required")
                ) from exc
            email = str(getattr(sociallogin.user, "email", "") or "").strip().lower()
            if email and get_user_model().objects.filter(email__iexact=email).exists():
                raise ImmediateHttpResponse(HttpResponseRedirect("/game?game_auth=error"))
        user = sociallogin.user
        if getattr(user, "pk", None):
            user.is_staff = is_owner_github_id(sociallogin_github_id(sociallogin))
            user.save(update_fields=["is_staff"])

    def save_user(self, request: Any, sociallogin: Any, form: Any = None) -> Any:
        with transaction.atomic():
            if request.session.get("github_link_intent"):
                player = Player.objects.select_for_update().filter(
                    pk=request.session.get("github_link_player_id"),
                    session_epoch=request.session.get("github_link_epoch"),
                    lifecycle=Player.Lifecycle.CONNECTED,
                ).first()
                if player is None or getattr(request.user, "pk", None) != player.user_id:
                    raise ImmediateHttpResponse(HttpResponseRedirect("/game?game_auth=error"))
                user = player.user
                request.session.pop("github_link_intent", None)
                request.session.pop("github_link_player_id", None)
                request.session.pop("github_link_epoch", None)
                return user
            user = super().save_user(request, sociallogin, form)
            if (
                not getattr(sociallogin, "is_existing", True)
                and not Player.objects.filter(user=user).exists()
            ):
                competition = validate_invite_code(request.session.get("account_invite_code"))
                competition = type(competition).objects.select_for_update().get(pk=competition.pk)
                from .account_services import invite_digest

                if CompetitionInviteRedemption.objects.filter(
                    competition=competition,
                    code_digest=invite_digest(competition.invite_code),
                ).exists():
                    raise ImmediateHttpResponse(
                        HttpResponseRedirect("/game?game_auth=invite_required")
                    )
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
                    code_digest=invite_digest(competition.invite_code),
                )
                request.session.pop("account_invite_code", None)
        user.is_staff = is_owner_github_id(sociallogin_github_id(sociallogin))
        user.save(update_fields=["is_staff"])
        return user

    def login(self, request: Any, user: Any) -> None:
        super().login(request, user)
        player = Player.objects.filter(user=user, lifecycle=Player.Lifecycle.CONNECTED).first()
        if player is None:
            return
        request.session.cycle_key()
        request.session["player_id"] = player.pk
        request.session["player_session_epoch"] = player.session_epoch
        request.session.save()
