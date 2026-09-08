# Security policy

## Supported versions

Only the latest commit on the default branch (`main`) is supported with
security fixes. Older commits and local development deployments are not
security-supported; update to the latest `main` before reproducing a report.

## Reporting a vulnerability

Please report suspected vulnerabilities privately through [GitHub private
vulnerability reporting](https://github.com/diamond447/BikeMapy/security/advisories/new).
Do not open a public issue for an unpatched vulnerability.

Include the affected revision, a short impact statement, reproduction steps,
and a minimal proof of concept. Remove personal data and redact passwords,
session cookies, API keys, tokens, private network addresses, and exact home
or starting locations from the report. If a private report cannot be opened,
contact the repository owner through a trusted GitHub channel and request a
private reporting route; do not send secrets in a public comment.


## Security boundaries and threat model

BikeMapy is a public, read-only route catalogue. The browser is an untrusted
client. It calls the versioned Django REST API, whose responses contain only
published routes, approved versions, geometry, and source attribution. Query
parameters are untrusted and are constrained by pagination, viewport bounds,
and DRF anonymous/user throttles.

The Django admin is a separate trusted operator boundary. It uses Django
authentication, sessions, and CSRF protection; only authorized maintainers
should receive staff accounts. Production deployments must use HTTPS, a
deployment-specific `DJANGO_SECRET_KEY`, restricted `DJANGO_ALLOWED_HOSTS`,
and secret storage provided by the deployment platform.

The scheduled Celery worker fetches untrusted, server-rendered BikeForum HTML.
The crawler follows robots policy, allows only configured BikeForum origins,
revalidates DNS and every redirect, paces requests, retries a bounded number
of times, and caps the response before parsing. BeautifulSoup/lxml extracts
text and links; fetched markup is never rendered as frontend HTML or executed.
The crawl frontier, leases, and response cache live in PostgreSQL/Redis.

GPX content is untrusted even when it comes from an approved Mapy URL. Every
request and redirect is restricted to HTTPS Mapy hosts and checked against
public DNS answers. Requests do not follow redirects automatically, use a
pinned transport, and enforce time, retry, byte, content-type, XML namespace,
coordinate, and point-count limits. `defusedxml` rejects entity expansion and
external entities (XXE). Original payloads are kept under non-public storage
keys only when policy allows it; quarantined data is not published and durable
cleanup removes payloads after deletion requests.

The accepted URL flows are the configured BikeForum seed and Mapy links
discovered in forum posts. There is no public URL-submission endpoint today.
Any future admin or API submission must use the same allowlists, redirect
checks, DNS checks, timeouts, and size limits; parsing a URL or a file name is
never an authorization decision.

PostgreSQL is an internal data store reached through Django's ORM and
parameterized queries. Redis is an internal cache and Celery broker/result
backend, not a public API; production network policy must restrict both and
credentials/TLS must be configured where the service supports them. Celery
tasks are bounded, retry-safe, and do not execute shell commands from fetched
data. BikeForum, Mapy, the GPX exporter, DNS, and package registries are
external dependencies: their availability and content are not trusted, and a
provider response is validated before it affects catalogue state.

### Main abuse cases and controls

| Risk | Control | Residual expectation |
| --- | --- | --- |
| SSRF through a seed, source, or redirect | scheme/host/port allowlists, DNS private-address rejection, redirect revalidation, pinned Mapy transport | Recheck DNS and allowlists when adding providers or URL flows |
| XXE, entity expansion, or parser exhaustion | `defusedxml`, strict GPX namespaces, bounded HTML/GPX bytes and points, bounded retries/timeouts | Keep limits finite and test hostile fixtures |
| Unsafe redirects or hostile files | redirects are explicit and same-origin/Mapy-only; content types, storage keys, quarantine, and cleanup are validated | Never expose original payload storage directly |
| Authentication/authorization failure | public API is read-only; admin is Django-authenticated and CSRF-protected; lifecycle and approved-version filters gate publication | Review every new write endpoint and staff permission |
| Secret leakage | environment/platform secret stores, no `.env` commits, Gitleaks in PR/default-branch CI, redacted reports/logs | Rotate any credential suspected of exposure |
| Rate-limit or resource abuse | crawler pacing, retry/page/task/response bounds, pagination and spatial limits, API throttles, Celery task time limits | Tune limits with capacity evidence; do not disable caps globally |

## Triage and remediation

Maintainers first verify reachability and affected assets, then classify
severity by confidentiality, integrity, availability, and exploitability.
Typical priorities are: exposed credentials or remote code execution (urgent),
SSRF/authentication bypass or private data exposure (high), denial of service
or stored/script injection (medium/high depending on reach), and hardening or
dependency maintenance (low/medium). Confirmed issues get an owner, a
regression test where practical, and a fix or mitigation before disclosure.

CI findings are actionable by default: CodeQL fails on errors and high/critical
security findings, while Gitleaks and dependency audits fail at their
configured thresholds. A suppression is allowed only when the finding is a
documented false positive or accepted, time-bounded risk; record the reason
and expiry next to the suppression and obtain maintainer review. Never
baseline a real credential or hide a finding to make a workflow green.

### Client-side discovery state

The discovery UI stores filters, the selected public route ID, and the
viewport-only mode in `localStorage`. It deliberately does not store the map
viewport: latitude, longitude, and bounds can represent sensitive geographic
data, and CodeQL's `js/clear-text-storage-of-sensitive-data` query correctly
guards browser-storage sinks. The current viewport is instead serialized in
the shareable URL by `frontend/src/App.tsx`; that URL is the canonical way to
restore a browse context across reloads or devices. No device geolocation,
inferred starting location, authentication data, or secret is collected or
written to browser storage.
