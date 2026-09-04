from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("ingestion", "0001_crawl_state")]

    operations = [
        migrations.AddField(
            model_name="crawlcheckpoint",
            name="lease_token",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="crawlcheckpoint",
            name="lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="crawltask",
            name="stream",
            field=models.CharField(default="incremental", max_length=120),
        ),
        migrations.AddField(
            model_name="crawltask",
            name="lease_token",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="crawltask",
            name="lease_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="CrawlPageWork",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("url", models.URLField(max_length=1000)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("processing", "Processing"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                        ],
                        default="queued",
                        max_length=20,
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("discovered_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("lease_until", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.TextField(blank=True)),
                ("status_code", models.PositiveSmallIntegerField(blank=True, null=True)),
                (
                    "task",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="frontier",
                        to="ingestion.crawltask",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["task", "status", "discovered_at"],
                        name="ingestion_c_task_id_54a1b5_idx",
                    )
                ]
            },
        ),
        migrations.AddConstraint(
            model_name="crawlpagework",
            constraint=models.UniqueConstraint(
                fields=("task", "url"), name="ingestion_task_url_unique"
            ),
        ),
    ]
