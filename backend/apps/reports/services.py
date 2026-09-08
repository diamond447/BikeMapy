"""Report submission, moderation, protection, and retention use cases."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.core.cache.backends.base import BaseCache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.catalogue.models import Route

from .models import Report, ReportAudit
from .turnstile import TurnstileVerifier


class ReportProtectionError(Exception):
    """Base class for a public submission rejected before persistence."""


class TurnstileRejected(ReportProtectionError):
    pass


class HoneypotTriggered(ReportProtectionError):
    pass


class DuplicateReport(ReportProtectionError):
    pass


class RateLimited(ReportProtectionError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("Report submission rate limit reached.")


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int = 0


def client_ip(request: Any) -> str:
    """Resolve a client IP with explicit, CIDR-scoped Cloudflare trust.

    Direct deployments use ``REMOTE_ADDR``.  Cloudflare Tunnel deployments can
    opt into ``CF-Connecting-IP`` only when the direct peer is in the configured
    trusted proxy CIDRs.  A malformed or multi-value header is rejected rather
    than interpreted, and all fallbacks remain suitable for HMAC rate keys.
    """

    meta = getattr(request, "META", {})

    def parse(value: Any) -> str | None:
        value = str(value or "").strip()
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return None

    peer = parse(meta.get("REMOTE_ADDR"))
    if peer and str(getattr(settings, "REPORT_CLIENT_IP_MODE", "direct")).lower() == "cloudflare":
        try:
            networks = tuple(
                ipaddress.ip_network(str(cidr).strip(), strict=False)
                for cidr in getattr(settings, "REPORT_TRUSTED_PROXY_CIDRS", ())
                if str(cidr).strip()
            )
        except ValueError:
            networks = ()
        if any(ipaddress.ip_address(peer) in network for network in networks):
            header = meta.get("HTTP_CF_CONNECTING_IP")
            if header is not None:
                return parse(header) or "unknown"
    return peer or "unknown"


def rate_limit_identifier(ip_address: str) -> str:
    """Return an HMAC identifier suitable for short-lived cache keys."""

    secret = str(
        getattr(settings, "REPORT_RATE_LIMIT_HMAC_SECRET", "")
        or getattr(settings, "SECRET_KEY", "local-development-key")
    ).encode()
    return hmac.new(secret, ip_address.encode(), hashlib.sha256).hexdigest()


# Names used by integration callers and tests that want to inspect the adapter.
derive_rate_limit_identifier = rate_limit_identifier


def _increment(key: str, timeout: int, backend: BaseCache) -> int:
    if backend.add(key, 1, timeout=timeout):
        return 1
    try:
        return int(backend.incr(key))
    except ValueError:
        backend.set(key, 1, timeout=timeout)
        return 1


def check_rate_limits(
    ip_address: str, *, cache_backend: BaseCache | None = None
) -> RateLimitResult:
    """Atomically count one attempt in separate hourly and daily windows."""

    identifier = rate_limit_identifier(ip_address)
    hourly_limit = max(1, int(getattr(settings, "REPORT_RATE_LIMIT_HOURLY", 3)))
    daily_limit = max(1, int(getattr(settings, "REPORT_RATE_LIMIT_DAILY", 10)))
    backend = cache_backend or cache
    try:
        hour_count = _increment(f"reports:rate:v1:{identifier}:hour", 3600, backend)
        day_count = _increment(f"reports:rate:v1:{identifier}:day", 86400, backend)
    except Exception as exc:
        # An unavailable limiter must fail closed.  Do not accept an
        # unprotected report merely because Redis is temporarily unavailable.
        raise RateLimited(300) from exc
    if hour_count > hourly_limit:
        return RateLimitResult(False, 3600)
    if day_count > daily_limit:
        return RateLimitResult(False, 86400)
    return RateLimitResult(True)


def report_fingerprint(route: Route, reason: str, message: str) -> str:
    canonical = "|".join((str(route.pk), reason.strip().lower(), " ".join(message.split()).lower()))
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify_turnstile(token: str, request: Any) -> bool:
    return TurnstileVerifier().verify(token, remote_ip=client_ip(request))


@transaction.atomic
def submit_report(
    route: Route,
    *,
    reason: str,
    message: str,
    contact_email: str = "",
    turnstile_token: str,
    honeypot: str = "",
    request: Any,
) -> Report:
    if honeypot.strip():
        raise HoneypotTriggered("Automated submission detected.")
    if not verify_turnstile(turnstile_token, request):
        raise TurnstileRejected("Turnstile verification failed.")
    limits = check_rate_limits(client_ip(request))
    if not limits.allowed:
        raise RateLimited(limits.retry_after)
    # Serialize duplicate checks for one route without changing any route
    # content or lifecycle state.  This closes the race between two identical
    # submissions arriving at the same time.
    route = Route.objects.select_for_update().get(pk=route.pk)
    fingerprint = report_fingerprint(route, reason, message)
    since = timezone.now() - timedelta(
        seconds=max(1, int(getattr(settings, "REPORT_DUPLICATE_WINDOW", 86400)))
    )
    if Report.objects.filter(
        route=route, duplicate_fingerprint=fingerprint, submitted_at__gte=since
    ).exists():
        raise DuplicateReport("A matching report was already received recently.")
    report = Report.objects.create(
        route=route,
        reason=reason,
        message=message,
        contact_email=contact_email,
        duplicate_fingerprint=fingerprint,
    )
    ReportAudit.objects.create(
        report=report,
        event="submitted",
        metadata={"reason": reason, "route_id": str(route.pk)},
    )
    return report


@transaction.atomic
def decide_report(report: Report, *, decision: str, reason: str, actor: Any = None) -> Report:
    if decision not in Report.Decision.values:
        raise ValidationError("Unknown report decision.")
    if not reason.strip():
        raise ValidationError("A report decision reason is required.")
    report = Report.objects.select_for_update().get(pk=report.pk)
    if report.status in {
        Report.Status.CLOSED,
        Report.Status.ACCEPTED,
        Report.Status.REJECTED,
        Report.Status.DUPLICATE,
    }:
        raise ValidationError("This report has already been decided.")
    status_by_decision: dict[str, str] = {
        Report.Decision.ACCEPT: Report.Status.ACCEPTED,
        Report.Decision.REJECT: Report.Status.REJECTED,
        Report.Decision.DUPLICATE: Report.Status.DUPLICATE,
        Report.Decision.CLOSE: Report.Status.CLOSED,
    }
    now = timezone.now()
    report.decision = decision
    report.decision_reason = reason
    report.status = status_by_decision[decision]
    report.decided_at = now
    report.closed_at = now
    report.save(
        update_fields=[
            "decision",
            "decision_reason",
            "status",
            "decided_at",
            "closed_at",
            "updated_at",
        ]
    )
    ReportAudit.objects.create(
        report=report,
        event="decision",
        metadata={"decision": decision, "status": report.status},
        actor=actor,
    )
    return report


def mark_report_in_review(report: Report, *, actor: Any = None) -> Report:
    with transaction.atomic():
        report = Report.objects.select_for_update().get(pk=report.pk)
        if report.status != Report.Status.PENDING:
            return report
        report.status = Report.Status.IN_REVIEW
        report.save(update_fields=["status", "updated_at"])
        ReportAudit.objects.create(report=report, event="review_started", actor=actor)
        return report


def retain_closed_reports(*, now: Any = None) -> dict[str, int]:
    """Apply retention transitions safely; each transition is idempotent and audited."""

    now = now or timezone.now()
    email_age = now - timedelta(
        seconds=max(1, int(getattr(settings, "REPORT_EMAIL_RETENTION", 90 * 86400)))
    )
    details_age = now - timedelta(
        seconds=max(1, int(getattr(settings, "REPORT_DETAILS_RETENTION", 365 * 86400)))
    )
    emails = details = 0
    for report in Report.objects.filter(closed_at__isnull=False).iterator():
        with transaction.atomic():
            locked = Report.objects.select_for_update().get(pk=report.pk)
            if locked.contact_email and locked.closed_at and locked.closed_at <= email_age:
                locked.contact_email = ""
                locked.email_anonymized_at = now
                locked.save(update_fields=["contact_email", "email_anonymized_at", "updated_at"])
                ReportAudit.objects.create(report=locked, event="email_retained_removed")
                emails += 1
            if (
                locked.personal_details_anonymized_at is None
                and locked.closed_at
                and locked.closed_at <= details_age
            ):
                locked.message = "[Personal details removed after retention period.]"
                locked.contact_email = ""
                locked.decision_reason = "[Decision details removed after retention period.]"
                locked.duplicate_fingerprint = ""
                locked.personal_details_anonymized_at = now
                locked.save(
                    update_fields=[
                        "message",
                        "contact_email",
                        "decision_reason",
                        "duplicate_fingerprint",
                        "personal_details_anonymized_at",
                        "updated_at",
                    ]
                )
                ReportAudit.objects.create(report=locked, event="personal_details_anonymized")
                details += 1
    return {"emails_removed": emails, "reports_anonymized": details}


create_report = submit_report
anonymize_old_reports = retain_closed_reports
