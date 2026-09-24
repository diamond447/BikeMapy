"""Transactional domain operations for private game competitions."""

from __future__ import annotations

import secrets
from datetime import timedelta
from math import sqrt
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Competition,
    CompetitionMembership,
    CompetitionRecomputation,
    CompetitionResult,
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
DISPATCH_RETRY_SECONDS = (30, 120, 600, 1800, 3600)


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
    competition: Competition, *, affected_player_id: int | None = None
) -> CompetitionRecomputation:
    competition.revision += 1
    competition.save(update_fields=("revision", "updated_at"))
    job, _ = CompetitionRecomputation.objects.get_or_create(
        competition=competition,
        generation=competition.revision,
        defaults={"affected_player_id": affected_player_id},
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
    colors = list(competition.memberships.values_list("color", flat=True))
    selected_color = normalize_color(color) if color is not None else available_color(colors)
    ensure_distinguishable(selected_color, colors)
    membership = CompetitionMembership.objects.create(
        competition=competition, player=player, color=selected_color
    )
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
