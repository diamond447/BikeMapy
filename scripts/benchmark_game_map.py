"""Benchmark the private competition viewport query and browser map update.

The default fixture has 80 members, 20 activities/member, and 250 points per
activity. Fifteen activities per member intersect the viewport (1,200 total);
the remaining activities are deliberately outside it. Run against staging:

    python scripts/benchmark_game_map.py --browser-url https://staging.example/game

Thirty warm samples are recorded as median/p95 and the command exits non-zero
when either release budget is exceeded.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.contrib.gis.geos import LineString  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from apps.accounts.models import (  # noqa: E402
    Competition,
    CompetitionMembership,
    ImportedActivity,
    Player,
)

API_BUDGET_MS = 1_500.0
BROWSER_BUDGET_MS = 100.0
RUNS = 30
BENCHMARK_ENVIRONMENT = (
    "DJANGO_SETTINGS_MODULE=config.settings",
    "POSTGRES_HOST=127.0.0.1",
    "POSTGRES_PORT=5432",
    "POSTGRES_DB=bikemapy",
    "POSTGRES_USER=bikemapy",
    "POSTGRES_PASSWORD=bikemapy-local-only",
    "DJANGO_CACHE_URL=redis://127.0.0.1:6379/1",
    "GAME_ENABLED=true",
    "STRAVA_OAUTH_CLIENT_ID=benchmark-client",
    "STRAVA_OAUTH_CLIENT_SECRET=benchmark-secret",
    "STRAVA_TOKEN_ENCRYPTION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "STRAVA_IDENTITY_GUARD_KEY=benchmark-identity-guard-key-123456789",
    "CORS_ALLOWED_ORIGINS=http://127.0.0.1:4173",
)
DEFAULT_MEMBERS = 80
DEFAULT_ACTIVITIES_PER_MEMBER = 20
DEFAULT_POINTS = 250
DEFAULT_IN_VIEWPORT = 15


def percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    index = min(len(values) - 1, round((len(values) - 1) * fraction))
    return values[index]


def track_points(
    index: int, member_index: int, in_viewport: bool, points: int
) -> list[tuple[float, float]]:
    if in_viewport:
        base_x = 14.05 + (member_index % 20) * 0.01
        base_y = 49.05 + (member_index // 20) * 0.03
    else:
        base_x = 30.0 + (member_index % 20) * 0.1
        base_y = 20.0 + (member_index // 20) * 0.1
    return [
        (base_x + (index % 10) * 0.001 + point * 0.0001, base_y + point * 0.0001)
        for point in range(points)
    ]


def fixture(
    members_count: int,
    activities_per_member: int,
    points: int,
    in_viewport: int,
) -> tuple[Player, Competition, int]:
    all_players: list[Player] = []
    for member_index in range(members_count):
        user = get_user_model().objects.create_user(username=f"benchmark-{uuid4()}")
        all_players.append(
            Player.objects.create(
                user=user,
                strava_athlete_id=int(uuid4().int % 2_000_000_000),
                strava_display_name=f"Benchmark rider {member_index}",
            )
        )
    competition = Competition.objects.create(
        owner=all_players[0],
        name="Private map benchmark",
        invite_code=f"bm-{uuid4().hex[:28]}",
    )
    CompetitionMembership.objects.bulk_create(
        [
            CompetitionMembership(
                competition=competition,
                player=player,
                color=f"#{(0x24 + member_index * 97) % 0xFFFFFF:06X}",
            )
            for member_index, player in enumerate(all_players)
        ]
    )
    rows: list[ImportedActivity] = []
    for member_index, player in enumerate(all_players):
        in_geometry = LineString(*track_points(0, member_index, True, points), srid=4326)
        out_geometry = LineString(*track_points(0, member_index, False, points), srid=4326)
        for activity_index in range(activities_per_member):
            rows.append(
                ImportedActivity(
                    player=player,
                    provider_activity_id=f"benchmark-{member_index}-{activity_index}",
                    calendar_date="2026-09-21",
                    geometry=in_geometry if activity_index < in_viewport else out_geometry,
                )
            )
    ImportedActivity.objects.bulk_create(rows, batch_size=500)
    return all_players[0], competition, len(rows)


def api_runs(client: Client, competition: Competition) -> list[float]:
    url = reverse("game-competition-map", args=[competition.pk])
    params = {"west": "14", "south": "49", "east": "15", "north": "50", "zoom": "12"}
    warmup = client.get(url, params, HTTP_HOST="localhost")
    if warmup.status_code >= 400:
        raise RuntimeError(f"map warm-up failed: {warmup.status_code}")
    samples = []
    for _ in range(RUNS):
        started = time.perf_counter()
        response = client.get(url, params, HTTP_HOST="localhost")
        if response.status_code >= 400:
            raise RuntimeError(f"map request failed: {response.status_code}")
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def browser_runs(url: str, api_url: str, session_key: str) -> list[float]:
    output = subprocess.check_output(
        [
            "corepack",
            "pnpm",
            "--dir",
            "frontend",
            "exec",
            "node",
            "benchmark/game-map-browser.mjs",
            url,
            api_url,
            session_key,
        ],
        text=True,
    )
    samples = json.loads(output)
    if not isinstance(samples, list) or len(samples) != RUNS:
        raise RuntimeError("browser benchmark did not return 30 samples")
    return [float(sample) for sample in samples]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--members", type=int, default=DEFAULT_MEMBERS)
    parser.add_argument("--activities-per-member", type=int, default=DEFAULT_ACTIVITIES_PER_MEMBER)
    parser.add_argument("--points-per-activity", type=int, default=DEFAULT_POINTS)
    parser.add_argument("--in-viewport-per-member", type=int, default=DEFAULT_IN_VIEWPORT)
    parser.add_argument("--browser-url")
    parser.add_argument("--browser-api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("docs/game-map-benchmark-results.json"))
    args = parser.parse_args()
    if connection.vendor != "postgresql":
        raise SystemExit("The dense benchmark requires a reachable PostGIS database.")
    player, competition, activity_count = fixture(
        args.members,
        args.activities_per_member,
        args.points_per_activity,
        args.in_viewport_per_member,
    )
    client = Client()
    session = client.session
    session["player_id"] = player.pk
    session["player_session_epoch"] = player.session_epoch
    session.save()
    api_samples = api_runs(client, competition)
    result: dict[str, object] = {
        "environment": {
            "database": connection.vendor,
            "database_name": connection.settings_dict.get("NAME"),
            "runs": RUNS,
        },
        "command": shlex.join(
            [
                "env",
                *BENCHMARK_ENVIRONMENT,
                "uv",
                "run",
                "--locked",
                "--extra",
                "dev",
                "python",
                *sys.argv,
            ]
        ),
        "fixture": {
            "members": args.members,
            "activities": activity_count,
            "points_per_activity": args.points_per_activity,
            "in_viewport_activities": args.members * args.in_viewport_per_member,
        },
        "api_ms": {"median": statistics.median(api_samples), "p95": percentile(api_samples, 0.95)},
    }
    if args.browser_url:
        browser_samples = browser_runs(
            args.browser_url, args.browser_api_url, session.session_key or ""
        )
        result["browser_response_to_render_ms"] = {
            "median": statistics.median(browser_samples),
            "p95": percentile(browser_samples, 0.95),
        }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    api_p95 = result["api_ms"]["p95"]  # type: ignore[index]
    if api_p95 > API_BUDGET_MS:
        raise SystemExit(f"API p95 budget exceeded: {api_p95:.1f} ms")
    if "browser_response_to_render_ms" in result:
        browser_p95 = result["browser_response_to_render_ms"]["p95"]  # type: ignore[index]
        if browser_p95 > BROWSER_BUDGET_MS:
            raise SystemExit(f"Browser p95 budget exceeded: {browser_p95:.1f} ms")


if __name__ == "__main__":
    main()
