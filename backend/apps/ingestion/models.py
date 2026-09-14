"""Durable state for incremental and historical BikeForum crawling."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db import models
from django.utils import timezone

from .cache_policy import configured_body_retention_seconds


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


class ExtractionStatus(models.TextChoices):
    """Lifecycle of one isolated GPX extraction attempt."""

    QUEUED = "queued", "Queued"
    PROCESSING = "processing", "Processing"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    BLOCKED = "blocked", "Blocked by activation gate"
    SUPERSEDED = "superseded", "Superseded"


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
    """Short-lived replay body plus durable metadata for conditional requests.

    ``body`` is raw upstream HTML and is cleared by the scheduled retention
    task. URL, redirect, status, validators, checksum, fetch time, and the
    body-specific expiry deadline are retained as crawler metadata so a later
    request can still use conditional HTTP semantics without retaining page
    content. A validator refresh never extends the body deadline. Empty
    responses have no body deadline and are excluded from expiry indexing.
    """

    url = models.URLField(max_length=1000, unique=True)
    final_url = models.URLField(max_length=1000, blank=True)
    status_code = models.PositiveSmallIntegerField()
    body = models.TextField(blank=True)
    body_expires_at = models.DateTimeField(blank=True, null=True)
    etag = models.CharField(max_length=500, blank=True)
    last_modified = models.CharField(max_length=255, blank=True)
    checksum = models.CharField(max_length=128, blank=True)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(fields=["fetched_at"], name="ingestion_c_fetched_6f7f6a_idx"),
            models.Index(
                fields=["body_expires_at", "id"],
                name="ingestion_c_body_expiry_idx",
                condition=models.Q(body_expires_at__isnull=False),
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(body="", body_expires_at__isnull=True)
                    | models.Q(body__gt="", body_expires_at__isnull=False)
                ),
                name="ingestion_cache_body_expiry_consistent",
            )
        ]

    def __str__(self) -> str:
        return self.url

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.body and self.body_expires_at is None:
            retention_seconds = configured_body_retention_seconds()
            self.body_expires_at = self.fetched_at + timedelta(seconds=retention_seconds)
        elif not self.body:
            self.body_expires_at = None
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "body_expires_at" not in update_fields:
            # Keep the application-level invariant intact for callers that
            # optimize writes with update_fields while changing the body.
            kwargs["update_fields"] = set(update_fields) | {"body_expires_at"}
        super().save(*args, **kwargs)


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


class ExtractionAttempt(models.Model):
    """Durable GPX work item and diagnostics for retries and review.

    The source URL is copied at creation time so that an audit record remains
    useful even when the source is subsequently unavailable.  Route sources
    themselves are protected from deletion, but this model deliberately owns
    only the processing history, not route content.
    """

    source = models.ForeignKey(
        "catalogue.RouteSource", on_delete=models.PROTECT, related_name="extraction_attempts"
    )
    version = models.ForeignKey(
        "catalogue.RouteVersion",
        on_delete=models.PROTECT,
        related_name="extraction_attempts",
        blank=True,
        null=True,
    )
    source_url = models.URLField(max_length=1000)
    status = models.CharField(
        max_length=20, choices=ExtractionStatus.choices, default=ExtractionStatus.QUEUED
    )
    attempt_number = models.PositiveIntegerField(default=1)
    diagnostics = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    checksum = models.CharField(max_length=128, blank=True)
    dispatch_attempts = models.PositiveIntegerField(default=0)
    dispatched_at = models.DateTimeField(blank=True, null=True)
    dispatch_task_id = models.CharField(max_length=255, blank=True)
    dispatch_error = models.TextField(blank=True)
    dispatch_claim_token = models.CharField(max_length=64, blank=True)
    dispatch_claimed_until = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "attempt_number"], name="ing_extract_source_attempt_unique"
            )
        ]
        indexes = [
            models.Index(fields=["source", "status"], name="ing_extract_source_status_idx"),
            models.Index(fields=["status", "created_at"], name="ing_extract_status_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.status}: {self.source_url} (attempt {self.attempt_number})"


class OrphanPayloadStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    FAILED = "failed", "Failed"
    COMPLETED = "completed", "Completed"
    EXHAUSTED = "exhausted", "Retry limit exhausted"


class OrphanPayloadCleanup(models.Model):
    """Durable cleanup work for a payload saved before a failed DB commit."""

    source = models.ForeignKey(
        "catalogue.RouteSource", on_delete=models.PROTECT, related_name="orphan_payloads"
    )
    attempt = models.ForeignKey(
        ExtractionAttempt, on_delete=models.PROTECT, related_name="orphan_payloads"
    )
    storage_key = models.CharField(max_length=1000)
    status = models.CharField(
        max_length=20, choices=OrphanPayloadStatus.choices, default=OrphanPayloadStatus.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    next_retry_at = models.DateTimeField(blank=True, null=True)
    claim_token = models.CharField(max_length=64, blank=True)
    claimed_until = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    last_attempt_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [
            models.Index(fields=["status", "created_at"], name="ing_orphan_status_created_idx"),
            models.Index(
                fields=["status", "next_retry_at", "created_at"],
                name="ing_orphan_reconcile_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.status}: {self.storage_key}"


class OrphanPayloadReconciliationState(models.Model):
    """Durable round-robin cursor preventing one cleanup class from starving."""

    class Bucket(models.TextChoices):
        PENDING = OrphanPayloadStatus.PENDING, "Pending"
        FAILED = OrphanPayloadStatus.FAILED, "Failed"

    last_selected_bucket = models.CharField(
        max_length=20, choices=Bucket.choices, default=Bucket.FAILED
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"orphan cleanup cursor: {self.last_selected_bucket}"
