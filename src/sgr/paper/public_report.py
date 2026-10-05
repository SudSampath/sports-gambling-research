"""Explicit public read models. Never serialize internal reports wholesale."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sgr.paper.models import PaperPolicy

Code = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_:.+\-]{1,160}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Money = Annotated[Decimal, Field(allow_inf_nan=False)]
Count = Annotated[int, Field(strict=True, ge=0, le=10000)]


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(PublicModel):
    id: Code
    market_id: Code
    asset_id: Code | None
    forecast_id: Code | None
    contract: Code
    event: Code
    eligible: bool
    reasons: list[Code]
    probability: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)] | None
    net_edge: Annotated[Decimal, Field(ge=-1, le=1, allow_inf_nan=False)] | None
    limit_price: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)] | None
    uncertainty_cost: Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
    decision_at: datetime
    feature_cutoff_at: datetime
    expires_at: datetime
    model_version: Code
    calibration_version: Code
    strategy_version: Code
    rule_version: Digest
    input_digest: Digest
    policy_fingerprint: Digest
    snapshot_ids: list[Code]

    @model_validator(mode="after")
    def chronology(self):
        for value in (self.decision_at, self.feature_cutoff_at, self.expires_at):
            if value.tzinfo is None:
                raise ValueError("Public evidence requires timezone-aware timestamps")
        if self.feature_cutoff_at > self.decision_at or self.expires_at <= self.decision_at:
            raise ValueError("Invalid point-in-time decision lineage")
        if self.model_version == "unknown" and self.probability is not None:
            raise ValueError("Unknown models cannot supply probabilities")
        if any(reason.startswith("unsupported_") or "ambiguous" in reason for reason in self.reasons) and self.probability is not None:
            raise ValueError("Unsupported or ambiguous outcomes must remain unknown")
        if self.eligible and (self.probability is None or self.net_edge is None):
            raise ValueError("Eligible evidence requires a supported forecast")
        return self


class Position(PublicModel):
    decision_id: Code
    contract: Code
    state: Literal["pending", "reserved", "submitted", "partially_filled", "filled",
                   "pending_settlement", "cancelled", "expired", "rejected", "disputed", "settled"]
    shares: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    spent: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    reservation: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    reason: Code | None


class Settlement(PublicModel):
    decision_id: Code
    state: Literal["settled", "disputed"]
    settled_at: datetime
    payout: Money
    realized_pnl: Money
    rule_version: Digest

    @model_validator(mode="after")
    def timezone(self):
        if self.settled_at.tzinfo is None:
            raise ValueError("Settlement timestamps must be timezone-aware")
        return self


class Mark(PublicModel):
    decision_id: Code
    liquidation_value: Money
    unpriced_shares: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    marked_at: datetime

    @model_validator(mode="after")
    def timezone(self):
        if self.marked_at.tzinfo is None:
            raise ValueError("Mark timestamps must be timezone-aware")
        return self


class Capital(PublicModel):
    fictional: Literal[True]
    scope: Literal["shared portfolio; displayed positions belong to this campaign"]
    cash: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    reserved: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    available: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    realized_pnl: Money
    paused: bool
    reconciled: bool

    @model_validator(mode="after")
    def accounting(self):
        if self.available != self.cash - self.reserved:
            raise ValueError("Cash/reservation accounting does not reconcile")
        return self


class Policy(PublicModel):
    version: Code
    initial_capital: Money
    stake: Money
    contract_cap: Money
    event_cap: Money
    correlated_cap: Money
    total_cap: Money
    loss_limit: Money
    min_net_edge: Money
    uncertainty_haircut: Money
    depth_fraction: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)]
    quote_max_age_seconds: Annotated[int, Field(ge=1, le=60)]
    decision_ttl_seconds: Annotated[int, Field(ge=1, le=300)]
    latency_ms: Annotated[int, Field(ge=0, le=30000)]


class PublicCampaign(PublicModel):
    schema_version: Literal[1] = 1
    public_id: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")]
    mode: Literal["public", "synthetic"]
    status: Code
    started_at: datetime
    scan_finished_at: datetime | None
    exported_at: datetime
    pages: Annotated[int, Field(ge=0, le=100)]
    requests: Annotated[int, Field(ge=0, le=1000)]
    completed_candidates: Count
    genuine_live_candidates: Count
    synthetic_candidates: Count
    eligible_decisions: Count
    exclusions: dict[Code, Count]
    risk_exclusions: dict[Code, Count]
    missing_audited_catalog: bool
    candidate_total: Count
    candidates: Annotated[list[Candidate], Field(max_length=200)]
    position_total: Count
    filled_position_total: Count
    positions: Annotated[list[Position], Field(max_length=200)]
    settlement_total: Count
    settlements: Annotated[list[Settlement], Field(max_length=200)]
    marked_estimate_total: Count
    marked_estimates: Annotated[list[Mark], Field(max_length=200)]
    capital: Capital
    policy: Policy
    source_report_sha256: Digest

    @model_validator(mode="after")
    def integrity(self):
        for value in (self.started_at, self.scan_finished_at, self.exported_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("Public evidence requires timezone-aware timestamps")
        if self.started_at > self.exported_at or (self.scan_finished_at is not None and not self.started_at <= self.scan_finished_at <= self.exported_at):
            raise ValueError("Campaign timestamps are out of order")
        if self.mode == "public" and self.synthetic_candidates:
            raise ValueError("Public counts cannot include synthetic candidates")
        if self.mode == "synthetic" and self.genuine_live_candidates:
            raise ValueError("Synthetic counts cannot include genuine markets")
        if self.mode == "synthetic" and self.synthetic_candidates != self.completed_candidates:
            raise ValueError("Synthetic coverage mismatch")
        if self.eligible_decisions > self.completed_candidates:
            raise ValueError("Eligible decisions exceed completed analyses")
        if not self.capital.reconciled:
            raise ValueError("Do not publish unreconciled portfolio evidence")
        if len(self.candidates) > self.candidate_total or len(self.positions) > self.position_total:
            raise ValueError("Sample counts exceed totals")
        if len(self.settlements) > self.settlement_total or len(self.marked_estimates) > self.marked_estimate_total:
            raise ValueError("Outcome sample counts exceed totals")
        eligible_sample = sum(c.eligible for c in self.candidates)
        if eligible_sample > self.eligible_decisions or (len(self.candidates) == self.candidate_total and eligible_sample != self.eligible_decisions):
            raise ValueError("Sample eligibility contradicts coverage")
        if self.candidate_total > self.completed_candidates:
            raise ValueError("Decision records exceed completed analyses")
        sampled_exclusions = Counter(reason for c in self.candidates if not c.eligible for reason in c.reasons)
        if any(n > self.exclusions.get(reason, 0) for reason, n in sampled_exclusions.items()):
            raise ValueError("Sample exclusions contradict coverage")
        if len({c.id for c in self.candidates}) != len(self.candidates):
            raise ValueError("Duplicate public decisions")
        position_ids = {p.decision_id for p in self.positions}
        if len(position_ids) != len(self.positions):
            raise ValueError("Duplicate public position records")
        filled_sample = sum(p.shares > 0 for p in self.positions)
        if (filled_sample > self.filled_position_total or self.filled_position_total > self.position_total
                or (len(self.positions) == self.position_total and filled_sample != self.filled_position_total)):
            raise ValueError("Filled position totals contradict their sample")
        if len(self.positions) == self.position_total and any(s.decision_id not in position_ids for s in [*self.settlements, *self.marked_estimates]):
            raise ValueError("Outcome references an absent position")
        for rows in (self.settlements, self.marked_estimates):
            if len({r.decision_id for r in rows}) != len(rows):
                raise ValueError("Duplicate outcome records")
        if self.mode == "public" and any(c.decision_at > self.exported_at for c in self.candidates):
            raise ValueError("Public decisions cannot use a virtual future timestamp")
        if self.mode == "public" and any(s.settled_at > self.exported_at for s in self.settlements):
            raise ValueError("Public settlement cannot use a virtual future timestamp")
        if self.mode == "public" and any(m.marked_at > self.exported_at for m in self.marked_estimates):
            raise ValueError("Public marks cannot use a virtual future timestamp")
        policy = PaperPolicy.model_validate(self.policy.model_dump())
        if any(c.policy_fingerprint != policy.fingerprint for c in self.candidates):
            raise ValueError("Published policy does not match decision lineage")
        return self


def public_campaign(report: dict, public_id: str) -> PublicCampaign:
    """Project allowlisted fields; all unknown internal fields stay private.

    Reports contain campaign-scoped rows but portfolio-wide balances. Limit row
    samples explicitly rather than making a 10,000-row UI look like full coverage.
    """
    campaign, coverage, portfolio = (report[k] for k in ("campaign", "coverage", "portfolio"))
    mode, portfolio_id = campaign["spec"]["mode"], campaign["spec"]["portfolio"]
    if (mode == "synthetic") != portfolio_id.startswith("synthetic-") or portfolio["portfolio"] != portfolio_id:
        raise ValueError("Campaign and portfolio source modes do not match")
    decisions = [r["decision"] for r in report["results"] if r.get("decision")]
    errors = [r["error"] for r in report["results"] if not r.get("decision") and r.get("error")]
    exclusions = Counter(errors)
    for decision in decisions:
        if not decision["eligible"]:
            exclusions.update(decision["reasons"])
    if (len(decisions) + len(errors) != coverage["completed_candidates"]
            or len({d["id"] for d in decisions}) != len(decisions)
            or sum(d["eligible"] for d in decisions) != coverage["eligible_decisions"]
            or dict(exclusions) != coverage["exclusions"]):
        raise ValueError("Decision records do not match aggregate coverage")
    # Keep supported rankings and a representative unknown sample distinct.
    all_candidates = [Candidate(**{
        **{k: d[k] for k in ("id", "market_id", "asset_id", "forecast_id", "eligible", "reasons", "probability", "net_edge", "limit_price", "uncertainty_cost",
                             "decision_at", "feature_cutoff_at", "expires_at", "model_version",
                             "calibration_version", "strategy_version", "rule_version", "input_digest", "policy_fingerprint", "snapshot_ids")},
        "contract": d["condition_id"], "event": d["event_id"],
    }) for d in decisions]
    ordered = sorted(all_candidates, key=lambda d: (not d.eligible, -(d.net_edge if d.net_edge is not None else Decimal("-1")), d.id))
    candidates = [d for d in ordered if d.eligible][:100] + [d for d in ordered if not d.eligible][:100]
    rows = portfolio["positions"]
    # Inventory before rejected admission attempts, with a disclosed sample cap.
    position_sample = sorted(rows, key=lambda p: (not Decimal(p["quantity"]), p["decision"]))[:200]
    positions = [Position(decision_id=p["decision"], contract=p["contract"], state=p["status"],
                          shares=p["quantity"], spent=Decimal(p["spent"])/1000000,
                          reservation=Decimal(p["remaining"])/1000000, reason=p["reason"] or None)
                 for p in position_sample]
    settlements = [Settlement(decision_id=s["decision_id"], state=s["status"], settled_at=s["settled_at"],
                              payout=Decimal(s["payout_micros"])/1000000,
                              realized_pnl=Decimal(s["pnl_micros"])/1000000, rule_version=s["rule_version"])
                   for s in portfolio["settlements"][:200]]
    marks = [Mark(**{k: m[k] for k in Mark.model_fields})
             for m in portfolio["marked_estimates"][:200]]
    policy = campaign["spec"]["policy"]
    return PublicCampaign(
        public_id=public_id, mode=campaign["spec"]["mode"], status=campaign["status"],
        started_at=campaign["started"], scan_finished_at=campaign["detail"].get("finished_at"),
        exported_at=datetime.now(timezone.utc), pages=campaign["pages"], requests=campaign["requests"],
        **{k: coverage[k] for k in ("completed_candidates", "genuine_live_candidates", "synthetic_candidates",
                                  "eligible_decisions", "exclusions")},
        risk_exclusions=dict(Counter(p["reason"] for p in rows if p["status"] == "rejected")),
        missing_audited_catalog=not bool(campaign["spec"]["definitions"]),
        candidate_total=len(decisions), candidates=candidates, position_total=len(rows),
        filled_position_total=sum(Decimal(p["quantity"]) > 0 for p in rows), positions=positions,
        settlement_total=len(portfolio["settlements"]), settlements=settlements,
        marked_estimate_total=len(portfolio["marked_estimates"]), marked_estimates=marks,
        capital=Capital(**{k: portfolio[k] for k in ("fictional", "cash", "reserved", "available", "realized_pnl")},
                        paused=bool(portfolio["paused"]),
                        reconciled=portfolio["ok"], scope="shared portfolio; displayed positions belong to this campaign"),
        policy=Policy(**{k: policy[k] for k in Policy.model_fields}),
        source_report_sha256=hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
    )


def write_public_campaign(report: dict, public_id: str, out: Path) -> PublicCampaign:
    model = public_campaign(report, public_id)  # Validate completely before touching output.
    if out.suffix != ".json":
        raise ValueError("Public exports must use a .json file")
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(".json.tmp")
    temporary.write_text(model.model_dump_json(indent=2) + "\n")
    temporary.replace(out)
    return model


class StressEvidence(PublicModel):
    completed_candidates: Count
    eligible_decisions: Count
    paper_positions: Count
    seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    candidates_per_second: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    worker_peak_rss_kib: Annotated[int, Field(ge=0)]
    runtime_peak_rss_kib: Annotated[int, Field(ge=0)]
    requests: Count
    invariants: bool


class RecoveryEvidence(PublicModel):
    completed_page_attempts: Count
    completed_analysis_attempts: Count
    fill_attempts: list[Count]
    fill_reconciliation_attempts: list[Count]
    offline_unchanged: bool
    replay_unchanged: bool
    invariants: bool
    settlement_once: bool
    synthetic_virtual_settlement_time: datetime


class PauseEvidence(PublicModel):
    paused: bool
    checkpoint_stable: bool
    resumed_completed: bool
    invariants: bool


class PublicEvidence(PublicModel):
    schema_version: Literal[1] = 1
    runtime: Code
    sdk: Code
    stress: StressEvidence
    recovery: RecoveryEvidence
    pause_resume: PauseEvidence


def public_delivery_evidence(raw: dict) -> PublicEvidence:
    stress = raw["observations"]["stress"]
    recovery = raw["observations"]["recovery"]
    return PublicEvidence(
        runtime=raw["runtime"].removeprefix("restate-server "), sdk=raw["sdk"],
        stress=StressEvidence(**{k: stress[k] for k in ("completed_candidates", "eligible_decisions", "paper_positions",
                                                       "seconds", "candidates_per_second", "requests", "invariants")},
                              worker_peak_rss_kib=stress["sampled_peak_group_rss_kib"]["worker"],
                              runtime_peak_rss_kib=stress["sampled_peak_group_rss_kib"]["runtime"]),
        recovery=RecoveryEvidence(**{k: recovery[k] for k in ("completed_page_attempts", "completed_analysis_attempts",
                                                             "offline_unchanged", "replay_unchanged", "invariants",
                                                             "settlement_once", "synthetic_virtual_settlement_time")},
                                  fill_attempts=sorted(recovery["fill_attempts"].values()),
                                  fill_reconciliation_attempts=sorted(recovery["fill_reconciliation_attempts"].values())),
        pause_resume=PauseEvidence(**{k: raw["observations"]["pause_resume"][k] for k in PauseEvidence.model_fields}),
    )
