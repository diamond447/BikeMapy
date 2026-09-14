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

## Launch analytics

The **Analytics counters** view is read-only and owner-only. It shows one
day-bucketed total for each of the three allow-listed product events:
route-detail views, GPX download clicks, and original-source clicks. These
totals are not unique visitors; Cloudflare Web Analytics remains the primary
visit metric. Do not use them to infer a visitor's route or identity. GPX
downloads remain disabled while `GPX_REDISTRIBUTION_APPROVED=false`, so the
download counter should remain zero at launch.

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

By default, rate limiting uses the direct `REMOTE_ADDR`. The same resolver is
used by generic API throttles. For the production Cloudflare Tunnel Compose
topology, set `REPORT_CLIENT_IP_MODE=cloudflare` and list only the direct Nginx
peer `172.30.0.2/32` in `REPORT_TRUSTED_PROXY_CIDRS`; do not list public
Cloudflare CIDRs. Only a single valid `CF-Connecting-IP` value from that peer
is accepted. Untrusted peers and malformed or comma-separated values never
become a client identity, and the forwarded chain is removed before Django
handles the request. API throttle cache keys contain HMAC identifiers rather
than raw addresses; set `RATE_LIMIT_HMAC_SECRET` to a dedicated deployment
secret (it falls back to the report secret, then Django's secret key).

Set `REPORT_TURNSTILE_SECRET_KEY` and `REPORT_RATE_LIMIT_HMAC_SECRET` in the
runtime environment. The daily Celery beat task removes closed-report email
after 90 days and replaces personal report details after 12 months. Both
transitions are idempotent and audited. The launch retention target for
administrative and moderation audit events is 24 months from creation, unless
a documented legal hold applies. This target is not implemented yet: audit
rows currently have no automatic expiry, so the owner must not treat this
document as evidence that the retention gate has passed.
