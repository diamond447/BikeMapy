"""Anonymous route reports and their append-only administrative audit trail."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Report(models.Model):
    """A public report awaiting an explicit owner decision.

    No network address or browser fingerprint is stored here.  The duplicate
    fingerprint is a one-way digest of the submitted report text and route,
    and is used only to suppress accidental repeated submissions.
    """

    class Reason(models.TextChoices):
        INCORRECT_ROUTE = "incorrect_route", "Incorrect route"
        SOURCE_ATTRIBUTION = "source_attribution", "Source or attribution"
        AUTHOR_REMOVAL = "author_removal", "Author removal"
        RIGHTS_HOLDER = "rights_holder", "Rights-holder request"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        IN_REVIEW = "in_review", "In review"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        DUPLICATE = "duplicate", "Duplicate"
        CLOSED = "closed", "Closed"

    class Decision(models.TextChoices):
        ACCEPT = "accept", "Accept"
        REJECT = "reject", "Reject"
        DUPLICATE = "duplicate", "Mark duplicate"
        CLOSE = "close", "Close without action"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    route = models.ForeignKey("catalogue.Route", on_delete=models.PROTECT, related_name="reports")
    reason = models.CharField(max_length=32, choices=Reason.choices)
    message = models.TextField(max_length=5000)
    contact_email = models.EmailField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    decision = models.CharField(max_length=20, choices=Decision.choices, blank=True)
    decision_reason = models.TextField(blank=True)
    duplicate_fingerprint = models.CharField(max_length=64, db_index=True)
    submitted_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    decided_at = models.DateTimeField(blank=True, null=True)
    closed_at = models.DateTimeField(blank=True, null=True)
    email_anonymized_at = models.DateTimeField(blank=True, null=True)
    personal_details_anonymized_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["status", "-submitted_at", "-id"]
        indexes = [
            models.Index(
                fields=["route", "duplicate_fingerprint"], name="reports_route_fingerprint_idx"
            ),
            models.Index(fields=["status", "submitted_at"], name="reports_status_submitted_idx"),
            models.Index(fields=["closed_at"], name="reports_closed_at_idx"),
        ]

    def __str__(self) -> str:
        return f"Report {self.id} ({self.get_status_display()})"


class ReportAudit(models.Model):
    """An audit event that never contains report message or contact data."""

    report = models.ForeignKey(Report, on_delete=models.PROTECT, related_name="audit_events")
    event = models.CharField(max_length=40)
    metadata = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="report_audit_events",
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "pk"]

    def __str__(self) -> str:
        return f"{self.report_id}: {self.event}"


# Public aliases keep the domain vocabulary convenient for API and admin
# integrations without duplicating the choices in several modules.
ReportReason = Report.Reason
ReportStatus = Report.Status
ReportDecision = Report.Decision
