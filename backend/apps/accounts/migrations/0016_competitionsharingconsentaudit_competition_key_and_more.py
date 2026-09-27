import apps.accounts.models
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0015_competitionsharingconsentaudit_and_disclosure'),
    ]

    operations = [
        migrations.AddField(
            model_name='competitionsharingconsentaudit',
            name='competition_key',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='competitionsharingconsentaudit',
            name='player_key',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='competitionsharingconsentaudit',
            name='retention_until',
            field=models.DateTimeField(default=apps.accounts.models.consent_audit_retention_until),
        ),
        migrations.AlterField(
            model_name='competitionsharingconsentaudit',
            name='membership',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sharing_audits', to='accounts.competitionmembership'),
        ),
    ]
