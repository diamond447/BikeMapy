from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("reference_routes", "0003_reference_route_source_gate")]

    operations = [
        migrations.CreateModel(
            name="ReferenceAlterationOffer",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("manifest_url", models.URLField(max_length=1000)),
                ("artifact_url", models.URLField(max_length=1000)),
                ("method_url", models.URLField(max_length=1000)),
                ("published_at", models.DateTimeField()),
                ("offered_snapshot_hash", models.CharField(max_length=64)),
                ("operator_evidence", models.TextField()),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                (
                    "source_import",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="alteration_offer",
                        to="reference_routes.referenceimport",
                    ),
                ),
            ],
        )
    ]
