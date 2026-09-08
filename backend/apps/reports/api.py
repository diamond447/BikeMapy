"""Public anonymous report endpoint."""

# mypy: disable-error-code="import-untyped,misc"

from __future__ import annotations

from uuid import UUID

from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.views_routes import public_route_queryset

from .serializers import ReportSubmissionSerializer
from .services import (
    DuplicateReport,
    HoneypotTriggered,
    RateLimited,
    TurnstileRejected,
    submit_report,
)


class RouteReportView(APIView):
    """Accept a report without creating a user account or changing route state."""

    @extend_schema(
        request=ReportSubmissionSerializer,
        responses={
            201: OpenApiResponse(description="Report entered the review queue."),
            400: OpenApiResponse(description="Invalid or failed protection."),
            409: OpenApiResponse(description="Matching report already exists."),
            429: OpenApiResponse(description="Report rate limit reached."),
        },
    )
    def post(self, request: Request, route_id: UUID) -> Response:
        route = get_object_or_404(public_route_queryset(), pk=route_id)
        serializer = ReportSubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            report = submit_report(
                route,
                reason=values["reason"],
                message=values["message"],
                contact_email=values.get("contact_email") or values.get("email", ""),
                turnstile_token=values["turnstile_token"],
                honeypot=values.get("website", ""),
                request=request,
            )
        except HoneypotTriggered:
            return Response(
                {"detail": "Unable to submit this report."}, status=status.HTTP_400_BAD_REQUEST
            )
        except TurnstileRejected:
            return Response(
                {"detail": "Security verification failed."}, status=status.HTTP_400_BAD_REQUEST
            )
        except DuplicateReport:
            return Response(
                {"detail": "A matching report was already submitted."},
                status=status.HTTP_409_CONFLICT,
            )
        except RateLimited as exc:
            response = Response(
                {"detail": "Report rate limit reached."}, status=status.HTTP_429_TOO_MANY_REQUESTS
            )
            response["Retry-After"] = str(exc.retry_after)
            return response
        except ValidationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            {"status": "received", "report_id": str(report.pk)}, status=status.HTTP_201_CREATED
        )
