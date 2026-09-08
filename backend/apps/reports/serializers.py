"""Public report submission serializer."""

# mypy: disable-error-code="import-untyped,misc"

from rest_framework import serializers

from .models import Report


class ReportSubmissionSerializer(serializers.Serializer):
    reason = serializers.ChoiceField(choices=Report.Reason.choices)
    message = serializers.CharField(min_length=10, max_length=5000, trim_whitespace=True)
    contact_email = serializers.EmailField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True, write_only=True)
    turnstile_token = serializers.CharField(write_only=True, max_length=4096)
    website = serializers.CharField(
        write_only=True, required=False, allow_blank=True, max_length=200
    )
