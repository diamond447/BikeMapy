"""Provider-neutral player accounts and invite-gated onboarding."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils.encoding import force_bytes
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.utils.http import urlsafe_base64_encode

from .competition_services import CompetitionError, join_competition
from .models import Competition, CompetitionInviteRedemption, Player

TEMP_PASSWORD_LENGTH = 20


class AccountError(ValueError):
    """Safe account error suitable for a public API response."""

    def __init__(self, detail: str, *, code: str = "invalid") -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


def normalize_username(value: Any) -> str:
    username = str(value or "").strip()
    if not username or len(username) > 150 or any(ch.isspace() for ch in username):
        raise AccountError("Enter a valid username.", code="invalid_username")
    return username


def normalize_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if not email or len(email) > 254 or "@" not in email:
        raise AccountError("Enter a valid email address.", code="invalid_email")
    return email


def invite_digest(code: str) -> str:
    key = str(getattr(settings, "SECRET_KEY", "")).encode()
    return hmac.new(key, code.strip().upper().encode(), hashlib.sha256).hexdigest()


def validate_invite_code(value: Any) -> Competition:
    """Validate without consuming a code or revealing account existence."""

    code = str(value or "").strip().upper()
    competition = Competition.objects.filter(invite_code=code, is_active=True).first()
    if competition is None:
        raise AccountError("The invite code is not valid.", code="invalid_invite")
    return competition


def _temporary_password() -> str:
    return secrets.token_urlsafe(TEMP_PASSWORD_LENGTH)


def _send_temporary_password(user: Any, password: str) -> None:
    # The password is only present in memory during onboarding.  Email backends
    # and logs must never receive it through structured logging.
    send_mail(
        subject="Your BikeMapy player account",
        message=(
            "Your temporary BikeMapy password is ready. Sign in and change it "
            "before accessing the game.\n\n" + password
        ),
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
        recipient_list=[user.email],
        fail_silently=True,
    )


@transaction.atomic
def create_invited_account(*, username: Any, email: Any, invite_code: Any) -> tuple[Player, str]:
    """Create an account and join the invited competition atomically."""

    username_value = normalize_username(username)
    email_value = normalize_email(email)
    competition = validate_invite_code(invite_code)
    user_model = get_user_model()
    if user_model.objects.filter(username__iexact=username_value).exists():
        raise AccountError("Unable to create this account.", code="account_unavailable")
    # A generic response is used by HTTP callers, while the domain service
    # rejects duplicate credentials before an account can be created.
    if user_model.objects.filter(email__iexact=email_value).exists():
        raise AccountError("Unable to create this account.", code="account_unavailable")
    temporary_password = _temporary_password()
    user = user_model.objects.create_user(
        username=username_value, email=email_value, password=temporary_password
    )
    player = Player.objects.create(
        user=user,
        strava_athlete_id=None,
        strava_display_name=username_value,
        nickname=username_value,
        must_change_password=True,
    )
    try:
        join_competition(player, invite_code=competition.invite_code)
    except CompetitionError as exc:
        raise AccountError(exc.detail, code=exc.code) from exc
    CompetitionInviteRedemption.objects.create(
        competition=competition, player=player, code_digest=invite_digest(competition.invite_code)
    )
    transaction.on_commit(lambda: _send_temporary_password(user, temporary_password))
    return player, temporary_password


def authenticate_player(*, identifier: Any, password: Any) -> Player | None:
    value = str(identifier or "").strip()
    user_model = get_user_model()
    user = user_model.objects.filter(Q(username__iexact=value) | Q(email__iexact=value)).first()
    if user is None:
        # Run a password hash even for unknown identifiers to reduce timing
        # differences and avoid an account enumeration oracle.
        authenticate(username="__unknown__", password=str(password or ""))
        return None
    authenticated = authenticate(username=user.username, password=str(password or ""))
    if authenticated is None:
        return None
    return Player.objects.filter(user=authenticated, lifecycle=Player.Lifecycle.CONNECTED).first()


def set_password(player: Player, password: str, *, clear_temporary: bool = True) -> None:
    password = str(password or "")
    if len(password) < 12:
        raise AccountError("Password must contain at least 12 characters.", code="weak_password")
    user = player.user
    user.set_password(password)
    user.save(update_fields=("password",))
    if clear_temporary and player.must_change_password:
        Player.objects.filter(pk=player.pk).update(must_change_password=False)
        player.must_change_password = False


def request_password_reset(email: Any) -> None:
    """Send a reset link without disclosing whether an address exists."""

    email_value = str(email or "").strip().lower()
    user = get_user_model().objects.filter(email__iexact=email_value, is_active=True).first()
    if user is None:
        return
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    base = str(getattr(settings, "GAME_FRONTEND_URL", "http://localhost:5173/game")).rstrip("/")
    send_mail(
        subject="Reset your BikeMapy password",
        message=f"Reset your password at {base}/reset-password/{uid}/{token}/",
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
        recipient_list=[user.email],
        fail_silently=True,
    )


def confirm_password_reset(*, uidb64: str, token: str, password: str) -> None:
    """Consume a Django single-use reset token and clear forced-change state."""

    try:
        user_id = force_str(urlsafe_base64_decode(uidb64))
        user = get_user_model().objects.get(pk=user_id, is_active=True)
    except (TypeError, ValueError, OverflowError, get_user_model().DoesNotExist):
        raise AccountError("The reset link is invalid or expired.", code="invalid_reset") from None
    if not default_token_generator.check_token(user, token):
        raise AccountError("The reset link is invalid or expired.", code="invalid_reset")
    player = Player.objects.filter(user=user, lifecycle=Player.Lifecycle.CONNECTED).first()
    if player is None:
        raise AccountError("The reset link is invalid or expired.", code="invalid_reset")
    set_password(player, password)
