import hashlib

from django.db import migrations, models


def _owner_key(competition_id: object, player_id: int | None, row_id: int) -> str:
    value = f"capture-owner:{competition_id}:{player_id or f'legacy-{row_id}'}".encode()
    return hashlib.sha256(value).hexdigest()


def populate_owner_keys(apps, schema_editor) -> None:
    del schema_editor
    CaptureFaceOwner = apps.get_model("accounts", "CaptureFaceOwner")
    CapturePlayerArea = apps.get_model("accounts", "CapturePlayerArea")
    for row in CaptureFaceOwner.objects.select_related("face__calculation").iterator():
        row.owner_key = _owner_key(
            row.face.calculation.competition_id,
            row.player_id,
            row.pk,
        )
        row.save(update_fields=("owner_key",))
    for row in CapturePlayerArea.objects.select_related("calculation").iterator():
        row.owner_key = _owner_key(row.calculation.competition_id, row.player_id, row.pk)
        row.save(update_fields=("owner_key",))


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0021_link_recomputation_capture_generations"),
    ]

    operations = [
        migrations.AddField(
            model_name="capturefaceowner",
            name="owner_key",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="captureplayerarea",
            name="owner_key",
            field=models.CharField(max_length=64, null=True),
        ),
        migrations.RunPython(populate_owner_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="capturefaceowner",
            name="owner_key",
            field=models.CharField(max_length=64),
        ),
        migrations.AlterField(
            model_name="captureplayerarea",
            name="owner_key",
            field=models.CharField(max_length=64),
        ),
        migrations.RemoveConstraint(
            model_name="capturefaceowner",
            name="accounts_capture_face_owner_unique",
        ),
        migrations.AddConstraint(
            model_name="capturefaceowner",
            constraint=models.UniqueConstraint(
                fields=("face", "owner_key"), name="accounts_capture_face_owner_unique"
            ),
        ),
        migrations.RemoveConstraint(
            model_name="captureplayerarea",
            name="accounts_capture_player_area_unique",
        ),
        migrations.AddConstraint(
            model_name="captureplayerarea",
            constraint=models.UniqueConstraint(
                fields=("calculation", "owner_key"),
                name="accounts_capture_player_area_unique",
            ),
        ),
    ]
