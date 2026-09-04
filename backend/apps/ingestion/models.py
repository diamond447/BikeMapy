"""Durable state for incremental and historical BikeForum crawling."""

from __future__ import annotations

from django.db import models
from django.utils import timezone


class CrawlTaskStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    PAUSED = "paused", "Paused"


class CrawlPageStatus(models.TextChoices):
    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    EXHAUSTED = "exhausted", "Retry limit exhausted"


class CrawlTask(models.Model):
    """A bounded crawl execution whose progress is safe to resume."""

    class Kind(models.TextChoices):
        INCREMENTAL = "incremental", "Incremental"
        BACKFILL = "backfill", "Historical backfill"

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.INCREMENTAL)
    status = models.CharField(
        max_length=20, choices=CrawlTaskStatus.choices, default=CrawlTaskStatus.PENDING
    )
    start_url = models.URLField(max_length=1000)
    stream = models.CharField(max_length=120, default="incremental")
    next_url = models.URLField(max_length=1000, blank=True)
    max_pages = models.PositiveIntegerField(default=1)
    pages_processed = models.PositiveIntegerField(default=0)
    items_imported = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)
    lease_until = models.DateTimeField(blank=True, null=True)
    lease_token = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"], name="ingestion_c_status_8db2dd_idx")
        ]

    def __str__(self) -> str:
        return f"{self.kind} crawl #{self.pk} ({self.status})"


class CrawlCheckpoint(models.Model):
    """One cursor per crawl stream, persisted independently of Celery."""

    stream = models.CharField(max_length=120, unique=True)
    next_url = models.URLField(max_length=1000, blank=True)
    page_number = models.PositiveIntegerField(default=0)
    last_successful_at = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True)
    lease_until = models.DateTimeField(blank=True, null=True)
    lease_token = models.CharField(max_length=64, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["stream"]

    def __str__(self) -> str:
        return self.stream


class CrawlResponseCache(models.Model):
    """Small HTTP cache used for conditional requests and replayable crawls."""

    url = models.URLField(max_length=1000, unique=True)
    final_url = models.URLField(max_length=1000, blank=True)
    status_code = models.PositiveSmallIntegerField()
    body = models.TextField(blank=True)
    etag = models.CharField(max_length=500, blank=True)
    last_modified = models.CharField(max_length=255, blank=True)
    checksum = models.CharField(max_length=128, blank=True)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["fetched_at"], name="ingestion_c_fetched_6f7f6a_idx")]

    def __str__(self) -> str:
        return self.url


class CrawlPageWork(models.Model):
    """Durable bounded frontier item, including failed pages for later retry."""

    task = models.ForeignKey(CrawlTask, on_delete=models.CASCADE, related_name="frontier")
    url = models.URLField(max_length=1000)
    status = models.CharField(
        max_length=20, choices=CrawlPageStatus.choices, default=CrawlPageStatus.QUEUED
    )
    attempts = models.PositiveIntegerField(default=0)
    discovered_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)
    lease_until = models.DateTimeField(blank=True, null=True)
    last_error = models.TextField(blank=True)
    status_code = models.PositiveSmallIntegerField(blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "url"], name="ingestion_task_url_unique")
        ]
        indexes = [
            models.Index(
                fields=["task", "status", "discovered_at"], name="ingestion_c_task_id_54a1b5_idx"
            )
        ]

    def __str__(self) -> str:
        return f"{self.status}: {self.url}"
