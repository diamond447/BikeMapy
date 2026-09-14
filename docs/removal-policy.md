# Removal Policy

**Last reviewed:** 2026-09-14
**Status:** launch draft — private contact and operator identity are pending

BikeMapy aims to preserve useful provenance while responding promptly to
author, rights-holder, privacy, and source-provider concerns. A report is not
an automatic takedown: the owner reviews the evidence and may hide, correct,
quarantine, or remove a route. Source-unavailable status alone does not remove
a route.

## How to contact us

Use the [BikeMapy public issue tracker](https://github.com/diamond447/BikeMapy/issues)
to start a removal request and label it as a removal/legal concern. Do not
publish identity documents, private addresses, or other sensitive material in
the issue. Ask the maintainer for a private channel in the first message.
Publishing a verified private legal contact and response-time commitment is a
launch blocker recorded in [the legal review](legal-review.md). The proposed
address is `legal@bikemapy.cz`, but it is not verified or active.

You can also use the route's **Report a problem** form and choose **Author
removal request** or **Rights-holder request**. The report is queued for owner
review; it does not automatically change public route state.

## Information to include

To help us find and assess the material, provide:

- the BikeMapy route URL or route ID;
- the exact BikeForum thread/post URL and, where relevant, Mapy.com source URL;
- your name or organization and a reply address (optional in the public form,
  but needed for a private follow-up);
- whether you are the author, rights holder, authorized representative, or the
  person identified in the material;
- the specific material and requested action (remove attribution, correct it,
  remove the source/payload, or suppress the route); and
- a short explanation of the rights, privacy, or factual basis for the request.

Do not send passwords, identity documents, or unrelated personal data. If a
request concerns a legal claim, provide only the minimum evidence needed to
locate and assess it. The maintainer may ask for reasonable verification before
acting on a third-party request.

## Review and outcomes

The owner records a decision and reason. Possible outcomes include correction,
source or author attribution update, GPX payload removal, quarantine, soft
deletion, or no change when the request is unsupported. Historical moderation
and removal metadata may be retained to prevent accidental re-import and to
show why a decision was made; it does not preserve a removed GPX file.

Removal requests that identify a BikeForum page also cover any raw HTML held in
`CrawlResponseCache.body`. The live cache body becomes ineligible for replay
after 24 hours from acquisition. Physical clearing is attempted in the next
successful bounded hourly cleanup, but backlog or an outage can delay that
operation. Cache metadata may remain for
conditional requests and auditability. Database backups made before that cleanup can retain
the body until their normal expiry (up to 30 daily host snapshots and 90
encrypted laptop snapshots), unless the operator securely removes the affected
backup artifacts sooner and records that action.

We will restrict access to report details to the owner-admin. Optional contact
email is removed 90 days after a report closes. Closed-report details are
anonymized after 365 days under the configured retention task. Administrative
audit events are proposed to expire 24 months after creation unless a
documented legal hold applies; this expiry is not implemented yet. If a
request is urgent, say why in the initial contact and include a safe reply
path.
