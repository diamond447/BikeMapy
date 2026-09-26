"""Transactional domain operations for private game competitions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, date, datetime, time, timedelta
from math import sqrt
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Competition,
    CompetitionMembership,
    CompetitionRecomputation,
    CompetitionResult,
    CompetitionSharingConsentAudit,
    ImportedActivity,
    Player,
)

INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INVITE_LENGTH = 10
DEFAULT_COLORS = (
    "#E45756",
    "#3A86FF",
    "#2A9D8F",
    "#F4A261",
    "#9B5DE5",
    "#00B4D8",
    "#E76F51",
    "#577590",
)
MIN_COLOR_DELTA_E = 18.0
MAX_DISPATCH_ATTEMPTS = 5
MAX_INVITE_ATTEMPTS = 5
MAX_COMPETITION_MEMBERS = 100
DISPATCH_RETRY_SECONDS = (30, 120, 600, 1800, 3600)
SHARING_SCOPES = frozenset(
    {
        CompetitionMembership.SharingScope.RECENT,
        CompetitionMembership.SharingScope.FULL_HISTORY,
    }
)
CURRENT_SHARING_DISCLOSURE_VERSION = "2026-09-25"
GAME_TIME_ZONE = ZoneInfo("Europe/Prague")


class CompetitionError(Exception):
    """Safe, user-facing domain error with a stable API code."""

    def __init__(self, detail: str, *, code: str = "invalid") -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


def normalize_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise CompetitionError("Competition name is required.", code="name_required")
    if len(name) > 120:
        raise CompetitionError(
            "Competition name must be 120 characters or fewer.", code="name_too_long"
        )
    return name


def normalize_color(value: Any) -> str:
    color = str(value or "").strip().upper()
    if (
        len(color) != 7
        or color[0] != "#"
        or any(char not in "0123456789ABCDEF" for char in color[1:])
    ):
        raise CompetitionError("Choose a valid six-digit hexadecimal color.", code="invalid_color")
    return color


def _color_lab(color: str) -> tuple[float, float, float]:
    """Convert sRGB to CIE Lab (D65) for perceptual color comparisons."""

    channels = [int(color[offset : offset + 2], 16) / 255 for offset in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    x = (linear[0] * 0.4124 + linear[1] * 0.3576 + linear[2] * 0.1805) / 0.95047
    y = linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
    z = (linear[0] * 0.0193 + linear[1] * 0.1192 + linear[2] * 0.9505) / 1.08883

    def pivot(value: float) -> float:
        return value ** (1 / 3) if value > 0.008856 else 7.787 * value + 16 / 116

    fx, fy, fz = pivot(x), pivot(y), pivot(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _color_delta_e(first: str, second: str) -> float:
    left = _color_lab(first)
    right = _color_lab(second)
    return sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))


def ensure_distinguishable(color: str, existing: list[str]) -> None:
    if any(_color_delta_e(color, candidate) < MIN_COLOR_DELTA_E for candidate in existing):
        raise CompetitionError(
            "That color is too close to a member color in this competition.",
            code="color_not_distinguishable",
        )


def available_color(existing: list[str]) -> str:
    for color in DEFAULT_COLORS:
        if all(_color_delta_e(color, candidate) >= MIN_COLOR_DELTA_E for candidate in existing):
            return color
    # A deterministic fallback keeps joins possible after the palette is full.
    for red in range(0, 256, 32):
        for green in range(0, 256, 32):
            for blue in range(0, 256, 32):
                candidate = f"#{red:02X}{green:02X}{blue:02X}"
                if all(_color_delta_e(candidate, value) >= MIN_COLOR_DELTA_E for value in existing):
                    return candidate
    raise CompetitionError("No sufficiently distinct member color is available.", code="no_color")


def _new_invite_code() -> str:
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))


def _create_with_unique_invite(**fields: Any) -> Competition:
    for _ in range(MAX_INVITE_ATTEMPTS):
        try:
            with transaction.atomic():
                return Competition.objects.create(**fields, invite_code=_new_invite_code())
        except IntegrityError:
            continue
    raise CompetitionError("Could not allocate a unique invite code.", code="invite_unavailable")


def _membership(player: Player, competition: Competition) -> CompetitionMembership:
    try:
        return CompetitionMembership.objects.get(player=player, competition=competition)
    except CompetitionMembership.DoesNotExist as exc:
        # Deliberately do not distinguish a missing object from a non-member.
        raise CompetitionError("Competition not found.", code="not_found") from exc


def sharing_is_active(membership: CompetitionMembership) -> bool:
    return bool(
        membership.sharing_consent_at is not None and membership.sharing_scope in SHARING_SCOPES
    )


def sharing_cutoff_date(at: datetime | None = None) -> date:
    """Return the inclusive start of the previous twelve Prague calendar months."""

    if at is not None and timezone.is_naive(at):
        at = timezone.make_aware(at, GAME_TIME_ZONE)
    local_date = timezone.localtime(at or timezone.now(), GAME_TIME_ZONE).date()
    try:
        return local_date.replace(year=local_date.year - 1)
    except ValueError:  # February 29 in a leap year.
        return local_date.replace(year=local_date.year - 1, day=28)


def activity_calendar_date(activity: ImportedActivity) -> date | None:
    if activity.calendar_date is not None:
        return activity.calendar_date
    if activity.started_at is None:
        return (
            timezone.localtime(activity.imported_at, GAME_TIME_ZONE).date()
            if activity.imported_at is not None
            else None
        )
    return timezone.localtime(activity.started_at, GAME_TIME_ZONE).date()


def activity_is_authorized_for_membership(
    activity: ImportedActivity,
    membership: CompetitionMembership,
    *,
    at: datetime | None = None,
) -> bool:
    """Apply the single sharing policy to one imported activity."""

    if (
        not sharing_is_active(membership)
        or activity.removed_at is not None
        or activity.geometry is None
    ):
        return False
    if membership.sharing_scope == CompetitionMembership.SharingScope.FULL_HISTORY:
        return True
    activity_date = activity_calendar_date(activity)
    return activity_date is not None and activity_date >= sharing_cutoff_date(at)


def authorized_activity_filter(
    memberships: Any,
    *,
    at: datetime | None = None,
) -> Q:
    """Build the DB predicate equivalent of the activity authorization policy."""

    full_history_ids: list[int] = []
    recent_ids: list[int] = []
    for membership in memberships:
        if not sharing_is_active(membership):
            continue
        if membership.sharing_scope == CompetitionMembership.SharingScope.FULL_HISTORY:
            full_history_ids.append(membership.player_id)
        else:
            recent_ids.append(membership.player_id)
    predicate = Q(pk__in=[])
    if full_history_ids:
        predicate |= Q(player_id__in=full_history_ids)
    if recent_ids:
        cutoff = sharing_cutoff_date(at)
        cutoff_start = timezone.make_aware(
            datetime.combine(cutoff, time.min), GAME_TIME_ZONE
        ).astimezone(UTC)
        predicate |= Q(player_id__in=recent_ids) & (
            Q(calendar_date__gte=cutoff)
            | Q(calendar_date__isnull=True, started_at__gte=cutoff_start)
            | Q(
                calendar_date__isnull=True,
                started_at__isnull=True,
                imported_at__gte=cutoff_start,
            )
        )
    return predicate


def authorized_activity_queryset(
    queryset: Any, memberships: Any, *, at: datetime | None = None
) -> Any:
    return queryset.filter(
        authorized_activity_filter(memberships, at=at),
        removed_at__isnull=True,
        geometry__isnull=False,
    )


def competition_member_label(membership: CompetitionMembership) -> str:
    """Return a stable, competition-scoped pseudonym without exposing PII."""

    secret = str(getattr(settings, "SECRET_KEY", "bikemapy-local-secret")).encode()
    message = f"competition-member:{membership.competition_id}:{membership.player_id}".encode()
    digest = hmac.new(secret, message, hashlib.sha256).hexdigest()[:8].upper()
    return f"Rider {digest}"


def _consent_audit_key(kind: str, value: Any) -> str:
    secret = str(getattr(settings, "SECRET_KEY", "bikemapy-local-secret")).encode()
    message = f"competition-sharing-audit:{kind}:{value}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _record_consent_audit(
    membership: CompetitionMembership,
    *,
    action: CompetitionSharingConsentAudit.Action,
    scope: str,
    disclosure_version: str,
) -> None:
    CompetitionSharingConsentAudit.objects.create(
        membership=membership,
        competition_key=_consent_audit_key("competition", membership.competition_id),
        player_key=_consent_audit_key("player", membership.player_id),
        action=action,
        scope=scope,
        disclosure_version=disclosure_version,
    )


@transaction.atomic
def grant_sharing_consent(
    player: Player,
    competition: Competition,
    *,
    scope: Any,
    disclosure_version: Any,
    confirmed: bool,
) -> CompetitionMembership:
    player = _locked_player(player)
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    try:
        membership = CompetitionMembership.objects.select_for_update().get(
            player=player, competition=locked
        )
    except CompetitionMembership.DoesNotExist as exc:
        raise CompetitionError("Competition not found.", code="not_found") from exc
    normalized = str(scope or "").strip().lower()
    if normalized not in SHARING_SCOPES:
        raise CompetitionError(
            "Choose recent or full available history.", code="invalid_sharing_scope"
        )
    version = str(disclosure_version or "").strip()
    if version != CURRENT_SHARING_DISCLOSURE_VERSION:
        raise CompetitionError(
            "This sharing disclosure is out of date. Review the current disclosure and try again.",
            code="disclosure_out_of_date",
        )
    if not confirmed:
        raise CompetitionError(
            "Confirm that you understand the activity sharing disclosure.",
            code="disclosure_confirmation_required",
        )
    previous_scope = membership.sharing_scope
    membership.sharing_scope = normalized
    membership.sharing_consent_at = timezone.now()
    membership.sharing_disclosure_version = version
    membership.save(
        update_fields=(
            "sharing_scope",
            "sharing_consent_at",
            "sharing_disclosure_version",
            "updated_at",
        )
    )
    _record_consent_audit(
        membership,
        action=CompetitionSharingConsentAudit.Action.GRANTED,
        scope=normalized,
        disclosure_version=version,
    )
    if previous_scope != normalized:
        from apps.reference_routes.completion_services import reset_competition_completion_data

        reset_competition_completion_data(locked, player_id=player.pk)
    schedule_recomputation(locked, affected_player_id=player.pk, bump_revision=False)
    return membership


@transaction.atomic
def withdraw_sharing_consent(player: Player, competition: Competition) -> CompetitionMembership:
    player = _locked_player(player)
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    try:
        membership = CompetitionMembership.objects.select_for_update().get(
            player=player, competition=locked
        )
    except CompetitionMembership.DoesNotExist as exc:
        raise CompetitionError("Competition not found.", code="not_found") from exc
    previous_scope = membership.sharing_scope
    membership.sharing_scope = CompetitionMembership.SharingScope.NONE
    membership.sharing_consent_at = None
    membership.sharing_disclosure_version = ""
    membership.save(
        update_fields=(
            "sharing_scope",
            "sharing_consent_at",
            "sharing_disclosure_version",
            "updated_at",
        )
    )
    _record_consent_audit(
        membership,
        action=CompetitionSharingConsentAudit.Action.WITHDRAWN,
        scope=previous_scope,
        disclosure_version=CURRENT_SHARING_DISCLOSURE_VERSION,
    )
    CompetitionResult.objects.filter(competition=locked, player=player).delete()
    from apps.reference_routes.completion_services import reset_competition_completion_data

    reset_competition_completion_data(locked, player_id=player.pk)
    schedule_recomputation(locked, affected_player_id=player.pk, bump_revision=False)
    return membership


def _locked_player(player: Player) -> Player:
    """Global lock order starts with Player, then Competition, then members."""

    return Player.objects.select_for_update().get(pk=player.pk)


def _claim_recomputation_dispatch(job_id: int | None = None) -> tuple[int, str] | None:
    """Atomically claim an outbox row before publishing it to the broker."""

    now = timezone.now()
    stale_dispatch = now - timedelta(minutes=10)
    candidate_ids = (
        [job_id]
        if job_id is not None
        else list(
            CompetitionRecomputation.objects.filter(
                Q(status=CompetitionRecomputation.Status.PENDING)
                | Q(status=CompetitionRecomputation.Status.FAILED),
                attempts__lt=MAX_DISPATCH_ATTEMPTS,
                next_attempt_at__lte=now,
            )
            .filter(Q(dispatched_at__isnull=True) | Q(dispatched_at__lt=stale_dispatch))
            .order_by("created_at", "pk")
            .values_list("pk", flat=True)[:100]
        )
    )
    for candidate_id in candidate_ids:
        with transaction.atomic():
            try:
                job_ref = CompetitionRecomputation.objects.get(pk=candidate_id)
                # Keep the worker and service lock order consistent.
                Competition.objects.select_for_update().get(pk=job_ref.competition_id)
                job = CompetitionRecomputation.objects.select_for_update().get(pk=candidate_id)
            except (Competition.DoesNotExist, CompetitionRecomputation.DoesNotExist):
                continue
            if job.status not in {
                CompetitionRecomputation.Status.PENDING,
                CompetitionRecomputation.Status.FAILED,
            }:
                continue
            if job.attempts >= MAX_DISPATCH_ATTEMPTS or job.next_attempt_at > now:
                continue
            if job.dispatched_at is not None and job.dispatched_at >= stale_dispatch:
                continue
            if job.dispatch_token and job.dispatch_lease_until and job.dispatch_lease_until > now:
                continue
            token = uuid4().hex
            job.dispatch_token = token
            job.dispatch_lease_until = now + timedelta(
                seconds=settings.GAME_RECOMPUTATION_DISPATCH_LEASE_SECONDS
            )
            job.save(update_fields=("dispatch_token", "dispatch_lease_until"))
            return job.pk, token
    return None


def _dispatch_recomputation(job_id: int, *, dispatch_token: str | None = None) -> bool:
    claim = (job_id, dispatch_token) if dispatch_token else _claim_recomputation_dispatch(job_id)
    if claim is None:
        return False
    claimed_job_id, token = claim
    try:
        from .tasks import recompute_competition_results_task

        recompute_competition_results_task.apply_async(args=(claimed_job_id,))
    except Exception as exc:
        try:
            with transaction.atomic():
                job = CompetitionRecomputation.objects.select_for_update().get(pk=claimed_job_id)
                if job.dispatch_token != token:
                    return False
                job.attempts += 1
                job.error = str(exc)[:240]
                if job.attempts >= MAX_DISPATCH_ATTEMPTS:
                    job.status = CompetitionRecomputation.Status.FAILED
                else:
                    retry_index = min(job.attempts - 1, len(DISPATCH_RETRY_SECONDS) - 1)
                    job.status = CompetitionRecomputation.Status.PENDING
                    job.next_attempt_at = timezone.now() + timedelta(
                        seconds=DISPATCH_RETRY_SECONDS[retry_index]
                    )
                job.dispatch_token = ""
                job.dispatch_lease_until = None
                job.save(
                    update_fields=(
                        "attempts",
                        "status",
                        "error",
                        "next_attempt_at",
                        "dispatch_token",
                        "dispatch_lease_until",
                    )
                )
        except CompetitionRecomputation.DoesNotExist:
            # A concurrent competition deletion cascades the outbox row. The
            # publication already failed, so there is nothing left to retry.
            return False
        return False
    with transaction.atomic():
        CompetitionRecomputation.objects.filter(pk=claimed_job_id, dispatch_token=token).update(
            dispatched_at=timezone.now(), dispatch_token="", dispatch_lease_until=None
        )
    return True


def schedule_recomputation(
    competition: Competition,
    *,
    affected_player_id: int | None = None,
    bump_revision: bool = True,
) -> CompetitionRecomputation:
    if bump_revision:
        competition.revision += 1
        competition.save(update_fields=("revision", "updated_at"))
    job, _ = CompetitionRecomputation.objects.get_or_create(
        competition=competition,
        generation=competition.revision,
        defaults={"affected_player_id": affected_player_id},
    )
    if job.affected_player_id != affected_player_id:
        job.affected_player_id = affected_player_id
    if job.status != CompetitionRecomputation.Status.PENDING:
        job.status = CompetitionRecomputation.Status.PENDING
        job.completed_at = None
        job.lease_token = ""
        job.lease_until = None
        job.dispatch_token = ""
        job.dispatch_lease_until = None
        job.next_attempt_at = timezone.now()
        job.dispatched_at = None
        job.error = ""
    job.save(
        update_fields=(
            "affected_player_id",
            "status",
            "completed_at",
            "lease_token",
            "lease_until",
            "dispatch_token",
            "dispatch_lease_until",
            "next_attempt_at",
            "dispatched_at",
            "error",
        )
    )
    from apps.reference_routes.completion_services import schedule_competition_completions

    schedule_competition_completions(
        competition, reason="competition-membership-or-activity-change"
    )
    return job


@transaction.atomic
def create_competition(
    player: Player, *, name: Any, color: Any = None
) -> tuple[Competition, CompetitionMembership]:
    player = _locked_player(player)
    selected_color = normalize_color(color) if color is not None else available_color([])
    competition = _create_with_unique_invite(owner=player, name=normalize_name(name))
    membership = CompetitionMembership.objects.create(
        competition=competition, player=player, color=selected_color
    )
    from apps.reference_routes.completion_services import schedule_competition_completions

    schedule_competition_completions(competition, reason="competition-created")
    if player.active_competition_id is None:
        Player.objects.filter(pk=player.pk).update(active_competition=competition)
        player.active_competition = competition
    return competition, membership


@transaction.atomic
def join_competition(
    player: Player, *, invite_code: Any, color: Any = None
) -> tuple[Competition, CompetitionMembership]:
    player = _locked_player(player)
    code = str(invite_code or "").strip().upper()
    try:
        competition = Competition.objects.select_for_update().get(invite_code=code, is_active=True)
    except Competition.DoesNotExist as exc:
        raise CompetitionError("That invite code is not valid.", code="invalid_invite") from exc
    if CompetitionMembership.objects.filter(competition=competition, player=player).exists():
        raise CompetitionError("You already belong to this competition.", code="already_member")
    has_reached_member_limit = competition.memberships.values("pk")[
        MAX_COMPETITION_MEMBERS - 1 : MAX_COMPETITION_MEMBERS
    ].exists()
    if has_reached_member_limit:
        raise CompetitionError(
            "This competition already has the maximum number of members.",
            code="member_limit",
        )
    colors = list(competition.memberships.values_list("color", flat=True))
    selected_color = normalize_color(color) if color is not None else available_color(colors)
    ensure_distinguishable(selected_color, colors)
    membership = CompetitionMembership.objects.create(
        competition=competition, player=player, color=selected_color
    )
    from apps.reference_routes.completion_services import schedule_competition_completions

    schedule_competition_completions(competition, reason="competition-member-joined")
    if player.active_competition_id is None:
        Player.objects.filter(pk=player.pk).update(active_competition=competition)
        player.active_competition = competition
    return competition, membership


@transaction.atomic
def switch_competition(player: Player, competition: Competition) -> CompetitionMembership:
    player = Player.objects.select_for_update().get(pk=player.pk)
    competition = Competition.objects.select_for_update().get(pk=competition.pk)
    membership = _membership(player, competition)
    Player.objects.filter(pk=player.pk).update(active_competition=competition)
    player.active_competition = competition
    return membership


@transaction.atomic
def rename_competition(player: Player, competition: Competition, *, name: Any) -> Competition:
    player = _locked_player(player)
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    _membership(player, locked)
    if locked.owner_id != player.pk:
        raise CompetitionError("Only the competition owner can rename it.", code="owner_required")
    locked.name = normalize_name(name)
    locked.save(update_fields=("name", "updated_at"))
    return locked


@transaction.atomic
def rotate_invite_code(player: Player, competition: Competition) -> Competition:
    player = _locked_player(player)
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    _membership(player, locked)
    if locked.owner_id != player.pk:
        raise CompetitionError(
            "Only the competition owner can rotate the invite code.", code="owner_required"
        )
    for _ in range(MAX_INVITE_ATTEMPTS):
        locked.invite_code = _new_invite_code()
        try:
            with transaction.atomic():
                locked.save(update_fields=("invite_code", "updated_at"))
            break
        except IntegrityError:
            continue
    else:
        raise CompetitionError(
            "Could not allocate a unique invite code.", code="invite_unavailable"
        )
    return locked


@transaction.atomic
def set_member_color(
    player: Player, competition: Competition, *, color: Any
) -> CompetitionMembership:
    player = _locked_player(player)
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    membership = _membership(player, locked)
    selected = normalize_color(color)
    existing = list(locked.memberships.exclude(pk=membership.pk).values_list("color", flat=True))
    ensure_distinguishable(selected, existing)
    membership.color = selected
    membership.save(update_fields=("color", "updated_at"))
    return membership


@transaction.atomic
def remove_member(
    owner: Player, competition: Competition, member: Player
) -> CompetitionRecomputation:
    member = Player.objects.select_for_update().get(pk=member.pk)
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    _membership(owner, locked)
    if locked.owner_id != owner.pk:
        raise CompetitionError(
            "Only the competition owner can remove members.", code="owner_required"
        )
    if member.pk == owner.pk:
        raise CompetitionError(
            "Transfer ownership before removing yourself.", code="owner_cannot_leave"
        )
    try:
        membership = CompetitionMembership.objects.get(competition=locked, player=member)
    except CompetitionMembership.DoesNotExist as exc:
        raise CompetitionError("Competition not found.", code="not_found") from exc
    CompetitionResult.objects.filter(competition=locked, player=member).delete()
    from apps.reference_routes.completion_services import reset_competition_completion_data

    reset_competition_completion_data(locked, player_id=member.pk)
    membership.delete()
    if member.active_competition_id == locked.pk:
        replacement = (
            CompetitionMembership.objects.select_for_update()
            .filter(player=member)
            .exclude(competition=locked)
            .order_by("joined_at", "pk")
            .first()
        )
        Player.objects.filter(pk=member.pk).update(
            active_competition=replacement.competition_id if replacement else None
        )
    return schedule_recomputation(locked, affected_player_id=member.pk)


@transaction.atomic
def leave_competition(player: Player, competition: Competition) -> CompetitionRecomputation:
    player = Player.objects.select_for_update().get(pk=player.pk)
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    _membership(player, locked)
    if locked.owner_id == player.pk:
        raise CompetitionError(
            "Transfer ownership or delete the competition before leaving.",
            code="owner_cannot_leave",
        )
    CompetitionResult.objects.filter(competition=locked, player=player).delete()
    from apps.reference_routes.completion_services import reset_competition_completion_data

    reset_competition_completion_data(locked, player_id=player.pk)
    CompetitionMembership.objects.filter(competition=locked, player=player).delete()
    if player.active_competition_id == locked.pk:
        replacement = (
            CompetitionMembership.objects.select_for_update()
            .filter(player=player)
            .exclude(competition=locked)
            .order_by("joined_at", "pk")
            .first()
        )
        Player.objects.filter(pk=player.pk).update(
            active_competition=replacement.competition_id if replacement else None
        )
    return schedule_recomputation(locked, affected_player_id=player.pk)


@transaction.atomic
def transfer_ownership(owner: Player, competition: Competition, new_owner: Player) -> Competition:
    locked_players = list(
        Player.objects.select_for_update().filter(pk__in=(owner.pk, new_owner.pk)).order_by("pk")
    )
    owner = next(player for player in locked_players if player.pk == owner.pk)
    new_owner = next(player for player in locked_players if player.pk == new_owner.pk)
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    _membership(owner, locked)
    if locked.owner_id != owner.pk:
        raise CompetitionError(
            "Only the competition owner can transfer ownership.", code="owner_required"
        )
    _membership(new_owner, locked)
    locked.owner_id = new_owner.pk
    locked.save(update_fields=("owner", "updated_at"))
    return locked


@transaction.atomic
def delete_competition(owner: Player, competition: Competition) -> None:
    # Lock every player that can be updated before taking the competition lock;
    # this matches switch/remove and prevents Player↔Competition deadlocks.
    locked_players = list(
        Player.objects.select_for_update()
        .filter(Q(active_competition=competition) | Q(pk=owner.pk))
        .order_by("pk")
    )
    owner = next(player for player in locked_players if player.pk == owner.pk)
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    _membership(owner, locked)
    if locked.owner_id != owner.pk:
        raise CompetitionError("Only the competition owner can delete it.", code="owner_required")
    Player.objects.filter(active_competition=locked).update(active_competition=None)
    locked.delete()
