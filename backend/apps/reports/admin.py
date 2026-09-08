"""Owner-only report queue and explicit moderation workflow."""

from __future__ import annotations

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from apps.accounts.admin import owner_admin_site
from apps.catalogue.admin import OwnerModelAdmin

from .models import Report, ReportAudit
from .services import decide_report, mark_report_in_review


@admin.register(Report, site=owner_admin_site)
class ReportAdmin(OwnerModelAdmin):
    list_display = ("id", "route", "reason", "status", "submitted_at", "decided_at", "workflow")
    list_filter = ("status", "reason", "decision")
    search_fields = ("id", "route__slug", "route__display_title", "message", "contact_email")
    readonly_fields = (
        "id",
        "route",
        "reason",
        "message",
        "contact_email",
        "status",
        "decision",
        "decision_reason",
        "duplicate_fingerprint",
        "submitted_at",
        "updated_at",
        "decided_at",
        "closed_at",
        "email_anonymized_at",
        "personal_details_anonymized_at",
    )

    @admin.display(description="Workflow")
    def workflow(self, obj: Report) -> str:
        return format_html(
            '<a href="{}">Review</a>',
            reverse("owner_admin:reports_report_review", args=[obj.pk]),
        )

    def get_urls(self):  # type: ignore[no-untyped-def]
        return [
            path(
                "<path:object_id>/review/",
                self.admin_site.admin_view(self.review_view),
                name="reports_report_review",
            )
        ] + super().get_urls()

    def review_view(self, request: HttpRequest, object_id: str) -> HttpResponse:
        report = self.get_queryset(request).select_related("route").get(pk=object_id)
        if request.method == "POST":
            action = request.POST.get("decision", "")
            reason = request.POST.get("reason", "")
            try:
                if action == "in_review":
                    report = mark_report_in_review(report, actor=request.user)
                else:
                    report = decide_report(
                        report, decision=action, reason=reason, actor=request.user
                    )
            except ValidationError as exc:
                self.message_user(request, str(exc), messages.ERROR)
            else:
                self.message_user(request, "Report workflow recorded.", messages.SUCCESS)
                return HttpResponseRedirect(
                    reverse("owner_admin:reports_report_change", args=[report.pk])
                )
        return TemplateResponse(
            request,
            "admin/reports/review_form.html",
            {**self.admin_site.each_context(request), "title": "Review report", "object": report},
        )


@admin.register(ReportAudit, site=owner_admin_site)
class ReportAuditAdmin(OwnerModelAdmin):
    list_display = ("report", "event", "actor", "created_at")
    list_filter = ("event",)
    search_fields = ("report__id", "event")
    readonly_fields = ("report", "event", "metadata", "actor", "created_at")
