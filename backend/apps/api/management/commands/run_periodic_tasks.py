"""Run the Celery beat schedule once, in-process, for lean deployments."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from config.celery import app

CRAWL_TASK = "bikemapy.ingestion.incremental_bikeforum_crawl"


class Command(BaseCommand):
    help = (
        "Run every scheduled maintenance task once without a Celery worker. "
        "Intended for host cron when BACKGROUND_JOBS_ENABLED=false."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--with-crawl",
            action="store_true",
            help="Also run the incremental BikeForum crawl (schedule it daily, not hourly).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        app.loader.import_default_modules()
        failed = []
        for entry in settings.CELERY_BEAT_SCHEDULE.values():
            name = entry["task"]
            if name == CRAWL_TASK and not options["with_crawl"]:
                continue
            result = app.tasks[name].apply()
            if result.failed():
                failed.append(name)
                self.stderr.write(f"{name}: failed ({type(result.result).__name__})")
            else:
                self.stdout.write(f"{name}: ok")
        if failed:
            raise CommandError(f"{len(failed)} periodic task(s) failed")
