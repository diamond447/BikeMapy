# Provider-neutral player accounts and activity uploads

Player onboarding is invite-only. A valid active competition invite is
required for a new local account or first-time GitHub account. The invite is
validated before onboarding and its redemption is recorded in the same
transaction as the new player and competition membership. Existing sign-in,
password reset, and explicitly authenticated GitHub linking do not require a
new invite.

Local onboarding sends a generated temporary password by email. The password
is stored only through Django's password hasher and the first authenticated
session must change it before the game is available. Login and reset responses
are intentionally enumeration-safe and account endpoints are rate-limited.

Players can upload FIT, GPX, and TCX files individually or in a ZIP archive.
Uploads require an ownership/authorization attestation and are processed by a
bounded asynchronous batch. XML external entities are disabled, archive paths
and expansion are bounded, and raw upload bytes are cleared after terminal
processing. A deterministic content fingerprint makes retries idempotent;
duplicate results are reported per file. Normalized activities use the same
private `ImportedActivity` model and recomputation lifecycle as provider
imports.

The legacy Strava integration remains isolated and optional. Direct uploads do
not create or require Strava credentials.
