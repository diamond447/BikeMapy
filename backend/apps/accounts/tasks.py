"""Scheduled lifecycle maintenance for private player accounts."""

from __future__ import annotations

from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

from .services import purge_expired_players, retry_revocations


@shared_task(name="bikemapy.accounts.purge_expired_players")  # type: ignore[untyped-decorator]
def purge_expired_players_task(limit: int = 100) -> dict[str, Any]:
    return purge_expired_players(limit=max(1, limit))


@shared_task(name="bikemapy.accounts.retry_revocations")  # type: ignore[untyped-decorator]
def retry_revocations_task(limit: int = 100) -> dict[str, Any]:
    return retry_revocations(limit=max(1, limit))
