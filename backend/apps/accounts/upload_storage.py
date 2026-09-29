"""Storage boundary for transient private activity upload payloads."""

from django.conf import settings
from django.core.files.storage import FileSystemStorage


class ActivityUploadStorage(FileSystemStorage):
    """Keep raw activity files outside the durable GPX media root."""

    @property
    def location(self) -> str:
        return str(settings.ACTIVITY_UPLOAD_ROOT)


activity_upload_storage = ActivityUploadStorage()
