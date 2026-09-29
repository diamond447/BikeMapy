# Provider-neutral player accounts and activity uploads

Player onboarding is invite-only. A valid active competition invite is
required for a new local account or first-time GitHub account. The invite is
validated before onboarding and its single-use onboarding redemption is
recorded in the same transaction as the new player and competition
membership. The ordinary competition join invite remains reusable. Existing sign-in,
password reset, and explicitly authenticated GitHub linking do not require a
new invite.

Before enabling `PLAYER_ACCOUNTS_ENABLED` in production, configure
`GAME_FRONTEND_URL` as the deployed, non-local HTTPS URL ending in `/game`, as well as
`EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`, `EMAIL_HOST`,
`EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, exactly one of
`EMAIL_USE_TLS` or `EMAIL_USE_SSL`, and a real `DEFAULT_FROM_EMAIL` sender
address. Keep SMTP credentials in the deployment secret store or untracked
production environment file. The backend fails startup when local accounts are
enabled with missing or placeholder mail settings. Local Compose defaults to
Django's console email backend for development.

Local onboarding sends a generated temporary password by email. The password
is stored only through Django's password hasher and the first authenticated
session must change it before the game is available. Login and reset responses
are intentionally enumeration-safe and account endpoints are rate-limited.
Password-reset messages are queued for Celery delivery using an encrypted email
payload derived from `DJANGO_SECRET_KEY`. Backend and worker processes must share
that key, and queued reset jobs should be drained before rotating it. The worker
performs account lookup and token generation. Transient SMTP failures are retried
up to three times; delivery failures are recorded in application logs without
logging addresses or reset tokens.

Players can upload FIT, GPX, and TCX files individually or in a ZIP archive.
Uploads require an ownership/authorization attestation and are processed by a
bounded asynchronous batch. XML external entities are disabled, archive paths
and expansion are bounded, and each payload is persisted as one private,
file-backed object and consumed sequentially; raw bytes are never accumulated
as a batch or retained in the database. Payload objects are cleared after
terminal processing and by the hourly cleanup task after at most 24 hours
(`ACTIVITY_UPLOAD_MAX_RETENTION_HOURS`). A deterministic content fingerprint
makes retries idempotent; duplicate results are reported per file. Normalized
activities use the same private `ImportedActivity` model and recomputation
lifecycle as provider imports.

The backend caps aggregate uploaded file bytes at 90 MiB. Nginx accepts
multipart request bodies up to 95 MiB, leaving 5 MiB for framing before
Cloudflare's standard 100 MB request-body ceiling. Larger bodies are rejected
at the proxy before multipart processing. Raw upload objects are stored in the dedicated
`activity_upload_data` volume, separate from durable GPX media. Account deletion
captures queued and processing object keys in a database deletion queue before
cascading upload rows. Storage failures remain visible in owner administration
and retry with backoff. A 15-minute reconciler queues unreferenced objects only
after a one-hour age grace period. Successful deletion queue records are purged
after 30 days; failed records remain until storage deletion succeeds.

The upload volume is deliberately omitted from durable database/GPX backups.
GPX archives exclude the legacy `media/private/activity_uploads/` directory;
the validator still accepts older archives that contain it, but restore
extraction skips those raw files. Restore clears transient upload references
and the dedicated volume; unfinished batches restored from a snapshot are
marked failed because raw payloads are not part of recovery points. A labeled
temporary rollback volume preserves the current upload volume only while a
restore is in progress; successful snapshots are removed immediately and
abandoned snapshots expire after 24 hours. Normalized private activities and
deletion queue metadata remain in the database backup under their existing
account and audit retention rules.

The legacy Strava integration remains isolated and optional. Direct uploads do
not create or require Strava credentials.
