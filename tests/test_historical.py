from __future__ import annotations

import asyncio

import duckdb
import pytest

from sgr.connectors.espn import EspnConnector
from sgr.research.historical import (
    EXPECTED_REGULAR_SEASON_GAMES,
    EXPECTED_TEAM_COUNT,
    SeasonCoverageError,
    expected_regular_season_games,
    expected_regular_season_weeks,
    expected_team_count,
    ingest_regular_season,
)
from sgr.research.storage import ResearchStore

from _season_fixtures import _postponed_event, full_season_weeks, install_week_payloads

SEASON_YEAR = 2023


def _context(tmp_path, monkeypatch):
    return {
        "connector": EspnConnector(cache_dir=tmp_path / "espn-cache"),
        "monkeypatch": monkeypatch,
    }


def test_pre_2021_seasons_expect_a_16_game_17_week_schedule():
    assert expected_regular_season_games(2020) == 256
    assert expected_regular_season_games(2010) == 256
    assert list(expected_regular_season_weeks(2020)) == list(range(1, 18))


def test_31_team_era_expects_248_games():
    """2000: the Browns' 1999 re-establishment made it 31 teams, before the
    Houston Texans joined as the 32nd franchise in 2002. (2001 also ran a
    31-team schedule but has its own documented 247-game exception above,
    for ESPN's missing DAL-SEA archive entry.)"""
    assert expected_regular_season_games(2000) == 248
    assert expected_team_count(2000) == 31
    assert expected_team_count(2002) == 32


def test_2021_and_later_seasons_expect_a_17_game_18_week_schedule():
    assert expected_regular_season_games(2021) == 272
    assert expected_regular_season_games(2025) == 272
    assert list(expected_regular_season_weeks(2021)) == list(range(1, 19))


def test_2022_expects_271_games_for_the_suspended_bills_bengals_game():
    assert expected_regular_season_games(2022) == 271


def test_2001_expects_247_games_for_espns_missing_dal_sea_archive_gap():
    assert expected_regular_season_games(2001) == 247


def test_postponed_game_placeholder_is_excluded_not_blocking(tmp_path, monkeypatch):
    """A rescheduled/cancelled game's permanent placeholder (found live: 2014
    week 12 BUF@NYJ, 2017 week 1 MIA@TB, 2022 week 17 CIN@BUF) must not
    block an otherwise-complete season's require_completed ingest."""
    ctx = _context(tmp_path, monkeypatch)
    payloads = full_season_weeks(SEASON_YEAR)
    payloads[1]["events"].append(_postponed_event(f"{SEASON_YEAR}-postponed", SEASON_YEAR, 1, 0, 1))
    install_week_payloads(ctx, payloads)
    store = ResearchStore(root=tmp_path / "store")

    report = asyncio.run(
        ingest_regular_season(
            ctx["connector"], store, SEASON_YEAR, require_completed=True, refresh=True
        )
    )

    assert report.is_complete
    assert report.games_captured == EXPECTED_REGULAR_SEASON_GAMES
    assert report.rescheduled_or_canceled_event_ids == (f"{SEASON_YEAR}-postponed",)


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
