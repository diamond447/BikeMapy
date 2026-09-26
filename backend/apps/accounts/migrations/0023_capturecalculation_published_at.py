from django.db import migrations, models


def backfill_current_publications(apps, schema_editor):
    del schema_editor
    capture_calculation = apps.get_model("accounts", "CaptureCalculation")
    for calculation in capture_calculation.objects.filter(
        is_current=True, published_at__isnull=True
    ).iterator():
        calculation.published_at = calculation.completed_at or calculation.requested_at
        calculation.save(update_fields=("published_at",))


def preserve_publication_history(apps, schema_editor):
    del apps, schema_editor


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0022_preserve_capture_owner_keys"),
    ]

    operations = [
        migrations.AddField(
            model_name="capturecalculation",
            name="published_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_current_publications, preserve_publication_history),
    ]
