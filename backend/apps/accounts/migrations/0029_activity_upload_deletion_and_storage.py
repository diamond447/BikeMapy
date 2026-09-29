import django.utils.timezone
from django.utils import timezone
from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import migrations, models

from apps.accounts.upload_storage import ActivityUploadStorage


def migrate_legacy_activity_uploads(apps, schema_editor):
    """Move pre-0029 transient objects out of durable GPX media storage."""

    Upload = apps.get_model("accounts", "ActivityUpload")
    old_storage = FileSystemStorage(location=settings.MEDIA_ROOT)
    new_storage = ActivityUploadStorage()
    for upload in Upload.objects.exclude(content_path="").iterator():
        key = upload.content_path
        if not key:
            continue
        if not new_storage.exists(key):
            try:
                with old_storage.open(key, "rb") as source:
                    saved_key = new_storage.save(key, source)
            except FileNotFoundError:
                Upload.objects.filter(pk=upload.pk).update(
                    content_path="",
                    status="failed",
                    error_code="missing_payload",
                    error_detail="The upload payload was unavailable during storage migration.",
                    processed_at=timezone.now(),
                )
                continue
            if saved_key != key:
                upload.content_path = saved_key
                upload.save(update_fields=("content_path",))
        old_storage.delete(key)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0028_activity_upload_content_path")]

    operations = [
        migrations.AlterField(
            model_name="activityupload",
            name="content_path",
            field=models.FileField(
                blank=True,
                null=True,
                storage=ActivityUploadStorage(),
                upload_to="private/activity_uploads/",
            ),
        ),
        migrations.RunPython(migrate_legacy_activity_uploads, migrations.RunPython.noop),
        migrations.CreateModel(
            name="ActivityUploadDeletion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("storage_key", models.CharField(max_length=500, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("failed", "Failed"),
                            ("deleted", "Deleted"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("last_error", models.CharField(blank=True, max_length=240)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ("created_at", "pk")},
        ),
        migrations.AddIndex(
            model_name="activityuploaddeletion",
            index=models.Index(
                fields=["status", "next_attempt_at"], name="accounts_ac_status_d392e5_idx"
            ),
        ),
    ]
