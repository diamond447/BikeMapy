"""Asynchronous local-account email delivery."""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


@shared_task(name="bikemapy.accounts.send_password_reset_email")  # type: ignore[untyped-decorator]
def send_password_reset_email_task(user_id: str, uid: str, token: str) -> dict[str, Any]:
    """Deliver a reset link after the HTTP response has been formed."""

    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        return {"sent": False}
    base = str(getattr(settings, "GAME_FRONTEND_URL", "http://localhost:5173/game")).rstrip("/")
    try:
        send_mail(
            subject="Reset your BikeMapy password",
            message=f"Reset your password at {base}/reset-password/{uid}/{token}/",
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=False,
        )
    except Exception as exc:
        # Avoid logging email addresses, reset material, or backend exception text.
        logger.error(
            "password reset email delivery failed (error_type=%s)",
            type(exc).__name__,
            extra={"event": "account_email_delivery_failed", "error_type": type(exc).__name__},
        )
        return {"sent": False}
    return {"sent": True}
