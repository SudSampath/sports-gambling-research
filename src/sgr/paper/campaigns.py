"""Durable application evidence/query catalog; Restate owns workflow execution."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sgr.connectors.polymarket import BudgetExhausted, RequestBudget
from sgr.paper.models import PaperPolicy
from sgr.research.contracts import ContractDefinition

TERMINAL = {"completed", "partial_provider_failure", "request_budget_exhausted", "deadline_reached", "paused_at_deadline", "failed"}


class CampaignSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    mode: Literal["public", "synthetic"] = "public"
    portfolio: str = Field(default="fictional-nfl", pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    policy: PaperPolicy = Field(default_factory=PaperPolicy)
    max_candidates: int = Field(default=500, ge=1, le=10000)
    max_pages: int = Field(default=5, ge=1, le=100)
    max_requests: int = Field(default=50, ge=1, le=1000)
    max_seconds: int = Field(default=120, ge=1, le=600)
    fanout: int = Field(default=4, ge=1, le=16)
    rechecks: int = Field(default=0, ge=0, le=2)
    recheck_seconds: int = Field(default=30, ge=1, le=60)
    nfl_only: bool = True
    definitions: tuple[ContractDefinition, ...] = ()
    test_delay_seconds: float = Field(default=0, ge=0, le=5)
    test_lose_fill_ack: bool = False

    @model_validator(mode="after")
    def synthetic_faults_only(self):
        if self.mode != "synthetic" and (self.test_delay_seconds or self.test_lose_fill_ack):
            raise ValueError("Fault injection only exists for labeled synthetic campaigns.")
        if self.mode == "synthetic" and not self.portfolio.startswith("synthetic-"):
            raise ValueError("Synthetic campaigns need a separate synthetic-* portfolio.")
        if self.mode == "public" and self.portfolio.startswith("synthetic-"):
            raise ValueError("Public campaigns cannot use a synthetic-* portfolio.")
        return self


class Catalog:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "campaigns.sqlite"
        with self.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS trade_requests (decision TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS campaigns(
                  id TEXT PRIMARY KEY, request TEXT, status TEXT, paused INTEGER DEFAULT 0,
                  pages INTEGER DEFAULT 0, cursor TEXT, requests INTEGER DEFAULT 0, started TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS analyses(
                  id TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS results(
                  campaign TEXT, candidate TEXT, analysis TEXT, payload TEXT,
                  PRIMARY KEY(campaign,candidate));
                CREATE TABLE IF NOT EXISTS inputs(
                  campaign TEXT, round INTEGER, payload TEXT, PRIMARY KEY(campaign,round));
                CREATE TABLE IF NOT EXISTS attempts(
                  operation TEXT PRIMARY KEY, n INTEGER);
                CREATE TABLE IF NOT EXISTS settlement_budgets(
                  id TEXT PRIMARY KEY, used INTEGER DEFAULT 0);
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def start(self, spec: CampaignSpec, started_at: datetime) -> dict:
        with self.connect() as c:
            old = c.execute("SELECT request FROM campaigns WHERE id=?", (spec.id,)).fetchone()
            if old and old[0] != spec.model_dump_json():
                raise ValueError("Campaign key reused with a different frozen policy or bounds.")
            c.execute("INSERT OR IGNORE INTO campaigns(id,request,status,started,detail) VALUES (?,?,?,?,?)",
                      (spec.id, spec.model_dump_json(), "queued", started_at.isoformat(), "{}"))
        return self.status(spec.id)

    def save_trade_request(self, decision: str, payload: dict):
        if payload["decision"]["id"] != decision:
            raise ValueError("Archived trade decision identity mismatch.")
        body = json.dumps(payload, sort_keys=True)
        with self.connect() as c:
            old = c.execute("SELECT payload FROM trade_requests WHERE decision=?", (decision,)).fetchone()
            if old and old[0] != body:
                raise ValueError("Archived trade request changed on replay.")
            c.execute("INSERT OR IGNORE INTO trade_requests VALUES (?,?)", (decision, body))
        return payload

    def trade_request(self, decision: str):
        with self.connect() as c:
            row = c.execute("SELECT payload FROM trade_requests WHERE decision=?", (decision,)).fetchone()
        if row is None:
            raise ValueError("Archived paper-trade request is absent.")
        return json.loads(row[0])

    def checkpoint(self, campaign: str, detail: dict) -> dict:
        with self.connect() as c:
            c.execute("UPDATE campaigns SET status=?, pages=?, cursor=?, detail=? WHERE id=?",
                      (detail["status"], detail.get("pages", 0), detail.get("cursor"), json.dumps(detail, sort_keys=True), campaign))
        return detail

    def control(self, campaign: str, paused: bool) -> dict:
        with self.connect() as c:
            c.execute("UPDATE campaigns SET paused=? WHERE id=?", (int(paused), campaign))
        return self.status(campaign)

    def status(self, campaign: str) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign,)).fetchone()
            if row is None:
                raise KeyError("Unknown campaign.")
            result = dict(row)
            result["detail"] = json.loads(result["detail"])
            result["spec"] = json.loads(result.pop("request"))
            result["completed_candidates"] = c.execute("SELECT count(*) FROM results WHERE campaign=?", (campaign,)).fetchone()[0]
            return result

    def claim_request(self, campaign: str):
        with self.connect() as c:
            row = c.execute("SELECT request, requests FROM campaigns WHERE id=?", (campaign,)).fetchone()
            maximum = json.loads(row["request"])["max_requests"]
            if row["requests"] >= maximum:
                raise BudgetExhausted("Campaign-wide persisted public request budget exhausted.")
            c.execute("UPDATE campaigns SET requests=requests+1 WHERE id=?", (campaign,))

    def attempt(self, operation: str):
        with self.connect() as c:
            c.execute("INSERT INTO attempts VALUES (?,1) ON CONFLICT(operation) DO UPDATE SET n=n+1", (operation,))

    def claim_settlement_request(self, operation: str):
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO settlement_budgets VALUES (?,0)", (operation,))
            used = c.execute("SELECT used FROM settlement_budgets WHERE id=?", (operation,)).fetchone()[0]
            if used >= 4:
                raise BudgetExhausted("Bounded settlement pass exhausted its independent request budget.")
            c.execute("UPDATE settlement_budgets SET used=used+1 WHERE id=?", (operation,))

    def input_digest(self, campaign: str, round_number: int):
        with self.connect() as c:
            row = c.execute("SELECT json_extract(payload,'$.digest') FROM inputs WHERE campaign=? AND round=?", (campaign, round_number)).fetchone()
            return row[0] if row else None

    def save_inputs(self, campaign: str, round_number: int, payload: dict) -> dict:
        body = json.dumps(payload, sort_keys=True)
        with self.connect() as c:
            old = c.execute("SELECT payload FROM inputs WHERE campaign=? AND round=?", (campaign, round_number)).fetchone()
            if old:
                return json.loads(old[0])
            c.execute("INSERT INTO inputs VALUES (?,?,?)", (campaign, round_number, body))
        return payload

    def inputs(self, campaign: str, round_number: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT payload FROM inputs WHERE campaign=? AND round=?", (campaign, round_number)).fetchone()
            return json.loads(row[0]) if row else None

    def save_analysis(self, result: dict) -> dict:
        key = result["decision"]["id"]
        body = json.dumps(result, sort_keys=True)
        with self.connect() as c:
            old = c.execute("SELECT payload FROM analyses WHERE id=?", (key,)).fetchone()
            if old and old[0] != body:
                raise ValueError("Analysis identity reused with changed evidence.")
            c.execute("INSERT OR IGNORE INTO analyses VALUES (?,?)", (key, body))
        return result

    def result(self, campaign: str, candidate: str, result: dict) -> dict:
        body = json.dumps(result, sort_keys=True)
        analysis = result.get("decision", {}).get("id")
        with self.connect() as c:
            old = c.execute("SELECT payload FROM results WHERE campaign=? AND candidate=?", (campaign, candidate)).fetchone()
            if old and old[0] != body:
                raise ValueError("Candidate result identity changed on replay.")
            c.execute("INSERT OR IGNORE INTO results VALUES (?,?,?,?)", (campaign, candidate, analysis, body))
        return result

    def analyzed_records(self, campaign: str) -> list[dict]:
        with self.connect() as c:
            return [json.loads(row[0]) for row in c.execute(
                "SELECT DISTINCT a.payload FROM analyses a JOIN results r ON r.analysis=a.id WHERE r.campaign=?",
                (campaign,))]

    def report(self, campaign: str) -> dict:
        status = self.status(campaign)
        with self.connect() as c:
            results = [json.loads(row[0]) for row in c.execute("SELECT payload FROM results WHERE campaign=? ORDER BY candidate", (campaign,))]
            attempts = {r["operation"]: r["n"] for r in c.execute("SELECT * FROM attempts")
                        if r["operation"].startswith(campaign+":")}
        from collections import Counter
        exclusions = Counter()
        ranked = []
        for result in results:
            decision = result.get("decision")
            if decision:
                if decision["eligible"]:
                    ranked.append(decision)
                else:
                    exclusions.update(decision["reasons"])
            elif result.get("error"):
                exclusions.update([result["error"]])
        return {"campaign": status, "data_label": "synthetic stress fixture" if status["spec"]["mode"] == "synthetic" else "public live screening",
                "coverage": {"completed_candidates": len(results), "eligible_decisions": len(ranked), "exclusions": dict(exclusions)},
                "rankings": sorted(ranked, key=lambda d: Decimal(d["net_edge"]), reverse=True),
                "operation_attempts": attempts, "results": results}


from decimal import Decimal


class CampaignBudget(RequestBudget):
    def __init__(self, catalog: Catalog, campaign: str):
        super().__init__(maximum=1000)
        self.catalog, self.campaign = catalog, campaign

    def claim(self):
        self.catalog.claim_request(self.campaign)
        self.used += 1


class SettlementBudget(RequestBudget):
    def __init__(self, catalog: Catalog, operation: str):
        super().__init__(maximum=4)
        self.catalog, self.operation = catalog, operation

    def claim(self):
        self.catalog.claim_settlement_request(self.operation)
        self.used += 1
