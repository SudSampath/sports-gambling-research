"""Labeled deterministic workload generator, not padded live observations."""
from datetime import timedelta
from decimal import Decimal
import hashlib

from sgr.connectors.polymarket import normalize_market
from sgr.models import NFLSeasonType
from sgr.research.contracts import ContractDefinition
from sgr.research.schemas import Game, PriceLevel, TokenBookSnapshot, stable_record_id

RULES = "Home wins including overtime: Home pays 1; Away pays 0. If cancelled both pay 0.5. NFL official final score is the resolution source."


def seed(store, campaign, now):
    source = store.retain_raw_snapshot("synthetic", {"generator": "synthetic-nfl-v1", "campaign": campaign, "seed": 1},
                                       source_url="https://example.org/sgr/synthetic", retrieved_at=now)
    target = Game(
        id=stable_record_id("game", "synthetic", campaign), provider_ids={"espn": "synthetic-"+campaign},
        event_time=now+timedelta(days=1), retrieved_at=now, source_snapshots=(source,),
        season_year=now.year, season_type=NFLSeasonType.POSTSEASON, week=1,
        home_team_id="synthetic-home", away_team_id="synthetic-away",
        kickoff_at=now+timedelta(days=1), completed=False, status="SCHEDULED", neutral_site=False,
    )
    history = [Game(
        id=stable_record_id("game", "synthetic", campaign, i), provider_ids={"espn": f"synthetic-{campaign}-{i}"},
        event_time=now-timedelta(days=100+i*7), retrieved_at=now, source_snapshots=(source,),
        season_year=now.year-1, season_type=NFLSeasonType.REGULAR, week=i+1,
        home_team_id=target.home_team_id, away_team_id=target.away_team_id,
        kickoff_at=now-timedelta(days=100+i*7), completed=True, status="FINAL", neutral_site=False,
        home_score=30, away_score=14,
    ) for i in range(10)]
    store.write([target, *history])
    return target, source


def candidate(index, target, source):
    supported = index % 200 == 0
    row = {
        "id": f"synthetic-{index}", "conditionId": f"synthetic-condition-{index}",
        "slug": f"synthetic-nfl-{index}", "question": "Synthetic Home vs Away" if supported else "Synthetic unsupported total",
        "description": RULES, "outcomes": ["Home", "Away"], "clobTokenIds": [f"synthetic-{index}-home", f"synthetic-{index}-away"],
        "endDate": (target.kickoff_at+timedelta(days=2)).isoformat(), "gameStartTime": target.kickoff_at.isoformat(),
        "sportsMarketType": "moneyline" if supported else "total", "active": True, "closed": False,
        "acceptingOrders": True, "negRisk": False, "feeType": "synthetic",
        "events": [{"id": target.id, "slug": "synthetic-game", "title": "Synthetic Home vs Away", "resolutionSource": "https://example.org/sgr/synthetic"}],
    }
    market = normalize_market(row, source)[1]
    definition = ContractDefinition(
        condition_id=market.condition_id, rule_version=market.rule_version, game_id=target.id,
        home_team_id=target.home_team_id, away_team_id=target.away_team_id,
        kickoff_at=target.kickoff_at, deadline_at=market.deadline_at, outcomes=market.outcomes,
        asset_ids=market.asset_ids, resolution_source=market.resolution_source, available_at=source.retrieved_at,
        home_outcome_index=0, tie_payout=Decimal(".5"), rule_source_url="https://polymarket.com/event/"+market.slug, rule_quote=RULES,
    )
    return market, definition


def books(market, now, source):
    result = []
    for i, asset in enumerate(market.asset_ids):
        price = Decimal(".4") if i == 0 else Decimal(".55")
        result.append(TokenBookSnapshot(
            id=stable_record_id("token_book_snapshot", asset, now.isoformat()),
            provider_ids={"asset": asset}, event_time=now, retrieved_at=now, source_snapshots=(source,),
            market_id=market.id, condition_id=market.condition_id, asset_id=asset, observed_at=now,
            available_at=now, metadata_at=now, bids=(PriceLevel(price_dollars=price-Decimal(".01"), contracts=Decimal(200)),),
            asks=(PriceLevel(price_dollars=price, contracts=Decimal(200)),),
            tick_size=Decimal(".01"), minimum_order_size=Decimal(1), fee_rate_bps=500,
        ))
    return result
