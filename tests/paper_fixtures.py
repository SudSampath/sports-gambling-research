"""Synthetic public contracts, depth, and capital; never account data."""
from datetime import datetime, timedelta, timezone
import hashlib

from sgr.models import NFLSeasonType
from sgr.research.contracts import ContractDefinition
from sgr.research.schemas import Game, RawSnapshotRef, stable_record_id
from sgr.connectors.polymarket import normalize_market

NOW = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
RULES = "Home wins including overtime: Home pays 1; Away pays 0. If cancelled both pay 0.5. NFL official final score is the resolution source."


def source(now=NOW):
    return RawSnapshotRef(provider="synthetic", path="synthetic.json", source_url="https://example.org/public", retrieved_at=now, sha256="0" * 64)


def row(index=1):
    return {
        "id": str(index), "conditionId": f"condition-{index}", "question": "Home vs Away",
        "slug": f"nfl-home-away-{index}", "description": RULES,
        "outcomes": '["Home", "Away"]', "clobTokenIds": f'["{index}01", "{index}02"]',
        "endDate": (NOW + timedelta(days=3)).isoformat(),
        "gameStartTime": (NOW + timedelta(days=1)).isoformat(),
        "active": True, "closed": False, "acceptingOrders": True, "sportsMarketType": "moneyline",
        "negRisk": False, "feeType": "sports_fees_v2",
        "events": [{"id": str(index), "slug": "synthetic-game", "title": "Home vs Away", "resolutionSource": "https://www.nfl.com/"}],
    }


def game(index=1, *, completed=False, when=None, year=2026, season_type=NFLSeasonType.POSTSEASON, home_score=30, away_score=14, retrieved=NOW):
    when = when or NOW + timedelta(days=1)
    return Game(
        id=stable_record_id("game", "synthetic", index), provider_ids={"espn": f"synthetic-{index}"},
        event_time=when, retrieved_at=retrieved, source_snapshots=(source(retrieved),),
        season_year=year, season_type=season_type, week=1, home_team_id="home", away_team_id="away",
        kickoff_at=when, status="FINAL" if completed else "SCHEDULED", completed=completed, neutral_site=False,
        home_score=home_score if completed else None, away_score=away_score if completed else None,
    )


def contract(index=1):
    market = normalize_market(row(index), source())[1]
    target = game(index)
    definition = ContractDefinition(
        condition_id=market.condition_id, rule_version=hashlib.sha256(RULES.encode()).hexdigest(),
        game_id=target.id, home_team_id=target.home_team_id, away_team_id=target.away_team_id,
        kickoff_at=target.kickoff_at, deadline_at=market.deadline_at, outcomes=market.outcomes,
        home_outcome_index=0, tie_payout="0.5", rule_source_url="https://polymarket.com/event/" + market.slug,
        asset_ids=market.asset_ids, resolution_source=market.resolution_source, available_at=NOW,
        rule_quote=RULES,
    )
    return market, target, definition


def book_payload(asset="101", condition="condition-1", *, depth="200"):
    return {
        "asset_id": asset, "market": condition, "timestamp": str(int(NOW.timestamp()*1000)), "hash": "synthetic",
        "bids": [{"price": "0.39", "size": depth}], "asks": [{"price": "0.40", "size": depth}],
        "tick_size": "0.01", "min_order_size": "1", "neg_risk": False,
    }
