# Launch verification record

This runbook records what must be checked before the first public launch and
keeps measured evidence separate from targets and owner actions. It is not a
promise that the checks have passed merely because the commands are listed.

## Launch configuration

The production frontend sets `VITE_PUBLIC_SITE_URL`, `VITE_API_URL`, and the
public `VITE_CF_WEB_ANALYTICS_TOKEN`. The token enables the Cloudflare Web
Analytics beacon, whose aggregate visits metric is the primary launch
measurement. The API deployment keeps `GPX_REDISTRIBUTION_APPROVED=false` and
`GPX_INTERNAL_REDIRECT=false` until a new documented legal approval is made.
Reports, OAuth owner IDs, Turnstile, trusted proxy CIDRs, crawler limits,
backup paths, and Sentry retention are configured only through the host-only
environment described in the deployment and operations runbooks.

The event endpoint accepts only `route_detail_view`, `gpx_download_click`, and
`original_source_click`. It stores a single day/event counter and no route ID,
URL, visitor ID, IP address, user-agent, or arbitrary event metadata. Product
events are best-effort and cannot block route browsing. The disabled GPX
decision means no public download click can occur at launch; its counter is
kept for a future legally approved configuration.

The counters are directional product signals, not authenticated facts: a
client can block or spoof a beacon and the API does not attempt to establish
unique visitors. Origin checks reduce accidental cross-site writes, while
requests without an Origin header remain supported for same-origin and
non-browser calls. Cloudflare's provider boundary and retention limits are
recorded in [the privacy notice](privacy.md) and remain subject to owner
review.

The disposable crawler rehearsal sets the internal `BIKEFORUM_CHECK_SOURCES`
flag to false only for its synthetic fixture URLs, so the rehearsal cannot
contact real Mapy hosts. Production keeps the default `true`; this harness-only
boundary must not be copied into a production environment.

## Verification matrix

| Area | Evidence or procedure | Status in this record |
| --- | --- | --- |
| Backend lint, types, tests, coverage | `make lint typecheck test`; retain terminal output and the coverage report. | Run locally for this change; record exact result below. |
| Frontend lint, types, tests, build | `cd frontend && pnpm lint && pnpm format && pnpm exec tsc -b --pretty false && pnpm test && VITE_PUBLIC_SITE_URL=http://localhost:5173 VITE_API_URL=https://api.example.invalid VITE_ENABLE_REPORTS=false pnpm build`; inspect the generated bundle. | Run locally for this change; owner/CI evidence remains separate. |
| API contract and dependency audit | `make api-schema`; run `pip-audit`, `pnpm audit --audit-level high`, and repository security checks where tooling is available. | Record each command result; do not infer CI success from local output. |
| Privacy analytics | Test empty-token and configured-token builds; POST each allow-listed event and verify only the day/event row changes. Inspect browser requests for payloads with no identifier or metadata. Verify allowed/disallowed Origin behavior and that throttle keys are global rather than IP-derived. | Automated unit/API checks are evidence; hosted Cloudflare dashboard and vendor-settings review is owner action. |
| GPX legal gate | With default settings, request route detail and `/gpx/`; verify no download URL and a 404 response. | Automated backend test; production configuration is an owner gate. |
| Accessibility and localization | Run `pnpm e2e`; review axe output, keyboard focus, English/Czech copy, and reduced-motion behavior. | Automated tests are evidence; owner screenshot review is pending. |
| Responsive acceptance | Run desktop and mobile Playwright evidence tests at representative viewport sizes; owner reviews screenshots for overlap, detail sheet, map, and footer. | Automated tests may pass; screenshot approval must be recorded by owner. |
| Backup and restore | Run `deploy/backup.sh` in the production-like host window, verify its manifest, then run `deploy/restore-drill.sh <backup-id>` and the scheduled restore-drill workflow. Preserve row-count and checksum evidence. | Disposable drill is runnable here; production host execution is pending owner. |
| Deployment rollback | Deploy a candidate image, verify health endpoints and representative route reads, switch to the previous image, and verify the same checks again. Record distinct content-addressed local image IDs; production uses immutable registry digests. Trigger for failed readiness, broken route reads, data corruption, or a legal/privacy incident. | `issue-18-rollback-20260906T133440Z.json` records candidate/previous IDs, running-container resolution, health, and route reads in a disposable Compose PostGIS/Redis stack. Production immutable-artifact rehearsal remains an owner action. |
| Crawler resumption | Stop a launch-like worker between checkpoint updates, restart it, and verify checkpoint/frontier resumption without duplicate records. Confirm `health/crawler/` after recovery. | `issue-18-crawler-20260906T131928Z.json` records a real PostGIS/Redis/Celery worker SIGKILL, expired lease, restart, and resumed frontier. Production worker evidence remains an operator action. |
| Partial catalogue/backfill | Load a representative partial fixture, verify browse/detail/source behavior, start bounded backfill, interrupt it, and resume from checkpoint. Report imported, failed, retried, and remaining counts. | The same crawler rehearsal records API browse/detail/source results before and after resume, one imported post/source becoming two with attempts `1 → 2`, and no duplicate boundary; real catalogue counts remain deployment evidence. |

## Operator actions and rollback triggers

The owner must review the production Cloudflare Web Analytics property and
record its site token, retention settings, and a screenshot showing that no
marketing cookies or fingerprinting feature is enabled. The owner must also
review desktop/mobile screenshots, confirm legal and controller details in the
public notices, verify backups and restore artifacts, and record image digests
and endpoint responses after deployment.

Roll back the immutable frontend/backend pair to the last known-good digest if
`/health/live/` or `/health/ready/` fails, representative route reads fail,
the crawler corrupts or duplicates data, restore verification fails, or an
unexpected analytics payload contains an identifier. Disable analytics by
removing the public token if the hosted property behaves contrary to the
privacy decision. Keep GPX redistribution disabled and remove any accidental
download exposure immediately; a legal or rights-holder concern is an
immediate launch rollback trigger.

## Measurement hypothesis

The initial product hypothesis is **500 unique visitors in the first 30 days**.
This is measured with Cloudflare Web Analytics and is neither a guarantee nor a
service-level objective. Record the observation window, Cloudflare visit
export, route-detail/source counters, and known catalogue/backfill state. Do
not treat product event totals as unique visitors or add them to the visits
metric.

## Local evidence

The agent records commands actually executed for this issue here, including
failures and environment limitations. GitHub Actions results, hosted
Cloudflare screenshots, production backup/restore logs, and owner acceptance
are external evidence and must not be fabricated in this file.

### Evidence collected in this environment (2026-09-06)

- The final local check passed: the backend suite passed 185 tests with 6
  skipped and 88.21% coverage; frontend ESLint/Prettier/type-check and 30
  frontend tests passed with 92.84% line coverage. The production frontend
  build completed. Vite emitted the existing large-chunk warning; it did not
  fail the build.
- `make api-schema` passed and regenerated `openapi.yaml` and the typed
  frontend client. The privacy event API is present in both artifacts.
- `uv tool run --from pip-audit==2.9.0 pip-audit --strict ...` passed with
  `No known vulnerabilities found`; `corepack pnpm audit --audit-level high`
  passed with the same result after activating the locked pnpm 10.10.0.
- The frontend Playwright suite passed: 4 tests, including the desktop/mobile
  evidence and accessibility/localization checks.
- The frontend secret scan passed for both the normal build and a configured
  token build. The configured build contains the Cloudflare beacon URL and
  only the supplied public token; no server secret is bundled.
- A disposable PostGIS restore drill passed with backup ID
  `20260906T132000Z` and `route_count=3`; its ID predates verification at
  `2026-09-06T13:21:59Z`. The generated manifest, drill output, and concise
  command log retain matching database and GPX SHA-256 values in
  [the evidence directory](evidence/). The restore-failure rollback behavior
  is also covered by `backend/config/tests/test_restore_scripts.py`.
  Production backup/restore remains an owner-host action.
- `scripts/rehearse_launch.py --mode rollback` built two local image revisions,
  verified readiness and a seeded route read on the candidate, switched the
  disposable Compose backend to the previous image, and verified both again.
  Evidence is retained in `issue-18-rollback-20260906T133440Z.json`; this is a
  local immutable-artifact rehearsal, not production deployment evidence.
- `scripts/rehearse_launch.py --mode crawler` used a real PostGIS/Redis/Celery
  worker process and a synthetic HTTP fixture. After one page imported one
  post/source, the worker was SIGKILLed while the second page was leased;
  after lease expiry and worker restart, the bounded task imported the second
  post/source idempotently. Browse/detail/source API checks passed before and
  after resume, with route counts 1 and 2. Evidence is retained in
  `issue-18-crawler-20260906T131928Z.json`; production worker evidence remains
  an operator action.

The following evidence is intentionally not claimed here: a hosted Cloudflare
dashboard screenshot and retention review, owner desktop/mobile screenshot
approval, CI workflow results for this branch, a production backup/restore,
an interrupted production crawler/backfill run, and the observed 30-day visit
result. Those remain explicit owner or deployment procedures in the matrix.
