"""Celery maintenance tasks for reports."""

from __future__ import annotations

from typing import Any

from celery import shared_task  # type: ignore[import-untyped]

from .services import retain_closed_reports


@shared_task(name="bikemapy.reports.retain_closed_reports")  # type: ignore[untyped-decorator]
def retain_closed_reports_task() -> dict[str, Any]:
    return retain_closed_reports()
