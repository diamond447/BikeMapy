"""Owner-admin authorization based on immutable GitHub account IDs."""

# django-allauth does not currently ship type stubs.
# mypy: disable-error-code="import-untyped"

from __future__ import annotations

from typing import Any

from django.conf import settings


def numeric_github_id(value: Any) -> str | None:
    """Return a canonical numeric GitHub ID, rejecting usernames and IDs with signs."""

    value = str(value or "").strip()
    return str(int(value)) if value.isdigit() and int(value) > 0 else None


def is_owner_github_id(value: Any) -> bool:
    github_id = numeric_github_id(value)
    allowed = {
        canonical
        for item in getattr(settings, "GITHUB_OWNER_IDS", ())
        if (canonical := numeric_github_id(item)) is not None
    }
    return github_id is not None and github_id in allowed


def is_owner(user: Any, request: Any = None) -> bool:
    """Require a current GitHub authentication plus the immutable linked UID."""

    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    if request is None:
        return False
    methods = request.session.get("account_authentication_methods", [])
    if not methods:
        return False
    latest = methods[-1]
    if (
        latest.get("method") != "socialaccount"
        or latest.get("provider") != "github"
        or not is_owner_github_id(latest.get("uid"))
    ):
        return False
    try:
        from allauth.socialaccount.models import SocialAccount

        return any(
            is_owner_github_id(account.uid)
            for account in SocialAccount.objects.filter(user_id=user.pk, provider="github")
        )
    except (ImportError, AttributeError):
        return False


def sociallogin_github_id(sociallogin: Any) -> str | None:
    """Extract the provider UID from an allauth login without using profile names."""

    account = getattr(sociallogin, "account", None)
    if getattr(account, "provider", None) != "github":
        return None
    return numeric_github_id(getattr(account, "uid", None))
