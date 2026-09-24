# Private Strava Completion Game Specification

## Document status

This document is the authoritative product and engineering specification for
the private game module. It is a design baseline for the implementation issues
listed below; it is not a promise that the module is enabled in production.
Decisions that change these rules require an issue and an explicit update to
this document.

The public BikeMapy catalogue has a separate source of truth in the
[public product specification](product-spec.md). This document complements
that specification and must not replace or redefine the public catalogue.

## Product boundary and intent

BikeMapy has two independently usable product areas:

1. The public catalogue discovers and presents cycling routes from BikeForum
   sources. It is anonymous, public, and useful without a player account or
   Strava connection.
2. `/game` is an authenticated, invite-only social cycling module. Members of
   a private competition see imported activity geometry in member-selected
   colors and can inspect completion and capture progress.

The game is a private “fog of war” over cycling. A player’s eligible activity
is imported once, then exposed through separate competition views over the same
activity record. Completion answers “how much of a known reference route has
been covered?” Capture answers “which bounded areas does a player’s accumulated
network currently own?” These are switchable views, not separate activity
imports or competing sources of truth.

The game must be disabled by default (`GAME_ENABLED=false`). A deployment with
the flag disabled and no Strava credentials must still build, start, and serve
the public catalogue. In that state the game frontend and private endpoints
are unavailable or return a deliberate not-found/disabled response, and no
private data is returned. Enabling the game is a separately controlled rollout;
it must not make Strava configuration a prerequisite for the public catalogue.

## Initial scope and non-goals

The initial official completion catalogue contains:

- Via Czechia routes, including their individual stages; and
- numbered Czech cycling routes.

Achievements, EuroVelo, additional national or regional catalogues, public
competitions, teams within a competition, seasons or fixed end dates, manual
territory editing, first-rider awards, and public sharing are outside the
initial scope. Capture and completion are also not achievements or first-rider
awards: they are deterministic derived views with documented evidence.

## Domain concepts

| Concept | Rule |
| --- | --- |
| Player | A person identified by an immutable Strava athlete ID. The account stores the imported Strava display name and profile image, plus an optional BikeMapy nickname. |
| Player lifecycle | A player is `connected`, `disconnected`, `pending-deletion`, or `deleted`. The lifecycle is independent of GitHub owner-admin access. |
| Competition | A private, invite-only container with a name, reusable unique invite code, owner, creation time, and active state. |
| Membership | A player may belong to multiple competitions and may select one active competition. Membership is the authorization boundary for all competition data. |
| Owner | The competition member allowed to rename, rotate the invite code, remove members, transfer ownership, and delete the competition. |
| Invite code | The only way to join an existing competition. Creation makes the creator the initial owner/member without an invite. A rotated code stops accepting the old value immediately. Invitations are reusable, not one-time. |
| Competition color | Each member chooses a color independently for each competition. The color must be sufficiently distinguishable from colors already used by that competition. |
| Eligible activity | An outdoor cycling activity of an allowed type with usable Strava-returned geographic geometry and a privacy level visible to Everyone or Followers. |
| Shared imported activity | One owner-scoped imported activity that may contribute only to a competition with a current recorded per-competition sharing opt-in and only while the Strava agreement/display gate permits that sharing. Pending or non-consenting memberships receive nothing. Its private source record is not duplicated as a separate import per competition. |
| Reference route | A versioned, provenance-aware Via Czechia route/stage or numbered Czech route approved by the source-evaluation decision. |
| Completion claim | Covered reference-route geometry and percentage for one player or the spatial union of a competition’s members. |
| Capture claim | A bounded polygon face derived from a player’s accumulated eligible trace network and its chronology. |

## Pre-implementation Strava agreement and display gate

See the dated [Strava API and cross-member display review](strava-api-review.md)
for the current decision. As of 2026-09-19, cross-member geometry, profile
fields, and derived competition results are rejected or blocked by the current
Strava API Agreement and API Policy. This specification remains a product
design baseline, not permission to implement or enable those features.

Completing the #65 review records the current rejection/blocker; it does not
unblock #47, #49, or any downstream cross-member or derived-result issue. The
affected issues remain blocked until Strava gives written approval for the
proposed uses and the conflicting issue scopes receive approved revisions.
Only a separately approved and reviewed personal-only issue may proceed while
that cross-member gate remains blocked.

Before implementing #47 or #49, the operator must complete a dedicated,
approved review of the current Strava API Agreement, developer terms,
documentation, and display policies. The review must be recorded in a
dedicated approved issue (or an explicitly approved update linked from this
specification); it must not be silently inferred from an API response or
assumed because Strava makes an activity visible to the athlete, Everyone, or
Followers.

At minimum, the decision must cover whether Strava permits this product to:

- display one participant’s activity geometry and calendar date to other
  participants in the same private competition;
- display a participant’s Strava profile image and display name to those
  participants, alongside a BikeMapy nickname;
- calculate and display derived individual, group-union completion, capture,
  leaderboard, and monthly aggregate results to other participants;
- retain imported geometry, derived projections, and the minimum chronology
  metadata for the documented period, including deletion and reconnection;
- process account disconnection, deauthorization, deletion, and webhook
  callbacks under the applicable terms;
- use Strava branding, attribution, links, and other required notices; and
- obtain and record informed participant consent for each competition and for
  any change in sharing scope.

Public availability or `Followers` visibility is an eligibility input for the
import policy, not permission to redistribute activity data to other people or
to publish derived results. This specification does not grant that permission.
The review must record any limitations, required wording, retention/deletion
conditions, and attribution obligations. If rights are unresolved or denied,
keep the cross-member game slice disabled (`GAME_ENABLED=false`). The current
personal-only fallback is deliberately narrow: a connected player may view
their own source geometry and date in an owner-scoped view, but not capture
territory, profile data for other people, leaderboards, or any group/monthly/
other derived aggregate. A user-specific completion or capture result remains
blocked until Strava confirms that the derived use is permitted. This fallback
is a separately reviewed and separately flagged mode (for example,
`PERSONAL_ONLY_MODE=false`), not an activation of `/game`; it requires its own
privacy and retention decision. Personal-only behavior is not approval to
activate the cross-member game.

The gate is a release blocker for cross-user display and derived processing.
Any changed Strava policy or product sharing scope requires a new approved
decision and an update to this specification before implementation or
enablement.

## Player and Strava lifecycle

### Authentication and account identity

Strava OAuth is the registration, authentication, and activity-connection
mechanism for players. The flow supports authorization, callback, token
refresh, session login, logout, reconnection, and revocation handling.

- Match returning players by immutable Strava athlete ID, never by mutable
  display name, email, or profile image.
- Handle invalid OAuth state, callback replay, denied consent, revoked access,
  and refresh failure safely and without creating a second account.
- Request only the scopes needed for activities visible to Everyone or
  Followers. Do not request access to `Only You` activities.
- Keep GitHub owner-admin authentication separate from player authentication.
  A Strava player is not thereby an owner-admin, and owner-admin authorization
  must not grant access to a player’s private game data without the applicable
  competition membership.
- Provide only the authenticated endpoints required by the `/game` frontend.

OAuth access and refresh credentials are secrets. Store them using the
deployment’s secure secret-storage/encryption policy, never expose them in
frontend responses, APIs, admin pages, or logs, and redact them from error
telemetry. OAuth callback state and session cookies must be protected against
forgery and replay.

### Disconnect, reconnection, and deletion

- Explicit disconnect or provider deauthorization stops synchronization
  immediately. Revoke the provider authorization where Strava supports it,
  delete local access and refresh credentials promptly, invalidate active
  player sessions and pending OAuth state, and begin deleting all applicable
  imported Strava Data and derived competition data immediately. Complete the
  deletion expeditiously and no later than 30 days; this deadline is an outer
  limit, not a reconnection or retention window. Transient caches are purged
  promptly and never exceed the seven-day maximum. The player may reconnect
  only through a fresh OAuth flow after the deletion path has started.
- A successful reconnection does not cancel deletion or restore deleted data;
  it starts a fresh owner record/import after fresh authorization and must not
  duplicate the player or activities. No imported activity, derived result,
  profile field, or immutable athlete-ID linkage may be retained to support a
  hypothetical reconnection.
- When the deletion completes, deterministically recompute affected
  competitions. The bounded, non-content deletion tombstone and
  backup/replica treatment described below are the only possible remaining
  records; they must not contain Strava Data, derived Personal Data, or an
  athlete identifier.
- A BikeMapy account deletion removes tokens, activities, memberships, and
  derived results immediately and triggers the same recomputation path.
- Deleting a competition removes that competition and its derived data, but
  does not delete the player account or shared imported activities used by the
  player’s other competitions.

Account deletion must never be blocked by unresolved competition ownership.
In the normal UI, an owner must transfer ownership or delete each owned
competition before leaving. If account deletion proceeds without that step,
the deterministic privacy-safe fallback is to delete every competition owned
by the account, including its memberships and derived data, before erasing the
account. It must never silently retain the deleted owner’s identity to keep a
competition alive.

Account deletion removes the profile display name, profile image, BikeMapy
nickname, sessions and OAuth state, provider tokens, imported activities,
memberships, and all derived projections. It triggers deterministic
recomputation for surviving competitions affected by other members’ data.
The implementation must define a bounded-retention, non-content audit/tombstone
record containing only what is necessary to prove that deletion ran (for
example, a random deletion event ID, timestamp, action status, and object
types/counts). It must contain no geometry, profile fields, athlete ID,
credentials, invite code, or other secret. That retention decision and its
backup/replica treatment require an approved implementation issue before
release; this specification does not promise indefinite audit retention or
instant erasure from a backup that has not actually been removed.

## Private competitions and membership

Every authenticated player may create a competition. The creator becomes its
initial owner and member through the creation flow; no invite is needed for
that initial membership, but the creator must complete the sharing opt-in
before any cross-member data is returned. A player joins an existing
competition only through its valid current invite code and may switch between
multiple competitions. There are no public competitions or one-time
invitations in this scope.

Connecting Strava, opening an invite, or creating a competition does not by
itself authorize cross-member sharing. Before a player’s activity, profile
image/name, or derived results can be shown to another member, the player must
give an informed, explicit opt-in for that specific competition. The consent
screen must state, in plain language:

- that the default import covers the previous 12 months and that requesting
  complete available history is a separate explicit action;
- that opted-in competition members may see the player’s eligible activity
  geometry and calendar date, profile image/name or BikeMapy nickname, and the
  resulting completion, capture, leaderboard, and group aggregate views;
- the retention, disconnect/deauthorization, recomputation, and deletion
  behavior; and
- how to withdraw consent, leave the competition, or delete the account.

The competition creator receives this disclosure and must opt in before any
cross-member data is returned. A player joining an existing competition via a
reusable invite code receives a privacy-minimal preview before accepting: the
competition name and, only if approved by the sharing policy, a non-identifying
member count. The preview never reveals the owner’s or any member’s identity,
profile image, nickname, activity, or derived result. Sharing scope and
12-month/full-history scope are explanatory policy text, not a disclosure of
current members. The invite alone is not membership or consent. Membership can
remain pending consent, but no private trace, profile field, or derived result
is exposed until the opt-in is recorded. Withdrawal removes that player’s
competition-derived projections and sharing promptly, then schedules
deterministic recomputation. A new opt-in is required if the sharing scope
materially changes, including a newly requested full-history import.

The owner may:

- rename the competition;
- rotate its reusable invite code, invalidating the previous code immediately;
- remove a member;
- transfer ownership; and
- delete the competition.

A non-owner member may leave voluntarily. An owner cannot leave while still the
owner: ownership must first be transferred or the competition must be deleted.
Removing a member or accepting a member’s departure removes that member’s
competition-derived results and schedules deterministic recomputation. Their
shared imported activities remain available for other competitions in which
they participate.

The API must enforce membership and ownership at the object level on every
competition endpoint. An unauthenticated user, a player from another
competition, and a former member must not enumerate or retrieve data by
guessing competition, membership, activity, route, or result identifiers.
Invite-code validation must be rate-limited and must not disclose whether
unrelated codes or competition identifiers exist. Codes are bearer secrets;
they must not appear in logs, analytics, public URLs, or error details.

The active competition is a private session/navigation preference. It may
survive ordinary authenticated navigation, together with member visibility
filters, but must not be encoded in a publicly cacheable URL in a way that
discloses membership or private data.

## Activities and import policy

### Eligibility and source geometry

Import outdoor cycling activities in these allowed categories, where Strava
provides usable geometry:

- ride;
- mountain-bike ride;
- gravel ride; and
- e-bike ride.

Exclude virtual activities, activities without usable geographic geometry, and
activities marked `Only You`. Use only the geometry returned by Strava after
Strava’s privacy controls. Never reconstruct, infer, or “fill in” hidden
portions of a track.

After initial connection, import the previous 12 months by default. Provide an
explicit player settings action for requesting the complete history available
from Strava; do not silently perform an unlimited historical backfill.

Process Strava webhook callbacks for created, updated, deleted, and
privacy-changed activities as untrusted hints. Under Strava’s webhook model,
the subscription `GET` challenge and verify token validate webhook setup only;
they do not make later `POST` callbacks signed or intrinsically authentic. The
importer must therefore:

- validate the callback schema and expected subscription, object/activity,
  owner/athlete, and event fields;
- rate-limit and deduplicate callbacks, then re-fetch the authoritative
  activity state from Strava where possible before importing or removing it;
- periodically reconcile connected players and handle provider deauthorization
  independently of callback delivery; and
- quarantine or mark reconciliation pending when a delete or deauthorization
  hint cannot be re-fetched safely. An unsigned hint must never directly cause
  irreversible local deletion, credential revocation, a lifecycle transition,
  or score publication. Corroborate it through an authenticated Strava API
  response/token state or another documented trusted reconciliation signal
  before taking those actions. An authenticated 404 or other authoritative
  absence permits removal; transient errors, rate limits, and unavailable
  provider responses remain pending. Deauthorization without a re-fetch also
  remains quarantined unless that trusted signal is documented and accepted.

The import and callback pipeline must be:

- idempotent, so duplicate delivery and retries cannot duplicate activities or
  scores;
- resumable, with visible backfill progress and retryable failures;
- rate-limit aware, using bounded queues and backoff; and
- safe to retry after interruption.

Issue #49 currently describes “verified” webhook events and requires webhook
“authenticity” validation. That wording and acceptance criterion must be
clarified in an approved issue/update before implementation: setup challenge
validation is not POST signature verification, unsigned delete/deauthorization
hints cannot directly perform irreversible actions, and the controls above
are the required trust model.

Store only the source metadata required for synchronization, chronology,
geometry processing, and audit. Internally, chronology may require the source
timestamp, but it is not a member-visible field. Other Strava metadata such as
title, speed, exact time, description, and private source details are not
exposed to competition members.

### Member visibility and sharing

Other members can see only an eligible activity’s geometry and calendar date,
rendered using its owner’s competition color. An individual trace inspection
shows the date only; it does not reveal exact time, title, speed, or unrelated
Strava metadata. The owner’s same imported activity may contribute to each
competition only while that owner has a current recorded sharing opt-in for
that competition and the Strava agreement/display gate permits cross-member
sharing. Pending, withdrawn, or non-consenting memberships receive no trace,
profile, or derived result. The private source record is not duplicated as a
separate import per competition, and one competition is never made visible to
another.

When an activity is deleted or becomes `Only You`, remove it from the local
eligible set and trigger downstream completion and capture recomputation. A
privacy change that makes it ineligible has the same effect as deletion.

## Reference routes, provenance, and rights

The initial catalogue is restricted to Via Czechia (parent routes and stages)
and numbered Czech cycling routes. A numbered route number is not assumed to
be globally unique; source identity and collection context are part of its
identity.

Before importing any source, issue #51 must record a dated decision covering:

- source URL, ownership, licence or permission basis, and coverage;
- required attribution and redistribution restrictions;
- stable source identifiers or an explicit BikeMapy identity strategy;
- update mechanism, expected geometry quality, and version/provenance needs;
  and
- a validation sample for missing, duplicated, disconnected, incorrectly
  ordered, empty, invalid, or otherwise pathological geometry.

OpenStreetMap relations and other sources must be evaluated explicitly. OSM
data brings ODbL attribution and notice obligations; a publicly viewable route
or map is not evidence of permission to copy, transform, or redistribute it.
Via Czechia data requires its own authoritative permission/licence decision.
Unresolved permission, licence, attribution, or redistribution questions are
blockers for ingestion. Do not invent a licence or treat this specification as
legal approval.

If Via Czechia cannot legally or technically be imported, the safe fallback is
to omit Via Czechia from completion results, show a clear unavailable/source
pending state, and continue only with independently approved numbered-route
sources. Do not scrape, rehost, or derive a substitute Via Czechia catalogue
while rights or geometry are unresolved.

Issue #53 imports only approved sources and models collections, parent routes,
optional stages, source identifiers, versions, geometry, provenance,
attribution, and active status. Normalize geometry for matching while retaining
source geometry and provenance. Unchanged reimports are idempotent; meaningful
source changes create an auditable new version and schedule recomputation
instead of silently replacing history. Invalid records fail in isolation with
actionable diagnostics. Required attribution is available to the authenticated
completion interface.

The existing [legal and attribution review](legal-review.md) remains the
authority for current BikeMapy provider decisions and launch blockers. The
dated [reference-route source decision](reference-route-sources.md) records
the separate OSM and Via Czechia evaluation required by this section. In
particular, neither review approves future game reference-route ingestion
merely because a source is publicly visible.

## Completion model

Completion and activity maps consume the same eligible imported activities.
For each reference route, compare activity geometry with an initial 50-metre
corridor. The tolerance is configurable and must be validated by fixtures and
benchmarks before it is treated as a production invariant. Direction is
irrelevant.

Count unique covered reference-route length. Repeated rides and overlapping
activities do not inflate coverage. For a player, completion is the covered
length divided by the valid length of the selected route version. For a
competition, group completion is the spatial union of member coverage, not the
sum of individual percentages.

Calculate both the complete Via Czechia route and each of its stages. Keep
parent/stage relationships and evidence sufficient to recompute after an
activity deletion, privacy change, member change, or reference-route version
change. Calculations run asynchronously, are idempotent, and expose explicit
`fresh`, `pending`, and `failed`/stale states; a pending or failed value must
not look like a final score.

Monthly newly completed distance is tracked for each player and competition
group using the `Europe/Prague` calendar. A reference segment is counted only
once for the same subject, even if it is ridden repeatedly in that month or
was covered by overlapping activities. Precision, percentage rounding,
zero-length/sliver treatment, invalid geometry handling, and the configured
50-metre policy must be documented with deterministic fixtures and benchmark
results.

The completion view under `/game` must:

- list Via Czechia routes/stages and numbered Czech routes;
- show individual and group-union percentages clearly distinguished;
- allow selecting a player or the combined competition progress;
- frame the complete reference geometry and distinguish covered from
  uncovered portions;
- show monthly newly completed distance;
- show source attribution and pending, stale, empty, or failed states; and
- preserve the active competition and relevant route/stage selection during
  authenticated navigation.

## Capture model

Capture is a switchable map mode over the same eligible activity data. Activity,
completion, and capture modes must not silently switch competition context or
refetch data from another competition.

### Topology and geometry policy

Each player’s eligible activities form an accumulated trace network. Nearby
traces may be connected using the initial 50-metre tolerance after the topology
validation in issue #55. The validated algorithm converts the network into
bounded polygon faces; unbounded exterior area is not owned. It must handle and
test noisy GPS, multi-day boundaries, intersections, nested loops, overlapping
claims, same-day claims, self-intersections, disconnected traces, invalid
geometry, and large worldwide extents. Issue #55 must record the selected
algorithm and rejected alternatives, with executable fixtures and expected
ownership results for every product example.

Invalid or pathological input must fail safely, preserving the last valid
result and scheduling a bounded retry rather than corrupting existing
ownership. Sliver handling, deterministic tie-breaking, safe recalculation
bounds, and accuracy/performance budgets are part of the validated algorithm.
Worldwide polygons require an appropriate projected or geodesic area method;
an unexamined single local projection is not sufficient.

### Chronology and ownership

The effective claim date of a polygon is the date of the oldest trace segment
required to complete that polygon’s boundary. Chronology uses the
`Europe/Prague` calendar date, including same-day ties, and is independent of
webhook delivery order.

- A completely newer boundary wins an overlap over an older effective claim.
- An older partial boundary cannot override an entirely newer boundary merely
  because the older boundary was completed later (the “bitten apple” case).
- A player must establish an entirely newer boundary to retake existing
  territory.
- If competing effective claim dates fall on the same Prague calendar day,
  all qualifying players share ownership of that area.
- Shared ownership is represented by all owner identities, with deterministic
  rendering and tie behavior. It is not assigned to whichever webhook arrived
  first.

Issue #57 persists current worldwide ownership, per-player currently owned
area, calculation/version evidence, and recomputation inputs. Activity changes,
membership changes, account deletion, and algorithm-version changes trigger
affected recalculation. Full rebuild and incremental processing must produce
equivalent results; a failed recalculation leaves the last valid result
identifiable and retryable.

## Game map and leaderboard experience

The `/game` shell requires Strava authentication and active competition
membership before returning private map data. It renders worldwide activity
geometry in each member’s competition color and provides independent member
visibility toggles. Hiding a member changes presentation only; it never changes
completion unions, capture ownership, or scores.

The map API returns viewport- and zoom-appropriate bounded/simplified geometry
with explicit response bounds. Spatial indexes, simplification, caching, and
bounded queries must preserve an agreed dense-dataset performance budget.
Private geometry, territory, and leaderboard data must never be indexed by
search engines or stored in a public/shared cache.

The capture view renders:

- currently owned territory in each owner’s competition color;
- same-day shared territory with distinguishable stripes containing every owner
  color (not color alone as the only distinction);
- a current-area leaderboard; and
- equal displayed shares of shared area in individual scores.

It also shows group official-route completion beside individual completion,
monthly completion gain, monthly net capture-area change, and concise help for
the 50-metre connection rule, boundary-age priority, same-day sharing, and
pending recalculation. Empty, disconnected, synchronizing, stale, pending,
failed, and partially populated states are explicit and understandable.

## Calendar and chronology convention

All game calendar operations use the IANA timezone `Europe/Prague`:

- activity calendar dates shown to members;
- month buckets for newly completed distance and net capture-area change;
- effective claim dates and same-day ownership ties; and
- deterministic ordering wherever a calendar date, rather than a displayed
  exact timestamp, is part of the product rule.

The source timestamp needed for chronology is retained only within the private
processing boundary. Member-facing activity details remain date-only.

## Privacy and threat model

Location history, Strava identity, and competition membership are sensitive.
The following boundaries are product requirements, not optional UI behavior.

| Threat | Required boundary/control |
| --- | --- |
| Token theft or accidental disclosure | Encrypt/protect OAuth credentials; redact them from logs, telemetry, admin pages, API payloads, and frontend state. Handle refresh and revocation without leaking secrets. |
| Forged/replayed OAuth callback | Validate state/session binding, reject invalid or replayed callbacks, and do not create duplicate accounts. |
| Forged/replayed Strava webhook | Treat `POST` callbacks as untrusted hints; validate the setup-only `GET` challenge, callback schema and expected subscription/object/owner, rate-limit and deduplicate, re-fetch authoritative state where possible, reconcile periodically, and make processing idempotent. |
| Invite-code guessing or reuse after rotation | Treat codes as bearer secrets, validate only valid current codes, rotate atomically, invalidate old codes immediately, rate-limit attempts, and avoid enumeration/error leakage. |
| Cross-competition data leakage | Authorize every object through the current player’s membership; scope queries, caches, jobs, and derived results to the competition. Former members lose access immediately. |
| Public URL, search, or cache disclosure | Keep private selections out of publicly cacheable URLs; send private responses with no-store/private cache controls; exclude game routes and geometry from indexing and public caches. |
| Over-sharing Strava metadata | Expose only owner/color, geometry, and calendar date to members. Never return exact time, title, speed, or unrelated Strava fields. |
| Hidden-track reconstruction | Accept only Strava-returned privacy-filtered geometry; never infer or reconstruct hidden portions. |
| Webhook ordering and retry changing scores | Use idempotent event handling, versioned deterministic recomputation, and the chronology rules above. Delivery order must not affect final results. |
| Deletion leaving private data or scores | Stop synchronization on disconnect/deauthorization, revoke where possible, delete credentials and invalidate sessions promptly, start deletion immediately, complete it expeditiously and no later than 30 days (never using that limit as a retention window), purge transient caches within seven days, remove derived records, and recompute affected competitions. |
| Owner-admin overreach | Keep GitHub owner-admin login separate from Strava player identity and enforce the documented administration boundary. |

The public catalogue’s privacy behavior remains documented in the
[privacy notice](privacy.md). Adding `/game` must not make public catalogue
requests carry player identity or private geometry. The final operator,
lawful basis, retention wording, and third-party terms remain subject to the
existing legal launch gates; this specification does not provide legal advice
or silently grant rights to Strava, OpenStreetMap, Via Czechia, or any other
source.

## Authorization, storage, and cache boundaries

The game is a separate domain within the modular monolith. It may share
infrastructure with the public catalogue, but it must have explicit models,
queries, jobs, and API authorization boundaries. Public catalogue endpoints
must remain deployable without Strava credentials, game migrations being
enabled, or a game session.

Private responses are scoped to an authenticated member and the selected
competition, use bounded queries, and are not eligible for shared/public
caching. Cache keys must not allow one competition or member filter to return
another competition’s result. Background tasks carry an explicit competition
and calculation/version scope and must re-check lifecycle and membership state
before publishing results.

The imported activity record is owner-scoped and reusable across that owner’s
memberships. Competition-derived maps, completion evidence, capture faces,
leaderboards, and monthly aggregates are competition-scoped. Removing a
membership deletes or invalidates only the affected competition projection,
then recomputes it from the retained eligible activity set.

## Delivery sequence and dependency DAG

Each issue is implemented in its own issue-scoped branch and pull request from
the current `main`, with `main` remaining deployable. The game flag remains off
until its dependent behavior, privacy controls, and rollout evidence are ready.
The issue titles below are copied exactly so that this specification remains
cross-referenceable:

`S` below is a required positive Strava API agreement/display-policy gate, not a
feature issue. The #65 review records a rejection/blocker and therefore does
not satisfy this gate. A dedicated written Strava approval and an approved
revision to any conflicting issue scope are required before #47 or #49, or any
affected cross-member/derived issue, starts. Only a separately approved and
reviewed personal-only issue may proceed while this gate remains blocked.

| Order | Issue | Depends on |
| ---: | --- | --- |
| 1 | [#48 docs: specify the private Strava completion game](https://github.com/diamond447/BikeMapy/issues/48) | — |
| S | Strava API agreement/display-policy approval (dedicated written Strava approval and approved specification/issue-scope revision) | #48; current legal/provider review; #65 records a rejection/blocker and does not satisfy S |
| 2 | [#47 feature: add Strava sign-in and player accounts](https://github.com/diamond447/BikeMapy/issues/47) | #48, S-positive approval; authentication foundations in [#12 feature: secure owner administration and moderation](https://github.com/diamond447/BikeMapy/issues/12); otherwise blocked (a personal-only replacement requires separate approval) |
| 3 | [#46 feature: add invite-only game competitions](https://github.com/diamond447/BikeMapy/issues/46) | #47 |
| 4 | [#49 feature: synchronize eligible Strava cycling activities](https://github.com/diamond447/BikeMapy/issues/49) | #47, #46, S-positive approval and approved scope revision; otherwise blocked |
| 5 | [#51 docs: evaluate official route sources for game completion](https://github.com/diamond447/BikeMapy/issues/51) | #48; coordinate with [#17 docs: complete legal, attribution, privacy, and removal launch gates](https://github.com/diamond447/BikeMapy/issues/17) |
| 6 | [#53 feature: import and version official completion routes](https://github.com/diamond447/BikeMapy/issues/53) | #51 |
| 7a | [#50 feature: calculate individual and group route completion](https://github.com/diamond447/BikeMapy/issues/50) | #49, #53 |
| 7b | [#52 feature: display private competition activity maps](https://github.com/diamond447/BikeMapy/issues/52) | #46, #49 |
| 7c | [#55 test: validate capture topology, chronology, and performance](https://github.com/diamond447/BikeMapy/issues/55) | #48, #49 |
| 8 | [#57 feature: calculate chronological capture territory](https://github.com/diamond447/BikeMapy/issues/57) | #46, #55 |
| 9 | [#54 feature: add official route completion dashboards](https://github.com/diamond447/BikeMapy/issues/54) | #52, #50 |
| 10 | [#56 feature: add capture maps and competition leaderboards](https://github.com/diamond447/BikeMapy/issues/56) | #52, #57, #54 |

The partial order is:

```text
#48 ──> S ──> #47 ──> #46 ──> #49 ──> #50 ──> #54 ──┐
  │                 │       │       │                │
  │                 │       └──────>#52 ─────────────┤
  │                 │               │                v
  ├──────>#51 ──> #53 ──────────────┘              #56
  │                                                    ^
  └────────────────────────>#55 ──> #57 ─────────────┘
#49 ────────────────────────────────> #55
```

The diagram also includes the required `#49 ──> #55` dependency: topology and
chronology fixtures cannot be validated until the eligible activity pipeline
exists. The table is authoritative for all dependency edges.

The current S outcome is rejected/blocked, so completing #65 does not unblock
#47, #49, or the cross-member/derived issues #46, #50, #52, #54, #55, #56, or
#57. They remain blocked pending written Strava approval and approved
revisions to conflicting issue scope. This document does not edit those issue
descriptions or grant implementation permission. A separately approved and
reviewed personal-only issue is the only permitted path to proceed before
that approval. Every merged increment must keep the public catalogue
independently usable and the game safely disabled when its flag is off.

## Verification and rollout gates

Before enabling any game slice, tests must cover its focused behavior and the
public catalogue must still pass its documented quality checks. In particular,
the implementation sequence must verify:

- the positively approved Strava API agreement/display-policy gate (the #65
  review alone is a rejection/blocker), participant consent, required
  branding/attribution, retention/deletion terms, and the personal-only
  fallback when cross-member rights are unresolved;
- OAuth lifecycle, token redaction, callback replay, and revocation;
- competition membership, invite rotation, ownership transfer, colors, and
  deletion/recomputation;
- eligibility, privacy changes, idempotent backfill/untrusted webhook hints,
  authoritative re-fetch/reconciliation, quotas, and retention;
- approved-source provenance, rights/attribution, versioning, and invalid
  geometry isolation;
- deterministic completion evidence, 50-metre benchmark fixtures, and union
  semantics;
- capture fixtures, same-day sharing, “bitten apple” chronology, worldwide
  area correctness, incremental/full rebuild equivalence, and safe retry; and
- private API authorization, no-store caching, responsive/accessibility map
  behavior, and representative performance budgets.

Roll out progressively: owner-only validation, then a small invite-only tester
group, bounded historical backfill, webhook/quota/retention observation, and
only then an explicit production enablement decision. If the Strava agreement
gate, participant consent, or another legal/technical source blocker remains,
keep cross-member sharing and the game slice disabled. The personal-only
fallback may be enabled only as a separately reviewed mode; never assume
permission for cross-member traces or derived results.

## Related documentation

- [Public BikeMapy product specification](product-spec.md)
- [Privacy notice](privacy.md)
- [Legal and attribution review](legal-review.md)
- [Terms](terms.md)
- [Removal policy](removal-policy.md)
- [Administration guide](administration.md)
- [Issue #48](https://github.com/diamond447/BikeMapy/issues/48)
