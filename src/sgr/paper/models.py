from __future__ import annotations
import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PaperPolicy(BaseModel):
    """Precommitted fictional USD. This policy never configures live execution."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: str = "fictional-fixed-v1"
    initial_capital: Decimal = Field(default=Decimal("1000"), gt=0)
    stake: Decimal = Field(default=Decimal("25"), gt=0)
    contract_cap: Decimal = Field(default=Decimal("50"), gt=0)
    event_cap: Decimal = Field(default=Decimal("100"), gt=0)
    correlated_cap: Decimal = Field(default=Decimal("150"), gt=0)
    total_cap: Decimal = Field(default=Decimal("400"), gt=0)
    loss_limit: Decimal = Field(default=Decimal("100"), gt=0)
    min_net_edge: Decimal = Field(default=Decimal(".03"), ge=0, lt=1)
    uncertainty_haircut: Decimal = Field(default=Decimal(".02"), ge=0, lt=1)
    depth_fraction: Decimal = Field(default=Decimal(".25"), gt=0, le=1)
    quote_max_age_seconds: int = Field(default=10, ge=1, le=60)
    decision_ttl_seconds: int = Field(default=30, ge=1, le=300)
    latency_ms: int = Field(default=500, ge=0, le=10000)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    @model_validator(mode="after")
    def coherent_caps(self):
        if not self.stake <= self.contract_cap <= self.event_cap <= self.correlated_cap <= self.total_cap <= self.initial_capital:
            raise ValueError("Paper exposure caps must be nested within fictional capital.")
        return self


class AnalysisDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    market_id: str
    condition_id: str
    event_id: str
    game_id: str | None = None
    kickoff_at: datetime | None = None
    asset_id: str | None = None
    groups: tuple[str, ...] = ()
    decision_at: datetime
    feature_cutoff_at: datetime | None = None
    expires_at: datetime
    eligible: bool
    reasons: tuple[str, ...]
    probability: Decimal | None = Field(default=None, ge=0, le=1)
    net_edge: Decimal | None = None
    uncertainty_cost: Decimal = Field(default=Decimal(".02"), ge=0, lt=1)
    limit_price: Decimal | None = Field(default=None, gt=0, lt=1)
    requested_notional: Decimal = Field(default=Decimal("0"), ge=0)
    forecast_id: str | None = None
    model_version: str = "unknown"
    calibration_version: str = "unknown"
    strategy_version: str = "nfl-cost-adjusted-v1"
    rule_version: str
    policy_fingerprint: str
    snapshot_ids: tuple[str, ...] = ()
    input_digest: str
    families: tuple[dict, ...] = ()

    @model_validator(mode="after")
    def executable_decisions_have_evidence(self):
        for name in ("decision_at", "expires_at"):
            if getattr(self, name).tzinfo is None:
                raise ValueError("Decision times must be aware.")
        if self.expires_at <= self.decision_at:
            raise ValueError("Decision expiry must follow analysis.")
        if self.eligible and self.feature_cutoff_at is not None and self.feature_cutoff_at > self.decision_at:
            raise ValueError("Model cutoff cannot follow decision time.")
        if self.eligible and (
            self.probability is None or self.asset_id is None or self.forecast_id is None
            or self.limit_price is None or self.requested_notional <= 0
            or self.net_edge is None or not self.groups or self.game_id is None or self.kickoff_at is None
        ):
            raise ValueError("Eligible analysis is missing executable lineage.")
        return self
