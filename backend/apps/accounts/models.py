"""Private player identity and Strava credential lifecycle models."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.catalogue.fields import RouteGeometryField

from .fields import EncryptedSecretField

OAUTH_STATE_TTL = timedelta(minutes=10)


def webhook_event_expiry() -> datetime:
    return timezone.now() + timedelta(days=30)


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
    active_competition = models.ForeignKey(
        "accounts.Competition",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="active_players",
    )
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
        now = timezone.now()
        self.lifecycle = (
            self.Lifecycle.PENDING_DELETION if pending_deletion else self.Lifecycle.DISCONNECTED
        )
        self.disconnected_at = now
        self.deletion_deadline = now + timedelta(days=30) if pending_deletion else None
        self.strava_display_name = ""
        self.strava_profile_image_url = ""
        self.nickname = ""
        self.invalidate_sessions()
        self.save(
            update_fields=(
                "lifecycle",
                "disconnected_at",
                "deletion_deadline",
                "strava_display_name",
                "strava_profile_image_url",
                "nickname",
                "updated_at",
            )
        )

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


class Competition(models.Model):
    """A private, invite-only game competition.

    The UUID primary key keeps guessed identifiers from becoming a useful
    enumeration primitive.  ``invite_code`` is intentionally rotated as one
    value: a code is reusable until the owner rotates it, at which point the
    previous value immediately stops authenticating join requests.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    owner = models.ForeignKey(
        "accounts.Player", on_delete=models.CASCADE, related_name="owned_competitions"
    )
    name = models.CharField(max_length=120)
    invite_code = models.CharField(max_length=32, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "id")

    def __str__(self) -> str:
        return self.name


class CompetitionMembership(models.Model):
    """One player's role and display color in one competition."""

    class SharingScope(models.TextChoices):
        NONE = "none", "No sharing"
        RECENT = "recent", "Recent history"
        FULL_HISTORY = "full_history", "Full available history"

    competition = models.ForeignKey(
        Competition, on_delete=models.CASCADE, related_name="memberships"
    )
    player = models.ForeignKey(
        "accounts.Player", on_delete=models.CASCADE, related_name="competition_memberships"
    )
    color = models.CharField(max_length=7)
    sharing_scope = models.CharField(
        max_length=16, choices=SharingScope.choices, default=SharingScope.NONE
    )
    sharing_consent_at = models.DateTimeField(null=True, blank=True)
    sharing_disclosure_version = models.CharField(max_length=32, blank=True)
    joined_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("joined_at", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("competition", "player"), name="accounts_competition_member_unique"
            ),
        ]
        indexes = [models.Index(fields=("player", "competition"))]

    def __str__(self) -> str:
        return f"{self.player_id} in {self.competition_id}"


class CompetitionSharingConsentAudit(models.Model):
    """Durable, non-PII record of each competition sharing decision."""

    class Action(models.TextChoices):
        GRANTED = "granted", "Granted"
        WITHDRAWN = "withdrawn", "Withdrawn"

    membership = models.ForeignKey(
        CompetitionMembership, on_delete=models.CASCADE, related_name="sharing_audits"
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    scope = models.CharField(max_length=16, choices=CompetitionMembership.SharingScope.choices)
    disclosure_version = models.CharField(max_length=32)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("-recorded_at", "-pk")
        indexes = [models.Index(fields=("membership", "recorded_at"))]

    def __str__(self) -> str:
        return f"sharing-consent:{self.membership_id}:{self.action}:{self.recorded_at.isoformat()}"


class ImportedActivity(models.Model):
    """An activity imported once for a player and shared by their competitions."""

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    player = models.ForeignKey(
        "accounts.Player", on_delete=models.CASCADE, related_name="imported_activities"
    )
    provider_activity_id = models.CharField(max_length=80)
    title = models.CharField(max_length=240, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    calendar_date = models.DateField(null=True, blank=True)
    activity_type = models.CharField(max_length=48, blank=True)
    visibility = models.CharField(max_length=32, blank=True)
    geometry = RouteGeometryField(srid=4326, spatial_index=True, blank=True, null=True)
    geometry_hash = models.CharField(max_length=64, blank=True)
    provider_updated_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removal_reason = models.CharField(max_length=48, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    imported_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("player", "provider_activity_id"), name="accounts_player_activity_unique"
            ),
        ]
        indexes = [models.Index(fields=("player", "imported_at"))]

    def __str__(self) -> str:
        return self.title or self.provider_activity_id


class StravaSyncState(models.Model):
    """Durable, non-secret progress for one player's activity import."""

    class Status(models.TextChoices):
        IDLE = "idle", "Idle"
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        PAUSED = "paused", "Paused"
        FAILED = "failed", "Failed"

    player = models.OneToOneField(
        "accounts.Player", on_delete=models.CASCADE, related_name="strava_sync_state"
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.IDLE)
    mode = models.CharField(max_length=16, default="incremental")
    history_start = models.DateTimeField(null=True, blank=True)
    cursor_page = models.PositiveIntegerField(default=1)
    imported_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    processed_count = models.PositiveIntegerField(default=0)
    last_provider_updated_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=240, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)
    lease_token = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Strava sync state for player {self.player_id}"


class StravaSyncJob(models.Model):
    """Bounded queue item for a resumable list/detail import or webhook."""

    class Kind(models.TextChoices):
        INITIAL = "initial", "Initial history"
        FULL_HISTORY = "full-history", "Full history"
        INCREMENTAL = "incremental", "Incremental"
        WEBHOOK = "webhook", "Webhook"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    player = models.ForeignKey(
        "accounts.Player", on_delete=models.CASCADE, related_name="strava_sync_jobs"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    idempotency_key = models.CharField(max_length=160, unique=True)
    webhook_event = models.ForeignKey(
        "accounts.StravaWebhookEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sync_jobs",
    )
    page = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    retryable = models.BooleanField(default=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.CharField(max_length=240, blank=True)
    lease_token = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    dispatch_token = models.CharField(max_length=64, blank=True)
    dispatch_lease_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("status", "next_attempt_at"))]

    def __str__(self) -> str:
        return f"Strava sync {self.kind} for player {self.player_id}"


class StravaQuotaState(models.Model):
    """Shared provider quota reservation state for all synchronization workers."""

    key = models.CharField(max_length=32, primary_key=True, default="global")
    short_window_used = models.PositiveIntegerField(default=0)
    daily_used = models.PositiveIntegerField(default=0)
    short_window_limit = models.PositiveIntegerField(default=100)
    daily_limit = models.PositiveIntegerField(default=1000)
    read_short_window_used = models.PositiveIntegerField(default=0)
    read_daily_used = models.PositiveIntegerField(default=0)
    read_short_window_limit = models.PositiveIntegerField(default=100)
    read_daily_limit = models.PositiveIntegerField(default=1000)
    short_window_reset_at = models.DateTimeField(null=True, blank=True)
    daily_reset_at = models.DateTimeField(null=True, blank=True)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    in_flight = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Strava quota {self.key}"


class StravaQuotaReservation(models.Model):
    """Expiring reservation preventing dead workers from consuming quota forever."""

    token = models.CharField(max_length=64, primary_key=True)
    expires_at = models.DateTimeField()
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=("expires_at", "released_at"))]

    def __str__(self) -> str:
        return f"Strava quota reservation {self.token}"


class StravaWebhookEvent(models.Model):
    """Idempotency and audit record for a verified provider event."""

    event_key = models.CharField(max_length=64, unique=True)
    player = models.ForeignKey(
        "accounts.Player",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="strava_webhook_events",
    )
    subscription_id = models.PositiveBigIntegerField(null=True, blank=True)
    object_id = models.PositiveBigIntegerField()
    owner_athlete_id = models.PositiveBigIntegerField()
    aspect_type = models.CharField(max_length=24)
    object_type = models.CharField(max_length=24, default="activity")
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=240, blank=True)
    expires_at = models.DateTimeField(default=webhook_event_expiry)

    def __str__(self) -> str:
        return f"Strava webhook {self.event_key}"


class CompetitionResult(models.Model):
    """Derived game data; deleting membership removes only this projection."""

    competition = models.ForeignKey(Competition, on_delete=models.CASCADE, related_name="results")
    player = models.ForeignKey(
        "accounts.Player", on_delete=models.CASCADE, related_name="competition_results"
    )
    activity = models.ForeignKey(
        ImportedActivity,
        on_delete=models.CASCADE,
        related_name="competition_results",
        null=True,
        blank=True,
    )
    points = models.IntegerField(default=0)
    computed_revision = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("competition", "player", "activity"),
                name="accounts_competition_result_unique",
            ),
        ]
        indexes = [models.Index(fields=("competition", "player"))]

    def __str__(self) -> str:
        return f"{self.competition_id}: {self.player_id} ({self.points})"


class CompetitionRecomputation(models.Model):
    """Durable work marker for deterministic score recomputation."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    competition = models.ForeignKey(
        Competition, on_delete=models.CASCADE, related_name="recomputations"
    )
    generation = models.PositiveBigIntegerField()
    affected_player_id = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=240, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    dispatch_token = models.CharField(max_length=64, blank=True)
    dispatch_lease_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("competition", "generation"), name="accounts_competition_generation_unique"
            ),
        ]
        indexes = [models.Index(fields=("status", "created_at"))]

    def __str__(self) -> str:
        return f"Recompute {self.competition_id} generation {self.generation}"


class PlayerCredential(models.Model):
    player = models.OneToOneField(Player, on_delete=models.CASCADE, related_name="credential")
    access_token = EncryptedSecretField()
    refresh_token = EncryptedSecretField()
    expires_at = models.DateTimeField()
    scopes = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Credentials for player {self.player_id}"


class PlayerIdentityGuard(models.Model):
    """Non-content synchronization state keyed by a one-way athlete digest."""

    identity_digest = models.CharField(max_length=64, unique=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=("invalidated_at",))]

    def __str__(self) -> str:
        return f"Player identity guard {self.pk}"


class RevocationJob(models.Model):
    """Bounded, encrypted retry state for provider deauthorization."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        FAILED = "failed", "Failed"

    access_token = EncryptedSecretField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def __str__(self) -> str:
        return f"Provider revocation job {self.pk}"


class PlayerDeletionTombstone(models.Model):
    """Bounded non-content proof that a player deletion completed."""

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"

    event_id = models.UUIDField(default=uuid4, unique=True, editable=False)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.COMPLETED, editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=("expires_at",))]

    def __str__(self) -> str:
        return f"Player deletion {self.event_id}"


class OAuthState(models.Model):
    """Single-use, session-bound state; only a digest is persisted."""

    state_digest = models.CharField(max_length=64, unique=True)
    session_key = models.CharField(max_length=40)
    player = models.ForeignKey(
        Player, on_delete=models.CASCADE, null=True, blank=True, related_name="oauth_states"
    )
    player_session_epoch = models.PositiveBigIntegerField(null=True, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"OAuth state {self.pk}"

    @classmethod
    def issue(
        cls,
        session_key: str,
        *,
        player: Player | None = None,
    ) -> tuple[OAuthState, str]:
        raw = secrets.token_urlsafe(32)
        state = cls.objects.create(
            state_digest=hashlib.sha256(raw.encode()).hexdigest(),
            session_key=session_key,
            player=player,
            player_session_epoch=player.session_epoch if player else None,
            expires_at=timezone.now() + OAUTH_STATE_TTL,
        )
        return state, raw

    @staticmethod
    def digest(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()
