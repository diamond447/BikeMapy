# Owner administration

BikeMapy administration is available only to GitHub accounts whose immutable
numeric GitHub ID is listed in `GITHUB_OWNER_IDS`. GitHub usernames are never
used for authorization, so renaming the owner account does not change access.
Keep the setting empty until the OAuth application has been configured and the
owner's numeric ID has been verified.

Configure a GitHub OAuth application with callback URL
`https://<host>/accounts/github/login/callback/`, then create the matching
django-allauth SocialApp for the `github` provider (or provide
`GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET` through the runtime
environment). The login entry point is
`/accounts/github/login/`; `/admin/` remains owner-only even for authenticated
non-allowlisted users.

The duplicate review page is available from each similarity relationship in
Django Admin. It renders both current route geometries in side-by-side
MapLibre maps, displays the persisted scoring evidence, and requires a reason
for merge-sources, keep-both, quarantine, and restore actions.

Moderation services use database transactions and append a
`ModerationDecision` containing the administrator, reason, timestamp, and
JSON `before`/`after` state. Historical source URLs and route versions remain
immutable. Source-unavailable records remain visible; soft removal activates a
denylist entry until an administrator explicitly restores it. Payload removal
metadata is retained, but it is never presented as a backup of the deleted
payload.

The public `Reviewed` badge is deliberately narrow. It is true only when the
latest review decision explicitly records all three boolean checks:

- technical validity;
- source context; and
- obvious content suitability.

It does not claim safety, current passability, or legal access.
