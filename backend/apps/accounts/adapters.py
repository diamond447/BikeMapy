"""django-allauth hooks for the single owner-admin boundary."""

# django-allauth does not currently ship type stubs.
# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from typing import Any

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .authorization import is_owner_github_id, sociallogin_github_id


class OwnerAccountAdapter(DefaultAccountAdapter):
    """Disable local username/password signup; the owner signs in via GitHub."""

    def is_open_for_signup(self, request: Any) -> bool:
        return False


class OwnerSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Keep staff status aligned with the immutable GitHub allowlist."""

    def is_open_for_signup(self, request: Any, sociallogin: Any) -> bool:
        # Only the allowlisted owner needs an account; other GitHub identities
        # must not create local users through the OAuth callback.
        return is_owner_github_id(sociallogin_github_id(sociallogin))

    def pre_social_login(self, request: Any, sociallogin: Any) -> None:
        super().pre_social_login(request, sociallogin)
        user = sociallogin.user
        if getattr(user, "pk", None):
            user.is_staff = is_owner_github_id(sociallogin_github_id(sociallogin))
            user.save(update_fields=["is_staff"])

    def save_user(self, request: Any, sociallogin: Any, form: Any = None) -> Any:
        user = super().save_user(request, sociallogin, form)
        user.is_staff = is_owner_github_id(sociallogin_github_id(sociallogin))
        user.save(update_fields=["is_staff"])
        return user
