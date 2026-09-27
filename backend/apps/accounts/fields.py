"""Database fields for secrets that must never be stored in plaintext."""

from __future__ import annotations

from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import models


def _cipher() -> Fernet:
    key = getattr(settings, "STRAVA_TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise ImproperlyConfigured("STRAVA_TOKEN_ENCRYPTION_KEY is required for player credentials")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            "STRAVA_TOKEN_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc


class EncryptedSecretField(models.TextField):  # type: ignore[type-arg]
    """A Fernet-encrypted text field with no plaintext value in the database."""

    description = "Fernet-encrypted secret"

    def get_prep_value(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, bytes):
            value = value.decode()
        return _cipher().encrypt(str(value).encode()).decode()

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> str | None:
        del expression, connection
        if value in (None, ""):
            return None
        try:
            return _cipher().decrypt(str(value).encode()).decode()
        except InvalidToken as exc:
            raise ValidationError("Stored player credential cannot be decrypted") from exc

    def to_python(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        return value if isinstance(value, str) else str(value)
