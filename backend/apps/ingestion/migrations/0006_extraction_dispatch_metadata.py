from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ingestion", "0005_orphan_payload_cleanup"),
    ]

    operations = [
        migrations.AddField(
            model_name="extractionattempt",
            name="dispatch_attempts",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="extractionattempt",
            name="dispatch_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="extractionattempt",
            name="dispatch_task_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="extractionattempt",
            name="dispatched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
