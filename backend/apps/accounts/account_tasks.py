"""Asynchronous local-account email delivery."""

from __future__ import annotations

import logging
import smtplib
from typing import Any

from celery import shared_task  # type: ignore[import-untyped]
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail

from .account_services import unprotect_reset_email

logger = logging.getLogger(__name__)


def _is_transient_smtp_error(exc: Exception) -> bool:
    if isinstance(exc, (OSError, smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)):
        return True
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [int(value[0]) for value in exc.recipients.values()]
        return bool(codes) and all(400 <= code < 500 for code in codes)
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= int(exc.smtp_code) < 500
    return False


@shared_task(
    bind=True,
    max_retries=3,
    name="bikemapy.accounts.send_password_reset_email",
)  # type: ignore[untyped-decorator]
def send_password_reset_email_task(task: Any, encrypted_email: str) -> dict[str, Any]:
    """Look up and deliver reset mail off-request using an opaque broker payload."""

    email = unprotect_reset_email(encrypted_email)
    user = get_user_model().objects.filter(email__iexact=email, is_active=True).first()
    if user is None:
        return {"accepted": True}
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
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
        # Never log recipients, reset material, or SMTP exception text.
        error_type = type(exc).__name__
        if _is_transient_smtp_error(exc):
            attempt = int(task.request.retries) + 1
            if task.request.retries < task.max_retries:
                countdown = min(30 * 2 ** int(task.request.retries), 900)
                logger.warning(
                    "password reset email retry scheduled (attempt=%s/%s error_type=%s)",
                    attempt,
                    task.max_retries + 1,
                    error_type,
                    extra={
                        "event": "account_email_retry_scheduled",
                        "error_type": error_type,
                        "attempt": attempt,
                    },
                )
                raise task.retry(countdown=countdown) from None
            logger.error(
                "password reset email retries exhausted (error_type=%s)",
                error_type,
                extra={"event": "account_email_delivery_failed", "error_type": error_type},
            )
            return {"accepted": True}
        logger.error(
            "password reset email delivery failed (error_type=%s)",
            error_type,
            extra={"event": "account_email_delivery_failed", "error_type": error_type},
        )
    return {"accepted": True}
