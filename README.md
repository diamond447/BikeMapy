<div align="center">

# BikeMapy

**A map-first atlas of cycling routes shared on Czech cycling forums, each with its source.**

[**bikemapy.cz**](https://bikemapy.cz) · [Architecture](docs/architecture.md) · [Local development](docs/local-development.md) · [Deployment](docs/deployment.md)

[![Website](https://img.shields.io/website?url=https%3A%2F%2Fbikemapy.cz&label=bikemapy.cz&up_message=online&down_message=offline&style=flat-square)](https://bikemapy.cz)
[![Quality checks](https://img.shields.io/github/actions/workflow/status/diamond447/BikeMapy/ci.yml?branch=main&label=quality%20checks&style=flat-square&logo=githubactions&logoColor=white)](https://github.com/diamond447/BikeMapy/actions/workflows/ci.yml)
[![Security assurance](https://img.shields.io/github/actions/workflow/status/diamond447/BikeMapy/security.yml?branch=main&label=security&style=flat-square&logo=githubactions&logoColor=white)](https://github.com/diamond447/BikeMapy/actions/workflows/security.yml)
[![Backend image](https://img.shields.io/github/actions/workflow/status/diamond447/BikeMapy/backend-image.yml?branch=main&label=backend%20image&style=flat-square&logo=docker&logoColor=white)](https://github.com/diamond447/BikeMapy/actions/workflows/backend-image.yml)

[![Open pull requests](https://img.shields.io/github/issues-pr/diamond447/BikeMapy?label=open%20PRs&style=flat-square&logo=github&color=2f6f5e)](https://github.com/diamond447/BikeMapy/pulls)
[![Closed pull requests](https://img.shields.io/github/issues-pr-closed/diamond447/BikeMapy?label=closed%20PRs&style=flat-square&logo=github&color=6f42c1)](https://github.com/diamond447/BikeMapy/pulls?q=is%3Apr+is%3Aclosed)
[![Last commit](https://img.shields.io/github/last-commit/diamond447/BikeMapy?style=flat-square&color=e0703b)](https://github.com/diamond447/BikeMapy/commits/main)
[![License: MIT](https://img.shields.io/github/license/diamond447/BikeMapy?style=flat-square&color=3d7ea6)](LICENSE)

[![Python 3.13](https://img.shields.io/badge/Python-3.13-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Django 6.1](https://img.shields.io/badge/Django-6.1-092E20?style=flat-square&logo=django&logoColor=white)](https://www.djangoproject.com/)
[![PostGIS](https://img.shields.io/badge/PostGIS-PostgreSQL-336791?style=flat-square&logo=postgresql&logoColor=white)](https://postgis.net/)
[![React 19](https://img.shields.io/badge/React-19-149ECA?style=flat-square&logo=react&logoColor=white)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-strict-3178C6?style=flat-square&logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![MapLibre GL JS](https://img.shields.io/badge/MapLibre-GL%20JS-396CB2?style=flat-square&logo=maplibre&logoColor=white)](https://maplibre.org/)

</div>

---

Riders share their best routes as Mapy.com links buried in long forum threads.
BikeMapy finds those links, imports each route's GPX and puts every route on one
map. Every route keeps its geometry, its source post and author, and a link back
to the original discussion.

<p align="center">
  <a href="https://bikemapy.cz/?lat=49.19&lng=16.62&z=9.6">
    <img src="docs/screenshots/map-desktop.jpg" alt="BikeMapy on desktop: 340 routes around Brno over the OpenStreetMap map, with the route list in the sidebar" width="100%">
  </a>
</p>

<table>
  <tr>
    <td width="68%" valign="top">
      <img src="docs/screenshots/route-detail-desktop.jpg" alt="Route detail: a 95 km loop in Česká Kanada with distance, climb, elevation profile, its BikeForum source and a GPX download">
    </td>
    <td width="32%" valign="top">
      <img src="docs/screenshots/map-mobile.jpg" alt="BikeMapy on a phone: routes around Brno with the route list collapsed">
    </td>
  </tr>
  <tr>
    <td align="center"><sub>Route detail with the elevation profile, the source post and a GPX download</sub></td>
    <td align="center"><sub>The same map on a phone</sub></td>
  </tr>
</table>

## ✨ Highlights

| | |
|---|---|
| 🗺️ **Map first** | MapLibre map over the OpenStreetMap standard map, with signed cycle routes from Waymarked Trails. Every route as a thin line, darker where many overlap; a heatmap only when zoomed far out. |
| 🔎 **Search and filter** | Search by place, author or route, filter by distance and climb, and share any view: the state lives in the URL. |
| 🧭 **Route detail** | Distance, ascent, elevation profile, loop or point-to-point, GPX download and the forum posts the route came from. |
| 🕸️ **Forum crawler** | A resumable, rate-limited crawler for Bike-Forum.cz that follows threads moved to MTBIKER and reads every Mapy link. It respects robots.txt. |
| 📥 **GPX import** | Short links are resolved and exported to GPX through a guarded client: HTTPS Mapy hosts and public addresses only. |
| 🧬 **Catalogue** | Immutable route versions, source provenance, duplicate and variant detection, and an audited moderation history. |
| 🔐 **Owner-only admin** | Django admin behind GitHub OAuth for one numeric account ID. Anonymous reports are protected by Turnstile and rate limits. |
| 🪶 **Lean mode** | One small host runs only PostgreSQL, the API and Nginx; scheduled work runs from systemd timers instead of Celery. |
| 🌍 **Czech and English** | The whole interface in both languages. |

## 🧩 How it works

```mermaid
flowchart LR
    forum[(Bike-Forum.cz<br/>MTBIKER)] -->|crawl threads| crawler[Crawler]
    crawler -->|Mapy links| sources[Route sources]
    sources -->|resolve and export| gpx[GPX import]
    gpx --> db[(PostgreSQL<br/>PostGIS)]
    db --> api[Django REST API]
    api -->|OpenAPI client| web[React + MapLibre<br/>bikemapy.cz]
    tiles[OSM and Waymarked<br/>Trails tiles] --> web
```

- **Backend**: Django 6.1, Django REST Framework and GeoDjango on PostgreSQL with
  PostGIS. Celery and Redis run the crawler and imports; in lean mode the tasks
  run inside requests and from timers.
- **API**: versioned, with paginated search, bounded viewport queries, route
  geometry, an OpenAPI schema and a generated TypeScript client.
- **Frontend**: React 19, TypeScript, Vite and MapLibre GL JS on Cloudflare Pages.
- **Delivery**: Docker Compose, Nginx and Cloudflare Tunnel. Commit-tagged images
  in GitHub Container Registry, published only after CI passes on `main`.

## 🚀 Quick start

Requirements: Docker with Compose v2, Python 3.13 with
[uv](https://docs.astral.sh/uv/), and Node.js 22 with Corepack.

```sh
cp .env.example .env
docker compose up --build
docker compose exec backend python backend/manage.py migrate
```

| Service | URL |
|---|---|
| Frontend | <http://localhost:5173> |
| API | <http://localhost:8000> |
| API docs | <http://localhost:8000/api/docs/> |

```sh
make install    # locked backend and frontend dependencies
make check      # lint, type checks, tests and frontend build
```

## 🧪 Quality

CI runs, for the parts of the code a change touches:
- lint, type checks and tests for the backend and frontend;
- the PostGIS and Celery suite;
- Playwright browser tests with axe accessibility checks;
- Compose smoke tests, a rollback rehearsal and a restore drill;
- CodeQL, Gitleaks and dependency audits.

## 📚 Documentation

- [Local development](docs/local-development.md)
- [Architecture](docs/architecture.md)
- [Deployment and operations](docs/deployment.md)
- [Security policy](SECURITY.md) and [contributing](CONTRIBUTING.md)
- Public notices: [privacy](docs/privacy.md), [terms](docs/terms.md),
  [removal policy](docs/removal-policy.md)

## 🕰️ Project history

Until October 2026 this repository also contained an experimental private game
built on Strava activities. It was removed to keep BikeMapy focused on the route
catalogue. The game continues, together with a bike-fitting prototype, in
separate private repositories.

## 📄 License

The source code is MIT licensed (see [LICENSE](LICENSE)). The licence does not
cover third-party maps, routes, GPX files or forum content. Map data
© [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), cycle
routes by [Waymarked Trails](https://cycling.waymarkedtrails.org/). BikeMapy is an
independent project with no affiliation with Mapy.com, Seznam.cz, Bike-forum.cz or MTBIKER.
