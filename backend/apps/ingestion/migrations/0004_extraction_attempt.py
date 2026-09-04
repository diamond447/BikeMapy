from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [
        ("catalogue", "0001_initial"),
        ("ingestion", "0003_exhausted_page_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExtractionAttempt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("source_url", models.URLField(max_length=1000)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("processing", "Processing"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                            ("superseded", "Superseded"),
                        ],
                        default="queued",
                        max_length=20,
                    ),
                ),
                ("attempt_number", models.PositiveIntegerField(default=1)),
                ("diagnostics", models.JSONField(blank=True, default=dict)),
                ("error", models.TextField(blank=True)),
                ("checksum", models.CharField(blank=True, max_length=128)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "source",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="extraction_attempts",
                        to="catalogue.routesource",
                    ),
                ),
                (
                    "version",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="extraction_attempts",
                        to="catalogue.routeversion",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=["source", "attempt_number"],
                        name="ing_extract_source_attempt_unique",
                    )
                ],
                "indexes": [
                    models.Index(
                        fields=["source", "status"], name="ing_extract_source_status_idx"
                    ),
                    models.Index(
                        fields=["status", "created_at"], name="ing_extract_status_created_idx"
                    ),
                ],
            },
        ),
    ]
