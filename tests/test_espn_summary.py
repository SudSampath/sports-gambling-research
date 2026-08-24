from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from sgr.connectors.espn import EspnConnector, EspnRequestError, EspnSchemaError

COMPLETED_FIXTURE = Path(__file__).parent / "fixtures" / "espn_summary_completed.json"
UPCOMING_FIXTURE = Path(__file__).parent / "fixtures" / "espn_summary_upcoming.json"
EVENT_ID = "401772918"


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _install_payload(connector: EspnConnector, monkeypatch, payload: dict) -> list[dict]:
    requests = []

    async def fake_get_json(path: str, params: dict | None = None):
        requests.append({"path": path, "params": params})
        return payload

    monkeypatch.setattr(connector, "get_json", fake_get_json)
    return requests


def test_completed_game_normalizes_boxscore_and_injuries(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)
    _install_payload(connector, monkeypatch, _payload(COMPLETED_FIXTURE))

    statlines, injuries = asyncio.run(connector.game_summary(EVENT_ID))

    assert len(statlines) == 2
    lamar = next(s for s in statlines if s.athlete.display_name == "Lamar Jackson")
    assert lamar.team.abbreviation == "BAL"
    assert lamar.stat_category == "passing"
    assert lamar.stat_labels == ("C/ATT", "YDS", "AVG", "TD", "INT", "SACKS", "QBR", "RTG")
    assert lamar.stat_values == ("14/19", "209", "11.0", "2", "0", "2-15", "94.9", "144.4")
    assert lamar.raw_snapshot_sha256
    assert lamar.normalization_version == EspnConnector.NORMALIZATION_VERSION_SUMMARY

    assert len(injuries) == 2
    ty = next(i for i in injuries if i.athlete.display_name == "Ty Johnson")
    assert ty.status_text == "Questionable"
    assert ty.athlete.position == "RB"
    assert ty.reported_at == datetime(2026, 8, 24, 19, 32, tzinfo=timezone.utc)


def test_upcoming_game_has_no_boxscore_but_has_injuries(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)
    _install_payload(connector, monkeypatch, _payload(UPCOMING_FIXTURE))

    statlines, injuries = asyncio.run(connector.game_summary(EVENT_ID))

    assert statlines == []
    assert len(injuries) == 1
    assert injuries[0].athlete.display_name == "Rodney Thomas II"
    assert injuries[0].team.abbreviation == "SEA"


def test_excluded_fields_are_never_read_into_normalized_output(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)
    payload = _payload(COMPLETED_FIXTURE)
    assert "predictor" in payload and "odds" in payload  # sanity: fixture actually has them
    statlines, injuries = asyncio.run(connector.game_summary(EVENT_ID))

    for item in (*statlines, *injuries):
        dumped = item.model_dump()
        assert set(dumped).isdisjoint({"predictor", "odds", "pickcenter", "winprobability"})


def test_cached_summary_is_reused_without_a_live_request(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)
    requests = _install_payload(connector, monkeypatch, _payload(COMPLETED_FIXTURE))
    asyncio.run(connector.game_summary(EVENT_ID))
    assert len(requests) == 1

    asyncio.run(connector.game_summary(EVENT_ID))
    assert len(requests) == 1  # second call reused the cache, no new request


def test_refresh_appends_a_new_immutable_snapshot(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)
    requests = _install_payload(connector, monkeypatch, _payload(COMPLETED_FIXTURE))
    asyncio.run(connector.game_summary(EVENT_ID))
    asyncio.run(connector.game_summary(EVENT_ID, refresh=True))
    assert len(requests) == 2

    snapshots = list((tmp_path / "nfl-summary" / f"event-{EVENT_ID}").glob("*.json"))
    assert len(snapshots) == 2


def test_schema_drift_fails_with_typed_error(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)
    _install_payload(connector, monkeypatch, {"boxscore": "not-an-object", "injuries": None})
    with pytest.raises(EspnSchemaError):
        asyncio.run(connector.game_summary(EVENT_ID))


def test_missing_event_id_is_rejected():
    connector = EspnConnector()
    with pytest.raises(ValueError):
        asyncio.run(connector.game_summary(""))


def test_provider_http_failure_is_typed(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)

    async def failing_get_json(path, params=None):
        request = httpx.Request("GET", "https://example.test")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("unavailable", request=request, response=response)

    monkeypatch.setattr(connector, "get_json", failing_get_json)
    with pytest.raises(EspnRequestError):
        asyncio.run(connector.game_summary(EVENT_ID))


def test_summary_and_scoreboard_caches_do_not_collide(tmp_path, monkeypatch):
    connector = EspnConnector(cache_dir=tmp_path)
    _install_payload(connector, monkeypatch, _payload(COMPLETED_FIXTURE))
    asyncio.run(connector.game_summary(EVENT_ID))
    assert (tmp_path / "nfl-summary").exists()
    assert not (tmp_path / "nfl-scoreboard" / f"event-{EVENT_ID}").exists()
