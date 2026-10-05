"""Reproducible, point-in-time opportunity hypotheses; no LLM probability."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json

from sgr.algorithms.value import ValueStrategy
from sgr.models import MarketSnapshot
from sgr.paper.ledger import fee_per_share, fresh
from sgr.paper.models import AnalysisDecision, PaperPolicy
from sgr.research.calibration import generate_calibrated_forecast, select_calibration_method
from sgr.research.contracts import ContractDefinition, equivalent, match_contract
from sgr.research.injury_adjustment import compute_injury_adjustment
from sgr.research.pythagorean import PythagoreanModelError
from sgr.research.schemas import (
    AvailabilityReport, Forecast, Game, PlayerGameStatline, PolymarketMarket,
    TokenBookSnapshot, stable_record_id,
)
from sgr.research.storage import ResearchStore
from sgr.risk.bankroll import fixed_paper_notional


class PointInTimeInputs:
    """A frozen model-only view: prices are never exposed to the forecaster."""
    def __init__(self, records: dict[str, list], cutoff: datetime):
        self.cutoff = cutoff
        self.records = {}
        for entity, batch in records.items():
            visible = [r for r in batch if r.retrieved_at <= cutoff and all(s.retrieved_at <= cutoff for s in r.source_snapshots)]
            if entity != "game":
                visible = [r for r in visible if r.event_time <= cutoff]
            by_provider = {}
            for record in sorted(visible, key=lambda r: (r.retrieved_at, r.id)):
                provider = (record.provider_ids.get("espn") or (record.home_team_id, record.away_team_id, record.kickoff_at)) if isinstance(record, Game) else record.id
                if isinstance(record, Game):
                    if record.completed and record.retrieved_at < record.kickoff_at:
                        continue
                    old = by_provider.get(provider)
                    if old and old.completed and record.completed and (old.kickoff_at, old.home_team_id, old.away_team_id, old.home_score, old.away_score) != (record.kickoff_at, record.home_team_id, record.away_team_id, record.home_score, record.away_score):
                        raise ValueError("Conflicting duplicate NFL event evidence.")
                    if old and old.completed and not record.completed:
                        continue
                by_provider[provider] = record
            self.records[entity] = sorted(by_provider.values(), key=lambda r: r.id)
        content = {key: [r.model_dump(mode="json") for r in batch] for key, batch in sorted(self.records.items())}
        self.digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_store(cls, store: ResearchStore, cutoff: datetime):
        if not store.database_path.exists():
            return cls({k: [] for k in ("game", "availability_report", "player_game_statline")}, cutoff)
        return cls({k: store.load_all_as_of(k, cutoff, all_versions=k == "game") for k in ("game", "availability_report", "player_game_statline")}, cutoff)

    def load_all(self, entity):
        return list(self.records.get(entity, []))

    def load(self, entity, record_id):
        matches = [x for x in self.load_all(entity) if x.id == record_id]
        if len(matches) != 1:
            raise KeyError("Model input identity missing or ambiguous.")
        return matches[0]


@dataclass(frozen=True)
class Benchmark:
    definition: ContractDefinition
    observed_at: datetime
    available_at: datetime
    probability: Decimal
    source_id: str
    method: str


def devig_decimal_odds(home: Decimal, away: Decimal) -> Decimal:
    if home <= 1 or away <= 1:
        raise ValueError("Two-sided decimal odds must exceed one.")
    return away / (home + away)


class CostAdjustedNFLStrategy(ValueStrategy):
    """Existing strategy plugin, with executable costs and conservative selection."""
    name = "nfl-cost-adjusted-v1"

    def hypotheses(self, definition, books, probability, benchmarks, decision_at):
        results = [{"family": "nfl_forecast", "status": "evaluated", "probability": str(probability)}]
        valid = [b for b in benchmarks if equivalent(definition, b.definition) and b.definition.available_at <= decision_at and b.observed_at <= b.available_at <= decision_at]
        results.extend({"family": "consensus_disagreement", "source": b.source_id, "method": b.method, "residual": str(probability-b.probability), "status": "hypothesis"} for b in valid)
        if not valid:
            results.append({"family": "consensus_disagreement", "status": "unknown", "reason": "no_exact_point_in_time_benchmark"})
        if len(books) == 2 and all(b.asks for b in books):
            quotes = [min(b.asks, key=lambda x: x.price_dollars) for b in books]
            cost = sum(q.price_dollars + fee_per_share(q.price_dollars, b.fee_rate_bps) for q, b in zip(quotes, books))
            results.append({"family": "exhaustive_outcomes", "status": "hypothesis", "combined_unit_cost": str(cost), "payout_if_final": "1", "depth_shares": str(min(q.contracts for q in quotes)), "atomic_execution_supported": False})
        return results


class Analyzer:
    def __init__(self, inputs: PointInTimeInputs, policy: PaperPolicy, definitions: list[ContractDefinition]):
        self.inputs, self.policy, self.definitions = inputs, policy, definitions
        self.calibrations = {}

    def screen(self, market: PolymarketMarket, now: datetime):
        if self.inputs.cutoff > now:
            from sgr.research.contracts import ContractMatch
            return ContractMatch(eligible=False, reason="future_model_input_cutoff")
        return match_contract(market, self.inputs.load_all("game"), self.definitions, decision_at=now)

    def analyze(self, market: PolymarketMarket, books: list[TokenBookSnapshot], *, now: datetime, benchmarks: list[Benchmark] | None = None) -> tuple[AnalysisDecision, Forecast | None]:
        policy = self.policy
        match = self.screen(market, now)
        base = dict(
            market_id=market.id, condition_id=market.condition_id, event_id=market.event_id,
            decision_at=now, expires_at=now + timedelta(seconds=policy.decision_ttl_seconds),
            rule_version=market.rule_version, policy_fingerprint=policy.fingerprint,
            input_digest=self.inputs.digest, snapshot_ids=tuple(b.id for b in books),
            feature_cutoff_at=self.inputs.cutoff,
        )
        identity = (market.condition_id, market.id, market.rule_version, self.inputs.digest,
                    *base["snapshot_ids"], CostAdjustedNFLStrategy.name, policy.fingerprint, now.isoformat())
        def rejected(reason, forecast=None):
            lineage = {} if forecast is None else dict(forecast_id=forecast.id, model_version=forecast.model_version,
                                                       calibration_version=forecast.calibration_version)
            return AnalysisDecision(id=stable_record_id("analysis", *identity, reason), eligible=False, reasons=(reason,), **lineage, **base), forecast
        if not match.eligible:
            return rejected(match.reason)
        definition = match.definition
        if len(books) != 2 or {b.asset_id for b in books} != set(market.asset_ids) or any(b.condition_id != market.condition_id or not fresh(b, now, policy) or not b.asks for b in books):
            return rejected("missing_stale_or_incoherent_executable_books")
        game = self.inputs.load("game", definition.game_id)
        try:
            years = sorted({g.season_year for g in self.inputs.load_all("game") if g.completed and g.season_year < game.season_year})
            calibration = self.calibrations.get(game.season_year)
            if calibration is None:
                calibration = select_calibration_method(self.inputs, years)
                self.calibrations[game.season_year] = calibration
            forecast = generate_calibrated_forecast(
                self.inputs, game.id, calibration, feature_cutoff_at=self.inputs.cutoff, forecast_created_at=now,
            )
            injury = compute_injury_adjustment(
                self.inputs, self.inputs.load_all("game"), self.inputs.load_all("player_game_statline"),
                self.inputs.load_all("availability_report"), game.home_team_id, game.away_team_id,
                game.season_year, self.inputs.cutoff, neutral_site=bool(game.neutral_site), exponent=float(forecast.exponent),
            )
            probability = max(Decimal(".000001"), min(Decimal(".999999"), forecast.home_win_probability + Decimal(str(injury.home_win_probability_delta))))
            model_version = forecast.model_version + "+point-in-time-injury-v1:" + hashlib.sha256(repr(calibration).encode()).hexdigest()[:16]
            refs = {(s.provider, s.path, s.sha256, s.retrieved_at): s for records in self.inputs.records.values() for r in records for s in r.source_snapshots}
            forecast = Forecast.model_validate({
                **forecast.model_dump(), "id": stable_record_id("forecast", game.id, model_version, self.inputs.digest, self.inputs.cutoff.isoformat(), now.isoformat()),
                "model_version": model_version, "home_win_probability": probability,
                "injury_adjustment": Decimal(str(injury.home_win_probability_delta)),
                "injury_adjusted_player_ids": tuple(a.player_id for a in injury.adjustments),
                "source_snapshots": tuple(refs.values()),
            })
        except (PythagoreanModelError, ValueError, KeyError) as error:
            return rejected("forecast_unavailable:" + type(error).__name__)
        by_asset = {b.asset_id: b for b in books}
        ordered = [by_asset[a] for a in market.asset_ids]
        home_index = definition.home_outcome_index
        probs = [probability if index == home_index else 1-probability for index in range(2)]
        best_asks = [min(b.asks, key=lambda x: x.price_dollars) for b in ordered]
        effective_costs = [
            ask.price_dollars + fee_per_share(ask.price_dollars, b.fee_rate_bps)
            + policy.uncertainty_haircut + forecast.uncertainty * Decimal(".05")
            for b, ask in zip(ordered, best_asks)
        ]
        # Reuse the strategy plugin for independently generated probabilities.
        strategy = CostAdjustedNFLStrategy(min_edge=float(policy.min_net_edge))
        signals = strategy.generate(
            [MarketSnapshot(market_id=market.id, ticker=market.external_id, title=market.question, yes_price=min(1,float(effective_costs[0])), no_price=min(1,float(effective_costs[1])), ts=now)],
            {market.id: float(probs[0])},
        )
        hypotheses = strategy.hypotheses(definition, ordered, probability, benchmarks or [], now)
        hypotheses.append({"family": "injury_response", "status": "adjusted" if injury.adjustments else "unknown",
                           "delta": str(injury.home_win_probability_delta),
                           "reason": "requires_corroborated_timestamped_reports_and_future_response_snapshots"})
        if not signals:
            return rejected("cost_adjusted_edge_below_policy", forecast)
        best = max(range(2), key=lambda i: probs[i]-effective_costs[i])
        if probs[best]-effective_costs[best] < policy.min_net_edge:
            return rejected("decimal_edge_below_policy", forecast)
        if best_asks[best].contracts * policy.depth_fraction < ordered[best].minimum_order_size:
            return rejected("conservative_depth_below_minimum", forecast)
        return AnalysisDecision(
            id=stable_record_id("analysis", *identity, model_version, forecast.calibration_version),
            eligible=True, reasons=("cost_adjusted_forecast_hypothesis",),
            asset_id=ordered[best].asset_id, game_id=game.id, kickoff_at=game.kickoff_at,
            groups=tuple(sorted(("nfl:team:"+game.home_team_id, "nfl:team:"+game.away_team_id))),
            probability=probs[best], net_edge=probs[best]-effective_costs[best],
            uncertainty_cost=policy.uncertainty_haircut + forecast.uncertainty * Decimal(".05"),
            limit_price=best_asks[best].price_dollars,
            requested_notional=fixed_paper_notional(policy.initial_capital, policy.stake, policy.contract_cap),
            forecast_id=forecast.id, model_version=model_version, calibration_version=forecast.calibration_version,
            families=tuple(hypotheses), **{**base, "event_id": game.id},
        ), forecast
