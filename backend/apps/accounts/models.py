"""Private player identity and Strava credential lifecycle models."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from .fields import EncryptedSecretField


class Player(models.Model):
    class Lifecycle(models.TextChoices):
        CONNECTED = "connected", "Connected"
        DISCONNECTED = "disconnected", "Disconnected"
        PENDING_DELETION = "pending-deletion", "Pending deletion"
        DELETED = "deleted", "Deleted"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="player"
    )
    strava_athlete_id = models.PositiveBigIntegerField(unique=True)
    strava_display_name = models.CharField(max_length=240, blank=True)
    strava_profile_image_url = models.URLField(max_length=500, blank=True)
    nickname = models.CharField(max_length=80, blank=True)
    lifecycle = models.CharField(
        max_length=24, choices=Lifecycle.choices, default=Lifecycle.CONNECTED
    )
    deletion_deadline = models.DateTimeField(null=True, blank=True)
    session_epoch = models.PositiveBigIntegerField(default=0)
    connected_at = models.DateTimeField(default=timezone.now)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("pk",)

    def __str__(self) -> str:
        return f"Player {self.strava_athlete_id}"

    def invalidate_sessions(self) -> None:
        self.session_epoch += 1
        self.save(update_fields=("session_epoch", "updated_at"))

    def mark_disconnected(self, *, pending_deletion: bool = True) -> None:
        self.lifecycle = (
            self.Lifecycle.PENDING_DELETION if pending_deletion else self.Lifecycle.DISCONNECTED
        )
        self.disconnected_at = timezone.now()
        self.deletion_deadline = timezone.now() + timedelta(days=30) if pending_deletion else None
        self.invalidate_sessions()
        self.save(update_fields=("lifecycle", "disconnected_at", "deletion_deadline", "updated_at"))

    def restore_connection(self) -> None:
        self.lifecycle = self.Lifecycle.CONNECTED
        self.deletion_deadline = None
        self.disconnected_at = None
        self.connected_at = timezone.now()
        self.save(
            update_fields=(
                "lifecycle",
                "deletion_deadline",
                "disconnected_at",
                "connected_at",
                "updated_at",
            )
        )


class PlayerCredential(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name="credential")
    access_token = EncryptedSecretField()
    refresh_token = EncryptedSecretField()
    expires_at = models.DateTimeField()
    scopes = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Credentials for player {self.player_id}"


class OAuthState(models.Model):
    """Single-use, session-bound state; only a digest is persisted."""

    state_digest = models.CharField(max_length=64, unique=True)
    session_key = models.CharField(max_length=40)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"OAuth state {self.pk}"

    @classmethod
    def issue(cls, session_key: str) -> tuple[OAuthState, str]:
        raw = secrets.token_urlsafe(32)
        state = cls.objects.create(
            state_digest=hashlib.sha256(raw.encode()).hexdigest(),
            session_key=session_key,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        return state, raw

    @staticmethod
    def digest(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()
