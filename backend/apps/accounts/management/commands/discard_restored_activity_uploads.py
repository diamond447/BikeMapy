"""Discard transient upload payload references after a durable restore."""

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from apps.accounts.models import ActivityUpload, ActivityUploadDeletion


class Command(BaseCommand):
    help = "Clear transient activity upload payloads and fail unfinished imports."

    def handle(self, *args: object, **options: object) -> None:
        now = timezone.now()
        deleted = 0
        uploads = ActivityUpload.objects.filter(
            Q(status__in=(ActivityUpload.Status.QUEUED, ActivityUpload.Status.PROCESSING))
            | Q(content__isnull=False)
            | (Q(content_path__isnull=False) & ~Q(content_path=""))
        )
        for upload in uploads.iterator():
            storage_key = upload.content_path.name if upload.content_path else ""
            if storage_key:
                ActivityUploadDeletion.objects.get_or_create(storage_key=storage_key)
            upload.content_path = None
            upload.content = None
            if upload.status in (ActivityUpload.Status.QUEUED, ActivityUpload.Status.PROCESSING):
                upload.status = ActivityUpload.Status.FAILED
                upload.error_code = "restore_payload_discarded"
                upload.error_detail = "Transient upload data is not restored from backups."
                upload.processed_at = now
            upload.save(
                update_fields=(
                    "content_path",
                    "content",
                    "status",
                    "error_code",
                    "error_detail",
                    "processed_at",
                )
            )
            deleted += 1
        self.stdout.write(f"Discarded {deleted} restored transient activity upload payloads")
