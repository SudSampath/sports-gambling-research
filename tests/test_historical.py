from __future__ import annotations

import asyncio

import duckdb
import pytest

from sgr.connectors.espn import EspnConnector
from sgr.research.historical import (
    EXPECTED_REGULAR_SEASON_GAMES,
    EXPECTED_TEAM_COUNT,
    SeasonCoverageError,
    ingest_regular_season,
)
from sgr.research.storage import ResearchStore

from _season_fixtures import full_season_weeks, install_week_payloads

SEASON_YEAR = 2023


def _context(tmp_path, monkeypatch):
    return {
        "connector": EspnConnector(cache_dir=tmp_path / "espn-cache"),
        "monkeypatch": monkeypatch,
    }


def test_complete_season_is_ingested_and_persisted(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch)
    install_week_payloads(ctx, full_season_weeks(SEASON_YEAR))
    store = ResearchStore(root=tmp_path / "store")

    report = asyncio.run(
        ingest_regular_season(
            ctx["connector"], store, SEASON_YEAR, require_completed=True, refresh=True
        )
    )

    assert report.is_complete
    assert report.games_captured == EXPECTED_REGULAR_SEASON_GAMES
    assert report.teams_captured == EXPECTED_TEAM_COUNT

    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        game_count = connection.execute("SELECT COUNT(*) FROM games").fetchone()[0]
        team_count = connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    assert game_count == EXPECTED_REGULAR_SEASON_GAMES
    assert team_count == EXPECTED_TEAM_COUNT


def test_current_season_schedule_does_not_require_completion(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch)
    install_week_payloads(ctx, full_season_weeks(2026, completed=False))
    store = ResearchStore(root=tmp_path / "store")

    report = asyncio.run(
        ingest_regular_season(
            ctx["connector"], store, 2026, require_completed=False, refresh=True
        )
    )

    assert report.is_complete
    assert report.games_captured == EXPECTED_REGULAR_SEASON_GAMES
    assert not report.incomplete_event_ids


def test_missing_games_fail_the_coverage_gate(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch)
    payloads = full_season_weeks(SEASON_YEAR)
    payloads[10] = {"events": []}  # drop a whole week
    install_week_payloads(ctx, payloads)
    store = ResearchStore(root=tmp_path / "store")

    with pytest.raises(SeasonCoverageError) as excinfo:
        asyncio.run(
            ingest_regular_season(
                ctx["connector"], store, SEASON_YEAR, require_completed=True, refresh=True
            )
        )

    assert excinfo.value.report.games_captured < EXPECTED_REGULAR_SEASON_GAMES
    assert not excinfo.value.report.is_complete


def test_duplicate_event_ids_are_excluded_and_reported(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch)
    payloads = full_season_weeks(SEASON_YEAR)
    # Re-inject week 1's first event under week 2 with the same event ID.
    duplicate_event = payloads[1]["events"][0]
    payloads[2]["events"][0] = duplicate_event
    install_week_payloads(ctx, payloads)
    store = ResearchStore(root=tmp_path / "store")

    with pytest.raises(SeasonCoverageError) as excinfo:
        asyncio.run(
            ingest_regular_season(
                ctx["connector"], store, SEASON_YEAR, require_completed=True, refresh=True
            )
        )

    assert duplicate_event["id"] in excinfo.value.report.duplicate_event_ids


def test_inconsistent_season_year_is_excluded_and_reported(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch)
    payloads = full_season_weeks(SEASON_YEAR)
    payloads[1]["events"][0]["season"]["year"] = SEASON_YEAR - 1
    install_week_payloads(ctx, payloads)
    store = ResearchStore(root=tmp_path / "store")

    with pytest.raises(SeasonCoverageError) as excinfo:
        asyncio.run(
            ingest_regular_season(
                ctx["connector"], store, SEASON_YEAR, require_completed=True, refresh=True
            )
        )

    assert len(excinfo.value.report.inconsistent_event_ids) == 1


def test_incomplete_games_in_a_historical_season_fail_the_gate(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch)
    payloads = full_season_weeks(SEASON_YEAR)
    payloads[1]["events"][0]["competitions"][0]["status"]["type"] = {
        "name": "STATUS_SCHEDULED",
        "completed": False,
    }
    install_week_payloads(ctx, payloads)
    store = ResearchStore(root=tmp_path / "store")

    with pytest.raises(SeasonCoverageError) as excinfo:
        asyncio.run(
            ingest_regular_season(
                ctx["connector"], store, SEASON_YEAR, require_completed=True, refresh=True
            )
        )

    assert len(excinfo.value.report.incomplete_event_ids) == 1


def test_rerun_against_unchanged_source_is_deterministic_with_no_duplicates(tmp_path, monkeypatch):
    ctx = _context(tmp_path, monkeypatch)
    install_week_payloads(ctx, full_season_weeks(SEASON_YEAR))
    store = ResearchStore(root=tmp_path / "store")

    asyncio.run(
        ingest_regular_season(
            ctx["connector"], store, SEASON_YEAR, require_completed=True, refresh=True
        )
    )
    # Second pass reuses the immutable cache (refresh=False): no new ESPN requests.
    ctx["requests"] = []
    report_two = asyncio.run(
        ingest_regular_season(
            ctx["connector"], store, SEASON_YEAR, require_completed=True, refresh=False
        )
    )

    assert report_two.games_captured == EXPECTED_REGULAR_SEASON_GAMES
    with duckdb.connect(str(store.database_path), read_only=True) as connection:
        game_count = connection.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    assert game_count == EXPECTED_REGULAR_SEASON_GAMES
