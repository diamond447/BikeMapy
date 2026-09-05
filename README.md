# BikeMapy

BikeMapy is an early-stage portfolio project for discovering cycling routes
shared in BikeForum discussions. Years of useful route links are difficult to
find once they are buried in forum threads. BikeMapy is intended to turn that
archive into a searchable, map-first catalogue while preserving attribution
and source context.

The project is currently in specification and early development. Its primary
goal is to demonstrate careful product and engineering decisions, with a
secondary goal of being genuinely useful to cyclists. The initial success
hypothesis is 500 unique visitors during the first 30 days after launch.

## Planned MVP

The MVP will incrementally discover BikeForum posts, obtain linked GPX data
where permitted, validate and deduplicate routes, and publish technically
valid non-duplicate routes automatically. Visitors will be able to explore a
MapLibre map, search and filter routes, inspect their source and geometry, and
report problems. Administration, provenance, recoverable moderation, and a
public read-only API are part of the product design.

The application is planned as a portable Docker Compose deployment: a React
frontend on Cloudflare Pages and a Django modular monolith that can later move
to a homeserver. Public GPX downloads depend on a prior review of the relevant
terms and data rights.

See the [product specification](docs/product-spec.md) for the approved product
requirements, architecture direction, operational model, and launch gates.
Owner OAuth administration and moderation workflows are documented in
the [administration guide](docs/administration.md).

## Local development

The production-oriented application skeleton runs with Docker Compose. Follow
the [local development guide](docs/local-development.md) to start the Django,
React, Celery, PostGIS, and Redis services and run the quality suite.

## Project status

No production setup or public launch is being claimed yet. Implementation will
follow the documented decisions, benchmarks, legal review, and incremental
delivery through GitHub issues and pull requests.
