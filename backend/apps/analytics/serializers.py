"""Validation for the intentionally tiny anonymous analytics contract."""

# mypy: disable-error-code="import-untyped,misc"

from collections.abc import Mapping

from rest_framework import serializers

from .models import AnalyticsCounter


class AnalyticsEventSerializer(serializers.Serializer):
    """Accept only known event names and no other event metadata."""

    event = serializers.ChoiceField(choices=AnalyticsCounter.Event.choices)

    def to_internal_value(self, data):  # type: ignore[no-untyped-def]
        if not isinstance(data, Mapping):
            raise serializers.ValidationError({"event": ["An object with an event is required."]})
        if set(data) != {"event"}:
            raise serializers.ValidationError({"event": ["Only the event field is accepted."]})
        return super().to_internal_value(data)
