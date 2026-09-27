from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_stravawebhookevent_importedactivity_activity_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='importedactivity',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
    ]
