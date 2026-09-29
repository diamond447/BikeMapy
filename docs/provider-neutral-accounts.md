# Provider-neutral player accounts and activity uploads

Player onboarding is invite-only. A valid active competition invite is
required for a new local account or first-time GitHub account. The invite is
validated before onboarding and its single-use onboarding redemption is
recorded in the same transaction as the new player and competition
membership. The ordinary competition join invite remains reusable. Existing sign-in,
password reset, and explicitly authenticated GitHub linking do not require a
new invite.

Before enabling `PLAYER_ACCOUNTS_ENABLED` in production, configure
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
Password-reset messages are queued for Celery delivery; transport failures are
recorded in application logs without logging addresses or reset tokens.

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

The legacy Strava integration remains isolated and optional. Direct uploads do
not create or require Strava credentials.
