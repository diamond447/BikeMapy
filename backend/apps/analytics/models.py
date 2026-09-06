"""Anonymous, day-bucketed counters for the product's launch hypothesis."""

from __future__ import annotations

from django.db import models


class AnalyticsCounter(models.Model):
    """A count of one allow-listed event for one local calendar day.

    The model deliberately has no route, session, user, IP address, user agent,
    or arbitrary metadata field.  It is therefore useful for aggregate product
    signals while remaining unable to reconstruct an individual visit.
    """

    class Event(models.TextChoices):
        ROUTE_DETAIL_VIEW = "route_detail_view", "Route-detail view"
        GPX_DOWNLOAD_CLICK = "gpx_download_click", "GPX download click"
        ORIGINAL_SOURCE_CLICK = "original_source_click", "Original-source click"

    day = models.DateField()
    event = models.CharField(max_length=32, choices=Event.choices)
    count = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ["-day", "event"]
        constraints = [
            models.UniqueConstraint(fields=["day", "event"], name="analytics_day_event_unique")
        ]
        indexes = [models.Index(fields=["event", "day"], name="analytics_event_day_idx")]

    def __str__(self) -> str:
        return f"{self.day}: {self.get_event_display()} ({self.count})"
