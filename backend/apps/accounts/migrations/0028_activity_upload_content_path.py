from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0027_activity_upload_processing")]

    operations = [
        migrations.AddField(
            model_name="activityupload",
            name="content_path",
            field=models.FileField(
                blank=True,
                null=True,
                upload_to="private/activity_uploads/",
            ),
        ),
    ]
