# Strava API and cross-member display review

**Review date:** 2026-09-19  
**Status:** cross-member game blocked; personal-only fallback only; not legal advice

This is an engineering and product record of the primary Strava sources checked
for BikeMapy. It is not a legal opinion, does not grant permission to use
Strava data, and does not replace Strava's written approval or applicable
privacy advice. The maintainer must repeat this review before requesting new
scopes, changing what is shown, or enabling the module.

## Decision

BikeMapy must keep the cross-member competition disabled. The current Strava
API Agreement and API Policy say that data supplied by one user may only be
displayed to that user in the developer application and that data about other
users may not be displayed even when it is publicly viewable on Strava. The
Policy also prohibits the relevant aggregation and geographic-information
uses. A private invite, participant consent, or a Strava `Everyone` or
`Followers` setting does not by itself override those restrictions.

No implementation issue may treat participant consent as permission for
cross-member traces, profiles, or derived results. The cross-member design
requires a written response from Strava that specifically approves the
proposed geometry, profile, and derived-result uses, or a change to the
applicable policy. Until then, `GAME_ENABLED=false` remains mandatory.

The safe fallback is a separate personal-only mode: an authenticated player
may inspect that player's own Strava data and unshared source geometry in a
short-lived, purpose-limited view. It must not expose another participant's
data, create a leaderboard, compute a group or monthly aggregate, store a
territory/capture index, or display a Strava profile to anyone else. The
fallback is not permission to enable `/game`; its own implementation and
privacy review are still required.

## Outcome matrix

The outcomes below are product gates, not legal conclusions.

| Proposed use | Outcome on 2026-09-19 | Basis and implementation gate |
| --- | --- | --- |
| Authenticated player sees their own display name, profile image, activity geometry, or calendar date | Allowed for the same authenticated player, using only the scopes and data needed for the registered application purpose | API Agreement Highlights (same-user display/privacy), §§2.3 and 5.1–5.2; API Policy §§2.1–2.2, 6.1–6.4, 7.1–7.3. Keep the response owner-scoped and respect Strava visibility. |
| One participant sees another participant's activity geometry or calendar date | **Rejected** | API Agreement Highlights (same-user display/privacy) and API Policy §§2.3, 6.1–6.2 prohibit displaying or disclosing another user's data, including publicly viewable data. Do not enable based on competition consent alone. |
| One participant sees another participant's Strava display name or profile image | **Rejected** | Names, usernames, pictures, and geolocation are Personal Data under API Agreement §5.2; the same-user display rule applies. A BikeMapy nickname cannot be used to smuggle the underlying profile data into the competition. |
| Individual completion, capture territory, leaderboard, group-union completion, or monthly aggregate is shown across participants | **Rejected** | API Policy §§5.4 and 5.7 prohibit the relevant analytics/aggregation and geographic-information processing; §§6.1–6.4 and 7.4 also constrain derived data, retention, and deletion. Explicit member consent does not cure the cross-user restriction. |
| Individual completion or capture is calculated for one player and shown only to that player | **Blocked pending written Strava confirmation** | The same-user rule may allow a user-specific application view, but §5.4 reaches data derived from Strava Data and §5.7 expressly addresses geographic information. Do not assume that “personal-only” makes completion or capture permitted. The raw-geometry fallback remains available while this ambiguity is unresolved. |
| Retain imported activity geometry or a derived territory index indefinitely | **Rejected** | API Policy §5.5 forbids a Persistent Index and §6.2 limits cache retention to seven days; §6.4 limits retention to the original purpose. |

## OAuth scopes and participant consent

Request the smallest scope needed and inspect the scopes returned by OAuth;
Strava allows a user to opt out of requested scopes. The current developer
documentation describes these relevant scopes:

- `read` reads public profile data and other public resources.
- `activity:read` reads the authenticated user's activities visible to
  Everyone or Followers, excluding privacy-zone data.
- `activity:read_all` adds the authenticated user's Only You activities and
  privacy-zone data. It is not needed for the personal fallback and must not
  be requested without a separately approved product need.
- `profile:read_all` reads all profile information even when the user has set
  profile visibility to Followers or Only You. It is not needed for the
  personal fallback.
- `activity:write` is not needed; BikeMapy must not request it for a read-only
  completion view.

Strava's documentation says `activity:read` is required for activity webhooks.
The app must treat the returned scope set as authoritative and stop or reduce
the feature when a user grants less than the required scope. A public Strava
visibility setting is an input to what the authenticated user can read; it is
not redistribution permission.

Before any personal data is read, the OAuth and BikeMapy consent screens must
state, in plain language:

1. which data types are collected (identity fields, activity geometry, and
   calendar date, as applicable);
2. how data is collected (OAuth scopes, API reads, and webhook hints);
3. how the user withdraws consent or disconnects Strava;
4. how the user requests deletion; and
5. how BikeMapy confirms successful deletion.

The API Policy requires this information at minimum. A competition-specific
consent screen may be required by applicable law, but it cannot authorize
cross-member display that the Strava API rules reject. If the proposed sharing
scope changes, obtain new consent and re-check the policy before processing
the new type of data.

## Retention, disconnection, and deletion

The implementation must use the following strictest operational behavior:

- Keep Strava Data only for the registered purpose. Any transient cache has a
  hard maximum of seven days; remove a resource immediately when Strava no
  longer provides it. Do not create a search index, archive, vector store,
  territory index, or other Persistent Index.
- On an activity deletion or visibility change, stop displaying the affected
  data immediately when authoritative Strava state is known and reflect the
  deletion no later than 48 hours.
- On a user request, OAuth revocation/deauthorization, Strava account
  deletion, cessation of API use, or Agreement termination, permanently delete
  the applicable Strava Data and Personal Data derived from it from systems,
  networks, and servers under BikeMapy's control. Complete this expeditiously
  and no later than 30 days; provide written confirmation to the user and
  retain only the minimum non-content evidence needed to prove the operation.
- On explicit disconnect, stop synchronization, delete access and refresh
  credentials promptly, invalidate sessions, and prevent stale data from
  reappearing. Reconnection requires fresh OAuth authorization and must not
  restore data that the user requested deleted.
- A breach involving the Developer Application or Strava Data must be
  reported to `legal@strava.com` as soon as possible and no later than 24 hours
  after discovery. Keep tokens out of logs, frontend responses, telemetry,
  backups intended for ordinary application recovery, and documentation.

The existing BikeMapy backup policy must be reconciled with these obligations
before the Strava fallback is enabled. A live-database deletion deadline is
not a promise that an unexpired backup has been erased. The owner must define
how affected backups, replicas, queues, and caches are deleted or isolated and
record the evidence.

## Webhook trust model

Strava's webhook documentation describes one application-wide subscription,
activity and athlete events, a setup `GET` challenge using a verify token, and
event `POST` callbacks containing an object type, object ID, aspect, event time,
and update fields. It requires a `200 OK` within two seconds and retries up to
three times otherwise.

The documented setup challenge validates the callback URL during subscription
creation. The documented event payload does not describe a signature or a
per-event authentication proof. Therefore a later `POST` is an **untrusted
hint**, not proof that an activity changed and not proof of the activity's
contents or ownership. The callback handler must:

- validate the expected subscription, schema, event type, owner/activity
  relationship, and replay/idempotency key;
- acknowledge quickly and process asynchronously;
- rate-limit and deduplicate deliveries;
- re-fetch authoritative state with the affected user's token before importing,
  publishing a score, or performing irreversible deletion;
- reconcile connected users periodically, because callback delivery is not a
  complete synchronization guarantee; and
- quarantine an unverifiable delete or deauthorization hint. A transient
  network error, `429`, or provider outage is not an authoritative deletion.

Never use a webhook payload to grant access, bypass an OAuth scope, publish a
result, revoke credentials, or perform irreversible local deletion on its own.

## Quotas, registration, and service limits

The current developer documentation lists default per-application limits of
200 requests per 15 minutes and 2,000 per day overall, with a separate
non-upload limit of 100 per 15 minutes and 1,000 per day. The response headers
report usage and limits; `429 Too Many Requests` must trigger bounded backoff,
not retries that amplify load.

The current API Policy describes a Standard Tier limited to 10 registered
athletes for hobby/early development or up to 9,999 registered athletes for a
larger application, with Extended Access above that only after Strava
approval. Admission, athlete-capacity increases, and rate-limit increases are
discretionary. BikeMapy must not promise a competition size or use repeated
polling/backfills to bypass these limits.

## Branding, attribution, and notices

If the integration uses Strava branding, use only the current official assets
and wording. The Brand Guidelines require the official “Connect with Strava”
button to link to Strava's OAuth URL, prohibit any implication of affiliation
or endorsement, and require “View on Strava” for a link back to an original
Strava source. Strava's API Policy §§4.1–4.6 also reserve the right to revoke
mark usage, prohibit confusing app names, and require prior written consent
for press statements referring to Strava.

Before enabling even the personal fallback, the public BikeMapy Privacy Notice
must link to this decision and explain the Strava data categories, scopes,
purpose, OAuth/deauthorization behavior, cache boundary, deletion process,
subprocessors, and Strava's usage-data statement. The Terms must not promise
cross-member sharing while this gate is blocked. The UI must include a
prominent link to Strava's current privacy policy and provide support/contact
and deletion-request paths.

## Personal-only fallback

While cross-member rights are rejected or unresolved, the only safe product
slice is:

1. one authenticated player authorizes the registered BikeMapy application;
2. the player can view that player's own Strava identity and eligible activity
   geometry/date, subject to the granted scope and Strava privacy settings;
3. the view is owner-scoped, has no member list, invite-based sharing,
   leaderboard, group union, monthly aggregate, capture territory, or public
   link; and
4. data is processed on demand with the seven-day maximum cache and the
   deletion/disconnection controls above.

If Strava confirms in writing that a user-specific derived completion or
capture view is allowed, record that response and update this document before
adding it. Until then, the fallback deliberately shows source geometry only.

## Launch blockers and re-review triggers

The following are blocking questions, not assumed permissions:

1. Obtain written Strava confirmation for any cross-member geometry, profile,
   or derived-result use, or remove those features from the product.
2. Confirm the final developer registration, access tier, athlete capacity,
   quotas, and any subscription requirement.
3. Implement and test OAuth scope minimization, consent withdrawal, deletion
   confirmation, 48-hour deletion reflection, and 30-day erasure across live
   data, queues, caches, replicas, and backups.
4. Publish the required BikeMapy privacy/contact/deletion notices and obtain
   the operator and legal review required by the existing launch gate.
5. Re-review this document whenever Strava changes the Agreement, Policy,
   scopes, webhook behavior, rate limits, brand requirements, or the product's
   data-sharing scope.

## Primary sources checked

All web sources below were retrieved on **2026-09-19**. Effective/revision
dates are shown where the source publishes one.

| Source | Date and relevant sections |
| --- | --- |
| [Strava API Agreement](https://www.strava.com/legal/api) | Effective 2026-06-01; Highlights (same-user display/privacy), §§2.3, 4.4, 5.1–5.2, 6.2, 8.1, 9.1, 11.1, 14.6. The Agreement incorporates the API Policy, Terms, Privacy Policy, and Brand Guidelines. |
| [Strava API Policy](https://www.strava.com/legal/api_policy) | Effective 2026-06-01; §§2.1–2.5 (authentication, consent, same-user display, deletion), §§3.1–3.7 (limits and tiers), §§4.1–4.6 (marks and attribution), §§5.1–5.16 (purpose, aggregation, geography, transfer, consent), §§6.1–6.6 (display, seven-day cache, deletion reflection, purpose retention), §§7.1–7.7 (privacy, deletion, controllers, subprocessors), and §§8.1–8.4 (security and 24-hour breach notice). |
| [OAuth authentication and scopes](https://developers.strava.com/docs/authentication/) | Retrieved 2026-09-19; OAuth consent, reduced granted scopes, minimum-scope guidance, `read`, `activity:read`, `activity:read_all`, `profile:read_all`, and webhook scope requirement. |
| [Rate limits and athlete capacity](https://developers.strava.com/docs/rate-limits/) | Retrieved 2026-09-19; default overall/non-upload quotas, `429` behavior, response headers, and capacity/review process. |
| [Webhook Events API](https://developers.strava.com/docs/webhooks/) | Retrieved 2026-09-19; event fields, setup challenge/verify token, two-second `200 OK`, three delivery attempts, deauthorization/activity events, and one-subscription limit. |
| [API Brand Guidelines](https://developers.strava.com/guidelines/) | Last revised 2025-09-29; retrieved 2026-09-19; Connect with Strava, logo separation, “View on Strava,” factual references, and no implied endorsement. |
| [Strava Privacy Policy](https://www.strava.com/legal/privacy) | Effective 2026-01-01; retrieved 2026-09-19; profile/activity/location data categories, visibility controls, third-party sharing, and user privacy choices. It does not relax the developer-specific API restrictions above. |
