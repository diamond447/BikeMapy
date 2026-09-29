# Provider-neutral player accounts and activity uploads

Player onboarding is invite-only. A valid active competition invite is
required for a new local account or first-time GitHub account. The invite is
validated before onboarding and its single-use onboarding redemption is
recorded in the same transaction as the new player and competition
membership. The ordinary competition join invite remains reusable. Existing sign-in,
password reset, and explicitly authenticated GitHub linking do not require a
new invite.

Local onboarding sends a generated temporary password by email. The password
is stored only through Django's password hasher and the first authenticated
session must change it before the game is available. Login and reset responses
are intentionally enumeration-safe and account endpoints are rate-limited.

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

The Nginx transport boundary accepts requests up to 90 MiB, matching the
backend aggregate file limit; larger bodies are rejected at the proxy before
multipart processing. Raw upload objects are stored in the dedicated
`activity_upload_data` volume, separate from durable GPX media. Account deletion
captures queued and processing object keys in a database deletion queue before
cascading upload rows. Storage failures remain visible in owner administration
and retry with backoff. A 15-minute reconciler queues unreferenced objects only
after a one-hour age grace period. Successful deletion queue records are purged
after 30 days; failed records remain until storage deletion succeeds.

The upload volume is deliberately omitted from database/GPX backups. GPX
archives also exclude the legacy `media/private/activity_uploads/` directory,
and the archive validator rejects it if it appears. Restore clears transient
upload references and the dedicated volume; unfinished batches restored from a
snapshot are marked failed because raw payloads are not part of recovery
points. Normalized private activities and deletion queue metadata remain in
the database backup under their existing account and audit retention rules.

The legacy Strava integration remains isolated and optional. Direct uploads do
not create or require Strava credentials.
