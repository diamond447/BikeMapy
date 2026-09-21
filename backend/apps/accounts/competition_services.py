"""Transactional domain operations for private game competitions."""

from __future__ import annotations

import secrets
from math import sqrt
from typing import Any

from django.db import transaction

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
MIN_COLOR_DISTANCE = 72


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


def _color_distance(first: str, second: str) -> float:
    left = tuple(int(first[offset : offset + 2], 16) for offset in (1, 3, 5))
    right = tuple(int(second[offset : offset + 2], 16) for offset in (1, 3, 5))
    return sqrt(float(sum((a - b) ** 2 for a, b in zip(left, right, strict=True))))


def ensure_distinguishable(color: str, existing: list[str]) -> None:
    if any(_color_distance(color, candidate) < MIN_COLOR_DISTANCE for candidate in existing):
        raise CompetitionError(
            "That color is too close to a member color in this competition.",
            code="color_not_distinguishable",
        )


def available_color(existing: list[str]) -> str:
    for color in DEFAULT_COLORS:
        if all(_color_distance(color, candidate) >= MIN_COLOR_DISTANCE for candidate in existing):
            return color
    # A deterministic fallback keeps joins possible after the palette is full.
    for red in range(0, 256, 32):
        for green in range(0, 256, 32):
            for blue in range(0, 256, 32):
                candidate = f"#{red:02X}{green:02X}{blue:02X}"
                if all(
                    _color_distance(candidate, value) >= MIN_COLOR_DISTANCE for value in existing
                ):
                    return candidate
    raise CompetitionError("No sufficiently distinct member color is available.", code="no_color")


def _new_invite_code() -> str:
    while True:
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))
        if not Competition.objects.filter(invite_code=code).exists():
            return code


def _membership(player: Player, competition: Competition) -> CompetitionMembership:
    try:
        return CompetitionMembership.objects.get(player=player, competition=competition)
    except CompetitionMembership.DoesNotExist as exc:
        # Deliberately do not distinguish a missing object from a non-member.
        raise CompetitionError("Competition not found.", code="not_found") from exc


def _dispatch_recomputation(job_id: int) -> None:
    try:
        from .tasks import recompute_competition_results_task

        recompute_competition_results_task.delay(job_id)
    except Exception:
        # The durable pending row is the source of truth; a worker can retry it.
        return


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
    transaction.on_commit(lambda: _dispatch_recomputation(job.pk))
    return job


@transaction.atomic
def create_competition(
    player: Player, *, name: Any, color: Any = None
) -> tuple[Competition, CompetitionMembership]:
    selected_color = normalize_color(color) if color is not None else available_color([])
    competition = Competition.objects.create(
        owner=player, name=normalize_name(name), invite_code=_new_invite_code()
    )
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
    membership = _membership(player, competition)
    Player.objects.filter(pk=player.pk).update(active_competition=competition)
    player.active_competition = competition
    return membership


@transaction.atomic
def rename_competition(player: Player, competition: Competition, *, name: Any) -> Competition:
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    _membership(player, locked)
    if locked.owner_id != player.pk:
        raise CompetitionError("Only the competition owner can rename it.", code="owner_required")
    locked.name = normalize_name(name)
    locked.save(update_fields=("name", "updated_at"))
    return locked


@transaction.atomic
def rotate_invite_code(player: Player, competition: Competition) -> Competition:
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    _membership(player, locked)
    if locked.owner_id != player.pk:
        raise CompetitionError(
            "Only the competition owner can rotate the invite code.", code="owner_required"
        )
    locked.invite_code = _new_invite_code()
    locked.save(update_fields=("invite_code", "updated_at"))
    return locked


@transaction.atomic
def set_member_color(
    player: Player, competition: Competition, *, color: Any
) -> CompetitionMembership:
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
        replacement = locked.memberships.exclude(player=member).order_by("joined_at", "pk").first()
        Player.objects.filter(pk=member.pk).update(
            active_competition=replacement.competition_id if replacement else None
        )
    return schedule_recomputation(locked, affected_player_id=member.pk)


@transaction.atomic
def leave_competition(player: Player, competition: Competition) -> CompetitionRecomputation:
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
        replacement = player.competition_memberships.order_by("joined_at", "pk").first()
        Player.objects.filter(pk=player.pk).update(
            active_competition=replacement.competition_id if replacement else None
        )
    return schedule_recomputation(locked, affected_player_id=player.pk)


@transaction.atomic
def transfer_ownership(owner: Player, competition: Competition, new_owner: Player) -> Competition:
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
    locked = Competition.objects.select_for_update().get(pk=competition.pk)
    _membership(owner, locked)
    if locked.owner_id != owner.pk:
        raise CompetitionError("Only the competition owner can delete it.", code="owner_required")
    Player.objects.filter(active_competition=locked).update(active_competition=None)
    locked.delete()
