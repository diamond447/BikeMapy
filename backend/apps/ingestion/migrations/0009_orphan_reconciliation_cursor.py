from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("ingestion", "0008_orphan_cleanup_leases")]

    operations = [
        migrations.CreateModel(
            name="OrphanPayloadReconciliationState",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "last_selected_bucket",
                    models.CharField(
                        choices=[("pending", "Pending"), ("failed", "Failed")],
                        default="failed",
                        max_length=20,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
