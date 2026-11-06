# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through [GitHub private vulnerability
reporting](https://github.com/diamond447/BikeMapy/security/advisories/new),
not in a public issue. Include the affected commit, the impact, steps to
reproduce and a minimal proof of concept, with secrets, personal data and
exact locations removed. Only the latest `main` is supported.

## Trust boundaries

- **Public API:** read-only and returns published routes only. Query
  parameters are bounded by pagination, viewport limits and DRF throttles.
- **Administration:** `/admin/` is limited to the GitHub accounts listed by
  numeric ID in `GITHUB_OWNER_IDS`. Local username and password sign-up is
  closed.
- **Crawler:** fetched HTML is untrusted. Requests are limited to the
  configured BikeForum origins; DNS and every redirect are checked for
  private addresses; responses are size capped. Fetched markup is parsed,
  never rendered.
- **GPX import:** limited to HTTPS Mapy.com hosts with the same DNS and
  redirect checks. `defusedxml` rejects entity expansion and external
  entities, and byte, point and coordinate limits apply. Original payloads
  are kept in private storage only.
- **Reports:** protected by Turnstile, a honeypot and HMAC-keyed rate limits;
  raw client IP addresses are not stored.
- **Infrastructure:** PostgreSQL and Redis are internal only. Secrets come
  from the host environment and never from the frontend build.

## In CI

CodeQL fails on high and critical findings and Gitleaks scans the history.
`pip-audit` fails on any known vulnerability, `pnpm audit` on high and
critical ones. An accepted
dependency finding needs an entry with a reason and an expiry date in
`.github/audit-exceptions.json`. A failing weekly scheduled run opens a
`security-assurance` issue.
