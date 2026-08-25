from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pytest_bdd import given, scenarios, then, when

from _game_factory import make_game, team_id
from sgr.connectors.espn import EspnConnector
from sgr.models import NFLAthlete, NFLInjuryReport, NFLTeam
from sgr.research.injury_ingest import ingest_current_injuries
from sgr.research.pythagorean import DEFAULT_EXPONENT, generate_forecast
from sgr.research.schemas import (
    AvailabilityReport,
    AvailabilityReportClass,
    Game,
    PlayerGameStatline,
    RawSnapshotRef,
    stable_record_id,
)
from sgr.research.storage import ResearchStore

scenarios("../features/injury_aware_forecasts.feature")

SEASON_START = datetime(2025, 9, 8, tzinfo=timezone.utc)
_SOURCE = RawSnapshotRef(
    provider="espn", path="raw/x.json", source_url="https://example.test/x",
    retrieved_at=SEASON_START, sha256="0" * 64,
)


def _week(n: int) -> datetime:
    return SEASON_START + timedelta(days=7 * (n - 1))


def _player_id(provider_id: str) -> str:
    return stable_record_id("player", "espn", provider_id)


def _statline(event_id: str, provider_id: str, team_abbr: str, week_number: int, *, yards: str = "280") -> PlayerGameStatline:
    return PlayerGameStatline(
        id=stable_record_id("player_game_statline", "espn", event_id, provider_id, "passing"),
        provider_ids={"espn": provider_id},
        event_time=_week(week_number),
        retrieved_at=_week(week_number),
        source_snapshots=(_SOURCE,),
        player_id=_player_id(provider_id),
        team_id=team_id(team_abbr),
        game_id=stable_record_id("game", "espn", event_id),
        stat_category="passing",
        stat_labels=("YDS", "TD", "INT"),
        stat_values=(yards, "2", "0"),
    )


@pytest.fixture
def injury_context(tmp_path):
    return {"store": ResearchStore(root=tmp_path / "store"), "cutoff": None, "game_id": None}


def _seed_established_starter(store: ResearchStore) -> None:
    games, statlines = [], []
    for i in range(1, 5):
        eid = f"g{i}"
        games.append(
            make_game(
                event_id=eid, season_year=2025, week=i, home_abbr="BUF", away_abbr="MIA",
                kickoff_at=_week(i), home_score=27, away_score=10, completed=True,
            )
        )
        statlines.append(_statline(eid, "starterQB", "BUF", i))
        statlines.append(_statline(eid, "miaQB", "MIA", i, yards="180"))
        if i == 1:
            # A backup QB usage entry so estimate_player_impact has a real
            # replacement to compare against (find_replacement needs a
            # second player in the same team/production group).
            statlines.append(_statline(eid, "backupQB", "BUF", i, yards="40"))
    store.write(games)
    store.write(statlines)

    upcoming = make_game(
        event_id="g5", season_year=2025, week=5, home_abbr="BUF", away_abbr="MIA",
        kickoff_at=_week(5), home_score=None, away_score=None, completed=False,
    )
    store.write([upcoming])


@given("a season with an established home-team starter and enough opponent history")
def established_starter(injury_context):
    _seed_established_starter(injury_context["store"])
    injury_context["cutoff"] = _week(5) - timedelta(hours=24)
    injury_context["game_id"] = stable_record_id("game", "espn", "g5")


def _availability_report(player_provider_id: str, *, provider: str, status_text: str, as_of: datetime) -> AvailabilityReport:
    return AvailabilityReport(
        id=stable_record_id("availability_report", provider, player_provider_id, as_of.isoformat()),
        provider_ids={provider: player_provider_id},
        event_time=as_of,
        retrieved_at=as_of,
        source_snapshots=(_SOURCE,),
        player_id=_player_id(player_provider_id),
        team_id=team_id("BUF"),
        game_id=stable_record_id("game", "espn", "g5"),
        report_class=AvailabilityReportClass.INJURY_STATUS,
        status_text=status_text,
        source_confidence=Decimal("0.8"),
    )


@given("that starter is confirmed out by two independent sources")
def confirmed_out(injury_context):
    as_of = injury_context["cutoff"] - timedelta(hours=1)
    reports = [
        _availability_report("starterQB", provider="beat-reporter-a", status_text="Out", as_of=as_of),
        _availability_report("starterQB", provider="beat-reporter-b", status_text="Out", as_of=as_of),
    ]
    injury_context["store"].write(reports)


@given("that starter is confirmed questionable by two independent sources")
def confirmed_questionable(injury_context):
    as_of = injury_context["cutoff"] - timedelta(hours=1)
    reports = [
        _availability_report("starterQB", provider="beat-reporter-a", status_text="Questionable", as_of=as_of),
        _availability_report("starterQB", provider="beat-reporter-b", status_text="Questionable", as_of=as_of),
    ]
    injury_context["store"].write(reports)


@given("that starter has only one uncorroborated report claiming he is out")
def uncorroborated_out(injury_context):
    as_of = injury_context["cutoff"] - timedelta(hours=1)
    injury_context["store"].write(
        [_availability_report("starterQB", provider="beat-reporter-a", status_text="Out", as_of=as_of)]
    )


@given("no availability reports exist for that starter")
def no_reports(injury_context):
    pass  # nothing written -- the given season already has none


@when("forecasts are generated with and without the injury adjustment")
def generate_both(injury_context):
    store, cutoff, game_id = injury_context["store"], injury_context["cutoff"], injury_context["game_id"]
    injury_context["adjusted"] = generate_forecast(
        store, game_id, feature_cutoff_at=cutoff, exponent=DEFAULT_EXPONENT, apply_injury_adjustment=True
    )
    injury_context["unadjusted"] = generate_forecast(
        store, game_id, feature_cutoff_at=cutoff, exponent=DEFAULT_EXPONENT, apply_injury_adjustment=False
    )


@then("the injury-aware forecast gives the home team a lower win probability than the unadjusted forecast")
def adjusted_is_lower(injury_context):
    assert injury_context["adjusted"].home_win_probability < injury_context["unadjusted"].home_win_probability


@then("the forecast records which player triggered the adjustment")
def records_player(injury_context):
    assert _player_id("starterQB") in injury_context["adjusted"].injury_adjusted_player_ids
    assert injury_context["adjusted"].injury_adjustment != Decimal("0")


@then("both forecasts report the same win probability")
def same_probability(injury_context):
    assert injury_context["adjusted"].home_win_probability == injury_context["unadjusted"].home_win_probability
    assert injury_context["adjusted"].injury_adjustment == Decimal("0")


@given("a season with one completed game and one not-yet-played game")
def completed_and_upcoming_games(injury_context):
    store = injury_context["store"]
    games = [
        make_game(
            event_id="done1", season_year=2025, week=1, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=_week(1), home_score=27, away_score=10, completed=True,
        ),
        make_game(
            event_id="upcoming1", season_year=2025, week=2, home_abbr="BUF", away_abbr="MIA",
            kickoff_at=_week(2), home_score=None, away_score=None, completed=False,
        ),
    ]
    store.write(games)


@when("current injuries are ingested for that season")
def run_ingest(injury_context, monkeypatch):
    store = injury_context["store"]
    connector = EspnConnector()
    queried_event_ids = []

    async def fake_game_summary(event_id, *, refresh=False):
        queried_event_ids.append(event_id)
        team = NFLTeam(espn_team_id="2", abbreviation="BUF", name="Buffalo Bills")
        athlete = NFLAthlete(espn_athlete_id="starterQB", display_name="Test Starter", position="QB")
        injury = NFLInjuryReport(
            event_id=event_id, team=team, athlete=athlete, status_text="Out",
            reported_at=SEASON_START, retrieved_at=SEASON_START,
            source_url=f"https://example.test/summary?event={event_id}",
            raw_snapshot_path=f"raw/espn/event-{event_id}/x.json",
            raw_snapshot_sha256="0" * 64, normalization_version="espn-summary-v1",
        )
        return [], [injury]

    monkeypatch.setattr(connector, "game_summary", fake_game_summary)
    injury_context["queried_event_ids"] = queried_event_ids
    injury_context["ingest_report"] = asyncio.run(ingest_current_injuries(connector, store, 2025))


@then("only the not-yet-played game is considered for ingestion")
def only_upcoming_considered(injury_context):
    assert injury_context["queried_event_ids"] == ["upcoming1"]
    assert injury_context["ingest_report"].games_considered == 1
