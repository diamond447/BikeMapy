"""Benchmark the private competition viewport query and browser map update.

Run this against a staging PostGIS database, for example::

    DJANGO_SETTINGS_MODULE=config.settings python scripts/benchmark_game_map.py \
      --fixture-size 15000 --browser-url http://127.0.0.1:4173/game \
      --output docs/game-map-benchmark-results.json

The command deliberately runs 30 warm requests and reports median/p95. It
fails when the API or browser budget is exceeded, making the result suitable
for CI or a release checklist.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path
from uuid import uuid4

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from apps.accounts.models import (  # noqa: E402
    Competition,
    CompetitionMembership,
    ImportedActivity,
    Player,
)

API_BUDGET_MS = 250.0
BROWSER_BUDGET_MS = 100.0
RUNS = 30


def percentile(values: list[float], fraction: float) -> float:
    values = sorted(values)
    index = min(len(values) - 1, round((len(values) - 1) * fraction))
    return values[index]


def fixture(size: int) -> tuple[Player, Competition]:
    user = get_user_model().objects.create_user(username=f"benchmark-{uuid4()}")
    player = Player.objects.create(
        user=user,
        strava_athlete_id=int(time.time() * 1000) % 2_000_000_000,
        strava_display_name="Benchmark rider",
    )
    competition = Competition.objects.create(
        owner=player,
        name="Private map benchmark",
        invite_code=f"benchmark-{uuid4().hex}",
    )
    CompetitionMembership.objects.create(competition=competition, player=player, color="#F4B942")
    from django.contrib.gis.geos import LineString

    rows = [
        ImportedActivity(
            player=player,
            provider_activity_id=f"benchmark-{index}",
            calendar_date="2026-09-21",
            geometry=LineString(
                (14.0 + (index % 100) / 1000, 49.0 + (index % 80) / 1000),
                (14.6 + (index % 100) / 1000, 49.6 + (index % 80) / 1000),
                srid=4326,
            ),
        )
        for index in range(size)
    ]
    ImportedActivity.objects.bulk_create(rows, batch_size=500)
    return player, competition


def api_runs(client: Client, competition: Competition) -> list[float]:
    url = reverse("game-competition-map", args=[competition.pk])
    params = {"west": "14", "south": "49", "east": "15", "north": "50", "zoom": "12"}
    samples = []
    for _ in range(RUNS):
        started = time.perf_counter()
        response = client.get(url, params)
        if response.status_code >= 400:
            raise RuntimeError(f"map request failed: {response.status_code}")
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def browser_runs(url: str) -> list[float]:
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    samples = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for _ in range(RUNS):
            page = browser.new_page()
            started = time.perf_counter()
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_selector('[data-map-source-loaded="true"]')
            samples.append((time.perf_counter() - started) * 1000)
            page.close()
        browser.close()
    return samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture-size", type=int, default=15_000)
    parser.add_argument("--browser-url")
    parser.add_argument("--output", type=Path, default=Path("docs/game-map-benchmark-results.json"))
    args = parser.parse_args()
    if connection.vendor != "postgresql":
        raise SystemExit("The dense benchmark requires a reachable PostGIS database.")
    with transaction.atomic():
        player, competition = fixture(args.fixture_size)
        client = Client()
        session = client.session
        session["player_id"] = player.pk
        session["player_session_epoch"] = player.session_epoch
        session.save()
        api_samples = api_runs(client, competition)
    result: dict[str, object] = {
        "environment": {"database": connection.vendor, "runs": RUNS},
        "fixture": {"activities": args.fixture_size, "members": 1},
        "api_ms": {"median": statistics.median(api_samples), "p95": percentile(api_samples, 0.95)},
    }
    if args.browser_url:
        browser_samples = browser_runs(args.browser_url)
        result["browser_ms"] = {
            "median": statistics.median(browser_samples),
            "p95": percentile(browser_samples, 0.95),
        }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    api_p95 = result["api_ms"]["p95"]  # type: ignore[index]
    if api_p95 > API_BUDGET_MS:
        raise SystemExit(f"API p95 budget exceeded: {api_p95:.1f} ms")
    if "browser_ms" in result:
        browser_p95 = result["browser_ms"]["p95"]  # type: ignore[index]
        if browser_p95 > BROWSER_BUDGET_MS:
            raise SystemExit(f"Browser p95 budget exceeded: {browser_p95:.1f} ms")


if __name__ == "__main__":
    main()
