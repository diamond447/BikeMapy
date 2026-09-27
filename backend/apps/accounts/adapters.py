"""django-allauth hooks for the single owner-admin boundary."""

# django-allauth does not currently ship type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.db import transaction
from django.http import HttpResponseRedirect

from .account_services import AccountError, invite_digest, validate_invite_code
from .authorization import is_owner_github_id, sociallogin_github_id
from .competition_services import join_competition
from .models import CompetitionInviteRedemption, Player


class OwnerSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Keep staff status aligned with the immutable GitHub allowlist."""

    def pre_social_login(self, request: Any, sociallogin: Any) -> None:
        super().pre_social_login(request, sociallogin)
        existing_user = getattr(request, "user", None)
        if getattr(existing_user, "is_authenticated", False) and getattr(
            sociallogin, "is_existing", False
        ):
            if getattr(sociallogin.user, "pk", None) != getattr(existing_user, "pk", None):
                raise ImmediateHttpResponse(HttpResponseRedirect("/game?game_auth=error"))
        is_authenticated_player = bool(
            getattr(existing_user, "is_authenticated", False)
            and Player.objects.filter(user_id=getattr(existing_user, "pk", None)).exists()
        )
        if not getattr(sociallogin, "is_existing", False) and not is_authenticated_player:
            code = request.session.get("account_invite_code")
            try:
                validate_invite_code(code)
            except AccountError as exc:
                raise ImmediateHttpResponse(
                    HttpResponseRedirect("/game?game_auth=invite_required")
                ) from exc
        user = sociallogin.user
        if getattr(user, "pk", None):
            user.is_staff = is_owner_github_id(sociallogin_github_id(sociallogin))
            user.save(update_fields=["is_staff"])

    def save_user(self, request: Any, sociallogin: Any, form: Any = None) -> Any:
        with transaction.atomic():
            user = super().save_user(request, sociallogin, form)
            if not Player.objects.filter(user=user).exists():
                competition = validate_invite_code(request.session.get("account_invite_code"))
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
