import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest

from paper_cases import setup_paper
from paper_fixtures import NOW, row, book_payload
from sgr.connectors.polymarket import BudgetExhausted, PolymarketConnector
from sgr.paper.campaigns import CampaignSpec, Catalog, SettlementBudget
from sgr.paper import workflows
from sgr.research.storage import ResearchStore


def test_campaign_spec_rejects_invalid_interpretations():
    with pytest.raises(ValueError):
        CampaignSpec(id="bad", definitions=({"condition_id": "missing-everything"},))


def test_settlement_budget_is_bounded_independently_of_exhausted_discovery(tmp_path):
    catalog = Catalog(tmp_path)
    catalog.start(CampaignSpec(id="bounded", max_requests=1), NOW)
    catalog.claim_request("bounded")
    with pytest.raises(BudgetExhausted):
        catalog.claim_request("bounded")
    budget = SettlementBudget(catalog, "bounded:position:pass-1")
    for _ in range(4):
        budget.claim()
    with pytest.raises(BudgetExhausted):
        budget.claim()
    assert catalog.status("bounded")["requests"] == 1


def test_public_workflow_operations_forecast_fill_and_poll_final_outcome(tmp_path, monkeypatch):
    """Production operations over synthetic public HTTP, never a live-trade claim."""
    c = setup_paper(tmp_path)
    monkeypatch.setattr(workflows, "ROOT", tmp_path)
    workflows._input_cache.clear()
    spec = CampaignSpec(id="public-http", definitions=(c["definition"],))
    catalog = Catalog(tmp_path)
    catalog.start(spec, NOW)
    monkeypatch.setattr(workflows, "_catalog", catalog)
    store = workflows.evidence(spec)
    store.write([*c["history"], c["inputs"].load("game", c["definition"].game_id)])
    workflows.freeze_inputs(spec, 0, NOW)
    assert c["definition"].game_id in [g.id for g in workflows.load_inputs(spec, 0).load_all("game")]
    final = False
    def provider(request):
        assert request.method == "GET"
        if request.url.path == "/markets/1":
            payload = row()
            if final:
                payload.update(closed=True, acceptingOrders=False, umaResolutionStatus="resolved", outcomePrices='["1", "0"]')
        elif request.url.path == "/book":
            payload = book_payload(request.url.params["token_id"])
        elif request.url.path == "/fee-rate":
            payload = {"base_fee": 500}
        elif request.url.path == "/tick-size":
            payload = {"minimum_tick_size": ".01"}
        else:
            raise AssertionError("Unexpected provider path")
        return httpx.Response(200, json=payload)
    @asynccontextmanager
    async def connector(spec, settlement_operation=None):
        async with PolymarketConnector(store, interval=0,
            clock=lambda: NOW+timedelta(days=2) if final else NOW,
            transport=httpx.MockTransport(provider)) as client:
            yield client
    monkeypatch.setattr(workflows, "connector", connector)
    snapshots = asyncio.run(workflows.fetch_books(spec, c["market"], NOW))
    request = {"key": "0:condition-1", "definitions": [c["definition"].model_dump(mode="json")]}
    analysis = asyncio.run(workflows.compute_analysis(spec, 0, request, snapshots, NOW))
    assert analysis["decision"]["eligible"], analysis["decision"]["reasons"]
    action = {"spec": spec.model_dump(mode="json"), "decision": analysis["decision"], "now": NOW.isoformat()}
    assert workflows.portfolio_effect(spec.portfolio, {**action, "action": "reserve"})["status"] == "reserved"
    selected = next(b for b in snapshots["books"] if b["asset_id"] == analysis["decision"]["asset_id"])
    fill = workflows.portfolio_effect(spec.portfolio, {**action, "action": "execute", "book": selected})
    assert fill["status"] in ("filled", "partial")
    final = True
    from sgr.paper.models import AnalysisDecision
    decision = AnalysisDecision.model_validate(analysis["decision"])
    outcome = asyncio.run(workflows.poll_settlement(spec, c["market"], decision, "pass-1"))
    settled = workflows.portfolio_effect(spec.portfolio, {"spec": spec.model_dump(mode="json"), "action": "settle",
        "decision_id": decision.id, "payout": outcome["payout"], "now": outcome["observed_at"],
        "rule_version": decision.rule_version, "source": outcome["source"]})
    assert settled["status"] == "settled"
    catalog.result(spec.id, request["key"], {"decision": analysis["decision"]})
    assert workflows.project_evidence(spec) == {"forecasts": 1}
    assert len(store.load_all("forecast")) == 1


def test_settlement_recovers_archived_request_after_workflow_state_is_gone(tmp_path, monkeypatch):
    from sgr.paper.ledger import Ledger
    c = setup_paper(tmp_path)
    spec = CampaignSpec(id="retained", mode="synthetic", portfolio="synthetic-retained")
    monkeypatch.setattr(workflows, "ROOT", tmp_path)
    catalog = Catalog(tmp_path)
    catalog.start(spec, NOW)
    monkeypatch.setattr(workflows, "_catalog", catalog)
    d = c["decision"]
    ledger = Ledger(tmp_path, spec.portfolio, spec.policy)
    ledger.reserve(d, now=NOW)
    ledger.execute(d, c["book"], now=NOW)
    archived = {"spec": spec.model_dump(mode="json"), "decision": d.model_dump(mode="json"),
                "market": c["market"].model_dump(mode="json"), "definitions": []}
    catalog.save_trade_request(d.id, archived)
    class MissingWorkflowState:
        async def get(self, name):
            return None
        def key(self):
            return d.id
        async def time(self):
            return NOW.timestamp()
        async def run_typed(self, name, function, options, *args):
            return function(*args)
        async def object_call(self, function, key, arg):
            return workflows.portfolio_effect(key, arg)
    outcome = asyncio.run(workflows.settlement(MissingWorkflowState(),
        {"settled_at": (NOW+timedelta(days=2)).isoformat()}))
    assert outcome["status"] == "settled"
    duplicate = asyncio.run(workflows.settlement(MissingWorkflowState(), {}))
    assert duplicate == outcome and len(ledger.reconcile()["settlements"]) == 1


def test_replay_after_workflow_retention_preserves_terminal_catalog(tmp_path, monkeypatch):
    import restate
    spec = CampaignSpec(id="done")
    catalog = Catalog(tmp_path)
    catalog.start(spec, NOW)
    catalog.checkpoint(spec.id, {"status": "completed", "pages": 2, "unique_markets": 100})
    monkeypatch.setattr(workflows, "_catalog", catalog)
    before = catalog.report(spec.id)
    class FreshWorkflowState:
        def key(self):
            return spec.id
        async def time(self):
            return NOW.timestamp()
        def set(self, key, value):
            pass
        async def run_typed(self, name, function, options, *args):
            return function(*args)
    assert asyncio.run(workflows.run(FreshWorkflowState(), spec.model_dump(mode="json"))) == before["campaign"]["detail"]
    changed = spec.model_copy(update={"max_candidates": 200})
    with pytest.raises(restate.TerminalError):
        asyncio.run(workflows.run(FreshWorkflowState(), changed.model_dump(mode="json")))
    assert catalog.report(spec.id) == before
