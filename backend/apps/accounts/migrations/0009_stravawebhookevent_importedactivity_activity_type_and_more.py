import apps.catalogue.fields
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_competitionrecomputation_leases'),
    ]

    operations = [
        migrations.CreateModel(
            name='StravaWebhookEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_key', models.CharField(max_length=64, unique=True)),
                ('subscription_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('object_id', models.PositiveBigIntegerField()),
                ('owner_athlete_id', models.PositiveBigIntegerField()),
                ('aspect_type', models.CharField(max_length=24)),
                ('object_type', models.CharField(default='activity', max_length=24)),
                ('payload', models.JSONField(blank=True, default=dict)),
                ('received_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('processed_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.CharField(blank=True, max_length=240)),
            ],
        ),
        migrations.AddField(
            model_name='importedactivity',
            name='activity_type',
            field=models.CharField(blank=True, max_length=48),
        ),
        migrations.AddField(
            model_name='importedactivity',
            name='calendar_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='importedactivity',
            name='geometry',
            field=apps.catalogue.fields.RouteGeometryField(blank=True, null=True, srid=4326),
        ),
        migrations.AddField(
            model_name='importedactivity',
            name='geometry_hash',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='importedactivity',
            name='provider_updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='importedactivity',
            name='removal_reason',
            field=models.CharField(blank=True, max_length=48),
        ),
        migrations.AddField(
            model_name='importedactivity',
            name='removed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='importedactivity',
            name='visibility',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.CreateModel(
            name='StravaSyncState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(choices=[('idle', 'Idle'), ('queued', 'Queued'), ('running', 'Running'), ('paused', 'Paused'), ('failed', 'Failed')], default='idle', max_length=16)),
                ('mode', models.CharField(default='incremental', max_length=16)),
                ('history_start', models.DateTimeField(blank=True, null=True)),
                ('cursor_page', models.PositiveIntegerField(default=1)),
                ('imported_count', models.PositiveIntegerField(default=0)),
                ('rejected_count', models.PositiveIntegerField(default=0)),
                ('processed_count', models.PositiveIntegerField(default=0)),
                ('last_provider_updated_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.CharField(blank=True, max_length=240)),
                ('next_attempt_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('lease_token', models.CharField(blank=True, max_length=64)),
                ('lease_until', models.DateTimeField(blank=True, null=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('player', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='strava_sync_state', to='accounts.player')),
            ],
        ),
        migrations.CreateModel(
            name='StravaSyncJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('initial', 'Initial history'), ('full-history', 'Full history'), ('incremental', 'Incremental'), ('webhook', 'Webhook')], max_length=16)),
                ('idempotency_key', models.CharField(max_length=160, unique=True)),
                ('page', models.PositiveIntegerField(default=1)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')], default='pending', max_length=16)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('next_attempt_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_error', models.CharField(blank=True, max_length=240)),
                ('lease_token', models.CharField(blank=True, max_length=64)),
                ('lease_until', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='strava_sync_jobs', to='accounts.player')),
                ('webhook_event', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='sync_jobs', to='accounts.stravawebhookevent')),
            ],
            options={
                'indexes': [models.Index(fields=['status', 'next_attempt_at'], name='accounts_st_status_18deb8_idx')],
            },
        ),
    ]
