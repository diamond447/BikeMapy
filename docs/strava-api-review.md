# Strava API agreement and cross-member display review

**Review date:** 2026-09-07
**Status:** blocked release gate; not legal advice
**Related issue:** [#65](https://github.com/diamond447/BikeMapy/issues/65)

This document records the provider-policy review required before BikeMapy can
implement or enable the private Strava game. It is an engineering decision
record, not legal advice, and it does not grant permission from Strava or from
any participant. `APPROVED` below means approved for the stated product
boundary only; it does not override a provider agreement or applicable law.

## Executive decision

The cross-member game is **BLOCKED**. BikeMapy has not obtained a verified,
current basis for redistributing one athlete's Strava activity data, profile
fields, or results derived from that data to other athletes. A player's
consent is necessary for a product privacy model, but it is not evidence that
Strava permits the redistribution. Public, Everyone, or Followers visibility
is an input to the import policy and is not treated as a redistribution
licence.

Keep `GAME_ENABLED=false` in every deployed or live-data environment. Automated
tests may exercise the game code with synthetic or mock data and no Strava
credentials or provider network access. Do not configure production Strava
credentials or return private game data while this gate is unresolved. Do not
enable a personal-only mode either: that mode still needs a separate review of
the current agreement, retention, deletion, and privacy basis. The
personal-only shape defined in [the game specification](game-spec.md#safe-personal-only-fallback)
is the safe fallback to implement only after that separate decision.

## Source register and access record

The following are the authoritative Strava-owned sources selected for this
review. They were checked on 2026-09-07. Direct requests to the Strava domains
were blocked by the review environment's network policy, so their current
contents could not be independently retrieved or quoted. This is an access
limitation, not a finding that the sources contain no restriction. No
rights-dependent outcome below is marked approved on the basis of an
unretrieved page.

| Source | Intended question | Access/result on 2026-09-07 |
| --- | --- | --- |
| [Strava API Agreement](https://www.strava.com/legal/api-agreement) | Permitted API use, redistribution, derived data, retention, deletion, and termination obligations | **Not retrieved**: direct `www.strava.com` access was blocked. Agreement terms must be re-read from the live page or written provider confirmation before release. |
| [Strava Terms of Service](https://www.strava.com/legal/terms-of-service) | User-facing rights and restrictions relevant to connected activity data | **Not retrieved**: direct `www.strava.com` access was blocked. |
| [Strava Privacy Policy](https://www.strava.com/legal/privacy) | Provider privacy, deletion, and disclosure context | **Not retrieved**: direct `www.strava.com` access was blocked. |
| [API documentation](https://developers.strava.com/docs/) | Current API contract and provider implementation guidance | **Not retrieved**: direct `developers.strava.com` access was blocked. |
| [Authentication and scopes](https://developers.strava.com/docs/authentication/) | OAuth flow, access scopes, token lifecycle, and deauthorization | **Not retrieved**: direct `developers.strava.com` access was blocked. |
| [API reference](https://developers.strava.com/docs/reference/) — [logged-in athlete](https://developers.strava.com/docs/reference/#api-Athletes-getLoggedInAthlete), [activity list](https://developers.strava.com/docs/reference/#api-Activities-getLoggedInAthleteActivities), [activity detail](https://developers.strava.com/docs/reference/#api-Activities-getActivityById), and [activity streams](https://developers.strava.com/docs/reference/#api-Streams-getActivityStreams) | Athlete, activity, map, date, and stream fields and endpoint semantics | **Not retrieved**: direct `developers.strava.com` access was blocked. |
| [Webhooks](https://developers.strava.com/docs/webhooks/) | Subscription challenge, callback schema, event trust, and deauthorization callbacks | **Not retrieved**: direct `developers.strava.com` access was blocked. |
| [Rate limits](https://developers.strava.com/docs/rate-limits/) | Per-application quotas, response headers, and backoff requirements | **Not retrieved**: direct `developers.strava.com` access was blocked. Do not hard-code a quota from a secondary source. |
| [Branding guidelines](https://developers.strava.com/docs/guidelines/) | Required Strava name, logo, links, attribution, and presentation | **Not retrieved**: direct `developers.strava.com` access was blocked. |
| [Strava's `go.strava` repository](https://github.com/strava/go.strava) | Official repository cross-check and provenance of old API client material | Retrieved 2026-09-07. Its README says the client and documentation are no longer updated; it is therefore not evidence of current policy or permission. |

The repository README was useful only to confirm that old client material is
not a safe substitute for the live developer documentation. Unofficial
mirrors, generated clients, blogs, and community answers were deliberately
not used to decide rights or restrictions.

## Decision matrix

| Use in BikeMapy | Outcome | Decision and consequence |
| --- | --- | --- |
| OAuth athlete ID, display name, and profile image | **BLOCKED for release** | The API may expose identity fields, but current agreement, display, and storage permissions were not verified. Use the immutable provider ID for account matching only after #47's gate is approved. Never make a display name or image the identity key. |
| A connected player's own activity geometry and calendar date | **BLOCKED for release** | A private, own-account view is the personal-only fallback shape, not current permission. It cannot be enabled until the live agreement and retention/deletion requirements are verified and separately approved. |
| One player's geometry/date shown to other competition members | **BLOCKED** | No verified permission to redistribute location history across athletes. Participant opt-in does not cure an unverified provider restriction. Do not implement or expose this view. |
| One player's profile image/name shown to other members | **BLOCKED** | Same result as geometry. A private competition and consent do not establish provider permission. Use no cross-member Strava profile fields. |
| Individual completion derived from one player's activities and shown to that player | **BLOCKED for release** | A derived result may still be subject to the agreement and source-data retention rules. Revisit after the current policy is retrieved. |
| Individual completion shown to other members | **BLOCKED** | No verified permission to disclose a result derived from another athlete's Strava data. |
| Group-union completion, capture territory, leaderboards, or monthly aggregates | **BLOCKED** | These combine or derive from multiple athletes' data and are the highest-risk sharing scope. Do not calculate, persist, or publish them for a live integration without written provider confirmation or a verified current contractual basis. |
| Competition-specific participant consent | **APPROVED as a required product control; insufficient alone** | Record explicit, per-competition, informed opt-in and allow withdrawal. Consent must describe geometry/date, identity fields, derived views, history scope, retention, deletion, and withdrawal. It does not grant Strava permission and cannot lift a `BLOCKED` provider outcome. |
| Retention, reconnection, disconnect, deauthorization, and deletion | **BLOCKED for release** | The game specification defines conservative engineering behavior, including prompt credential removal, recomputation, and a bounded 30-day reconnection window. The provider's current requirements and backup obligations still need verification. No retention promise may be presented as provider-approved. |
| Branding, attribution, and links | **BLOCKED for release** | Preserve a provider link and a dedicated attribution slot in the design, but do not ship Strava marks or wording until the current branding guidance is retrieved and checked. |
| Webhook setup and event processing | **BLOCKED for live integration; safe trust model APPROVED** | The callback endpoint may be designed as an untrusted input. Validate the setup challenge only for subscription setup, validate schema/subscription/object/owner fields, rate-limit and deduplicate, and re-fetch authoritative state before changes. A POST must not be treated as signed or intrinsically authentic unless the live docs explicitly document such a guarantee. |
| API quotas and backoff | **BLOCKED for live integration; engineering control APPROVED** | Use bounded queues, response-header-aware throttling, exponential backoff, idempotency, and reconciliation. Do not assume a numeric quota until the live rate-limit page or provider confirmation is available. |

## Safe personal-only fallback

If Strava confirms a personal use case but not cross-member sharing, BikeMapy
may consider a separately approved, separately flagged mode with all of these
properties:

- a player sees only their own eligible geometry and own derived completion or
  capture result;
- no other player's geometry, date, profile field, nickname, score, or
  aggregate is returned, calculated for display, or placed in a shared cache;
- there are no competitions, invites, group unions, territory comparisons,
  leaderboards, or monthly group aggregates in that mode;
- the same OAuth, credential, deletion, retention, webhook, rate-limit, and
  no-store controls apply; and
- `GAME_ENABLED` remains false for the cross-member module. A possible future
  `PERSONAL_ONLY_MODE` flag must default to false and requires its own approved
  issue and policy decision.

This is a product safety fallback, not a conclusion that Strava currently
authorizes the mode.

## Issue and rollout consequences

| Issue | Consequence of this review |
| --- | --- |
| #47 Strava sign-in and player accounts | **Blocked for a live integration.** Documentation-only OAuth interfaces and tests may be prepared behind the disabled flag, without production credentials or a claim that the policy gate passed. |
| #49 activity synchronization | **Blocked for a live integration.** Do not import, retain, or process real Strava activity data until the agreement, scopes, retention, webhook, and deletion decisions are approved. |
| #46 invite-only competitions | The non-data competition shell may be built behind `GAME_ENABLED=false`, but it must not expose members, previews, or game data and must not be presented as enabled. |
| #50, #52, #54, #55, #56, #57 | **Blocked for real Strava data and release.** Fixtures and pure algorithm design may proceed with synthetic data only; no cross-member result may be calculated, persisted, or displayed from provider data. |
| #51 and #53 official reference routes | Independent source-rights review remains required by the game specification. They may proceed separately, but do not use Strava data as evidence of completion. |
| `GAME_ENABLED` | Must remain `false` in every deployed or live-data environment until the provider gate, consent UX, source rights, deletion/retention behavior, and rollout evidence are approved. Isolated automated tests may use synthetic/mock data with no provider credentials or network. |

## Required unblock evidence

Before #47 or #49 is enabled, the maintainer must re-run this review from an
environment that can retrieve the live primary sources, record their version
or effective date, and resolve at least:

1. whether cross-athlete display of geometry, calendar date, profile fields,
   and derived results is permitted;
2. whether participant consent changes that permission or only documents the
   product's own privacy basis;
3. whether group unions, capture, rankings, and monthly aggregates are
   permitted and under what restrictions;
4. approved OAuth scopes and limits on history and privacy-filtered geometry;
5. retention, deauthorization, deletion, reconnection, and backup treatment;
6. webhook authenticity guarantees, callback handling, and reconciliation;
7. current quotas, branding, attribution, and required links/notices; and
8. a dated owner approval of the resulting product and legal decision.

Until all eight items are resolved, unresolved questions remain launch
blockers and no implementation issue may rely on an assumed permission.

## Related records

- [Private Strava completion game specification](game-spec.md)
- [Legal and attribution review](legal-review.md)
- [Privacy notice](privacy.md)
- [Removal policy](removal-policy.md)
- [Terms](terms.md)
