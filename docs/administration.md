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

## Anonymous reports

Reports are available at the route detail endpoint
`POST /api/v1/routes/<route-id>/reports/`. The form accepts incorrect-route,
source/attribution, author-removal, rights-holder, and other reasons, with an
optional contact email. Turnstile and a server-side honeypot are required;
reports are queued for explicit owner review and never change route visibility
or content automatically.

The owner can open **Reports** in Django Admin and record start-review,
accept, reject, duplicate, or close-without-action decisions. Every submission
and decision has an audit event. `REPORT_RATE_LIMIT_HOURLY` and
`REPORT_RATE_LIMIT_DAILY` default to 3 and 10. Rate identifiers are HMACs held
only in Redis, with 24-hour maximum retention; raw client addresses are never
stored in the database.

By default, rate limiting uses the direct `REMOTE_ADDR`. For the production
Cloudflare Tunnel Compose topology, set `REPORT_CLIENT_IP_MODE=cloudflare` and
list only the direct Nginx peer `172.30.0.2/32` in
`REPORT_TRUSTED_PROXY_CIDRS`; do not list public Cloudflare CIDRs. Only a single valid
`CF-Connecting-IP` value from those peers is accepted; untrusted peers and
malformed or comma-separated values are safely ignored/rejected.

Set `REPORT_TURNSTILE_SECRET_KEY` and `REPORT_RATE_LIMIT_HMAC_SECRET` in the
runtime environment. The daily Celery beat task removes closed-report email
after 90 days and replaces personal report details after 12 months. Both
transitions are idempotent and audited.
