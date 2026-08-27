from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from pytest_bdd import given, scenarios, then, when

from sgr.connectors.theoddsapi import HistoricalOddsSnapshot
from sgr.research.timestamped_odds import normalize_historical_odds, select_odds_at_or_before

scenarios("../features/timestamped_odds.feature")

REQUESTED_AT = datetime(2024, 9, 8, 18, 0, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 8, 27, tzinfo=timezone.utc)

FULL_PAYLOAD = {
    "timestamp": "2024-09-08T17:55:00Z",
    "previous_timestamp": "2024-09-08T17:50:00Z",
    "next_timestamp": "2024-09-08T18:00:00Z",
    "data": [
        {
            "id": "abc123",
            "sport_key": "americanfootball_nfl",
            "commence_time": "2024-09-09T00:20:00Z",
            "home_team": "Buffalo Bills",
            "away_team": "Arizona Cardinals",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "title": "DraftKings",
                    "last_update": "2024-09-08T17:54:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Buffalo Bills", "price": 1.45},
                                {"name": "Arizona Cardinals", "price": 2.85},
                            ],
                        },
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Buffalo Bills", "price": 1.91, "point": -3.5},
                                {"name": "Arizona Cardinals", "price": 1.91, "point": 3.5},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": 1.91, "point": 46.5},
                                {"name": "Under", "price": 1.91, "point": 46.5},
                            ],
                        },
                    ],
                },
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "last_update": "2024-09-08T17:53:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Buffalo Bills", "price": 1.47},
                                {"name": "Arizona Cardinals", "price": 2.80},
                            ],
                        },
                    ],
                },
            ],
        }
    ],
}

EMPTY_PAYLOAD = {
    "timestamp": "2024-09-08T17:55:00Z",
    "previous_timestamp": "2024-09-08T17:50:00Z",
    "next_timestamp": "2024-09-08T18:00:00Z",
    "data": [],
}


def _snapshot(payload: dict) -> HistoricalOddsSnapshot:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return HistoricalOddsSnapshot(
        payload=payload,
        requested_at=REQUESTED_AT,
        retrieved_at=RETRIEVED_AT,
        sha256=hashlib.sha256(canonical).hexdigest(),
        source_url="https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl/odds",
    )


@pytest.fixture
def odds_context():
    return {}


@given("a historical odds snapshot with two bookmakers quoting h2h, spreads, and totals")
def full_snapshot(odds_context):
    odds_context["snapshot"] = _snapshot(FULL_PAYLOAD)


@when("the snapshot is normalized")
def normalize(odds_context):
    records, coverage = normalize_historical_odds(odds_context["snapshot"])
    odds_context["records"] = records
    odds_context["coverage"] = coverage


@then("each bookmaker's outcomes remain separately identified")
def bookmakers_distinct(odds_context):
    bookmaker_keys = {record.bookmaker_key for record in odds_context["records"]}
    assert bookmaker_keys == {"draftkings", "fanduel"}
    draftkings_h2h = [
        r for r in odds_context["records"] if r.bookmaker_key == "draftkings" and r.market_key == "h2h"
    ]
    fanduel_h2h = [r for r in odds_context["records"] if r.bookmaker_key == "fanduel" and r.market_key == "h2h"]
    assert len(draftkings_h2h) == 2
    assert len(fanduel_h2h) == 2
    assert draftkings_h2h[0].provider_timestamp != fanduel_h2h[0].provider_timestamp


@then("spread and total outcomes carry a point value and h2h outcomes do not")
def points_present_where_expected(odds_context):
    for record in odds_context["records"]:
        if record.market_key == "h2h":
            assert record.point is None
        else:
            assert record.point is not None


@given("a historical odds snapshot with no events")
def empty_snapshot(odds_context):
    odds_context["snapshot"] = _snapshot(EMPTY_PAYLOAD)


@then("no observations are written")
def no_observations(odds_context):
    assert odds_context["records"] == ()


@then("the coverage report still records the snapshot timestamp")
def coverage_has_timestamp(odds_context):
    assert odds_context["coverage"].snapshot_timestamp == datetime(2024, 9, 8, 17, 55, tzinfo=timezone.utc)


@when("the snapshot is normalized twice")
def normalize_twice(odds_context):
    first_records, _ = normalize_historical_odds(odds_context["snapshot"])
    second_records, _ = normalize_historical_odds(odds_context["snapshot"])
    odds_context["first_ids"] = {r.id for r in first_records}
    odds_context["second_ids"] = {r.id for r in second_records}


@then("both runs produce the exact same set of record IDs")
def same_ids(odds_context):
    assert odds_context["first_ids"] == odds_context["second_ids"]
    assert len(odds_context["first_ids"]) > 0


@given("timestamped odds observations before and after a prediction cutoff")
def observations_around_cutoff(odds_context):
    earlier_payload = json.loads(json.dumps(FULL_PAYLOAD))
    earlier_payload["timestamp"] = "2024-09-08T17:45:00Z"
    earlier_payload["data"][0]["bookmakers"][0]["last_update"] = "2024-09-08T17:44:00Z"

    later_payload = json.loads(json.dumps(FULL_PAYLOAD))
    later_payload["timestamp"] = "2024-09-08T17:55:00Z"
    later_payload["data"][0]["bookmakers"][0]["last_update"] = "2024-09-08T17:54:00Z"

    earlier_records, _ = normalize_historical_odds(_snapshot(earlier_payload))
    later_records, _ = normalize_historical_odds(_snapshot(later_payload))
    odds_context["records"] = (*earlier_records, *later_records)
    odds_context["cutoff"] = datetime(2024, 9, 8, 17, 50, 0, tzinfo=timezone.utc)


@when("odds are selected for that cutoff")
def select_for_cutoff(odds_context):
    odds_context["selected"] = select_odds_at_or_before(
        odds_context["records"],
        provider_event_id="abc123",
        bookmaker_key="draftkings",
        market_key="h2h",
        outcome_name="Buffalo Bills",
        prediction_cutoff=odds_context["cutoff"],
    )


@then("the selected observation is the latest one at or before the cutoff")
def selected_is_latest_eligible(odds_context):
    selected = odds_context["selected"]
    assert selected is not None
    assert selected.snapshot_timestamp <= odds_context["cutoff"]
    assert selected.snapshot_timestamp == datetime(2024, 9, 8, 17, 45, tzinfo=timezone.utc)


@given("timestamped odds observations that all come after a prediction cutoff")
def observations_all_after_cutoff(odds_context):
    records, _ = normalize_historical_odds(_snapshot(FULL_PAYLOAD))
    odds_context["records"] = records
    odds_context["cutoff"] = datetime(2024, 9, 8, 17, 0, tzinfo=timezone.utc)


@then("no observation is selected")
def nothing_selected(odds_context):
    assert odds_context["selected"] is None
