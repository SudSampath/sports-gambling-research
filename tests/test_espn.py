from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from sgr.connectors.espn import (
    EspnConnector,
    EspnRequestError,
    EspnSchemaError,
    PointInTimeDataUnavailableError,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "espn_scoreboard_2025-09-07.json"
GAME_DATE = date(2025, 9, 7)
SOURCE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=20250907"


def _fixture_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_normalizes_completed_game_and_retains_snapshot_provenance(tmp_path):
    connector = EspnConnector(cache_dir=tmp_path)
    retrieved_at = datetime(2025, 9, 7, 18, tzinfo=timezone.utc)
    snapshot = connector._write_snapshot(GAME_DATE, _fixture_payload(), SOURCE_URL, retrieved_at)

    games = asyncio.run(connector.games_for_date(GAME_DATE))

    assert len(games) == 1
    game = games[0]
    assert game.event_id == "401772918"
    assert game.season_year == 2025
    assert game.season_type == "regular"
    assert game.week == 1
    assert game.neutral_site is False
    assert game.kickoff == datetime(2025, 9, 8, 0, 20, tzinfo=timezone.utc)
    assert game.status == "STATUS_FINAL"
    assert game.completed is True
    assert game.home_team.abbreviation == "BUF"
    assert game.away_team.abbreviation == "BAL"
    assert (game.home_score, game.away_score) == (41, 40)
    assert game.retrieved_at == retrieved_at
    assert game.source_url == SOURCE_URL
    assert game.raw_snapshot_path == snapshot["snapshot_path"]
    assert len(game.raw_snapshot_sha256) == 64
    assert game.normalization_version == "espn-scoreboard-v1"


def test_reuses_cached_snapshot_without_a_live_request(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)
    connector._write_snapshot(
        GAME_DATE, _fixture_payload(), SOURCE_URL, datetime(2025, 9, 7, 18, tzinfo=timezone.utc)
    )

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("cached read unexpectedly requested ESPN")

    monkeypatch.setattr(connector, "_fetch_and_store", unexpected_fetch)

    games = asyncio.run(connector.games_for_date(GAME_DATE))

    assert [game.event_id for game in games] == ["401772918"]


def test_point_in_time_reads_latest_snapshot_available_at_prediction_time(tmp_path):
    connector = EspnConnector(cache_dir=tmp_path)
    first_retrieval = datetime(2025, 9, 7, 12, tzinfo=timezone.utc)
    second_retrieval = first_retrieval + timedelta(hours=2)
    first = _fixture_payload()
    second = _fixture_payload()
    second["events"][0]["competitions"][0]["competitors"][0]["score"] = "41"
    second["events"][0]["competitions"][0]["competitors"][1]["score"] = "42"
    connector._write_snapshot(GAME_DATE, first, SOURCE_URL, first_retrieval)
    connector._write_snapshot(GAME_DATE, second, SOURCE_URL, second_retrieval)

    games = asyncio.run(
        connector.games_for_date(GAME_DATE, prediction_at=first_retrieval + timedelta(minutes=30))
    )

    assert games[0].retrieved_at == first_retrieval
    assert (games[0].away_score, games[0].home_score) == (40, 41)


def test_point_in_time_read_rejects_missing_snapshot(tmp_path):
    connector = EspnConnector(cache_dir=tmp_path)

    with pytest.raises(PointInTimeDataUnavailableError, match="No cached ESPN scoreboard snapshot"):
        asyncio.run(
            connector.games_for_date(
                GAME_DATE, prediction_at=datetime(2025, 9, 7, 12, tzinfo=timezone.utc)
            )
        )


def test_schema_drift_fails_with_typed_error(tmp_path):
    connector = EspnConnector(cache_dir=tmp_path)
    connector._write_snapshot(
        GAME_DATE, {"unexpected": []}, SOURCE_URL, datetime(2025, 9, 7, 18, tzinfo=timezone.utc)
    )

    with pytest.raises(EspnSchemaError, match="missing its events list"):
        asyncio.run(connector.games_for_date(GAME_DATE))


def test_corrupt_cached_snapshot_fails_with_typed_error(tmp_path):
    connector = EspnConnector(cache_dir=tmp_path)
    snapshot = connector._write_snapshot(
        GAME_DATE, _fixture_payload(), SOURCE_URL, datetime(2025, 9, 7, 18, tzinfo=timezone.utc)
    )
    path = Path(snapshot["snapshot_path"])
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["payload"]["events"][0]["id"] = "modified-after-capture"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(EspnSchemaError, match="Cached ESPN snapshot is invalid"):
        asyncio.run(connector.games_for_date(GAME_DATE))


def test_network_failure_is_typed_and_never_fabricates_data(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)

    async def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(connector, "get_json", unavailable)

    with pytest.raises(EspnRequestError, match="checking connectivity"):
        asyncio.run(connector.games_for_date(GAME_DATE, refresh=True))
