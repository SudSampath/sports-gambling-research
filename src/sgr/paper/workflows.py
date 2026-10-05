"""Restate owns bounded campaigns, analysis and paper-trade lifecycles."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import time

import restate

from sgr.connectors.base import APIRequestError
from sgr.connectors.polymarket import PolymarketConnector, public_array
from sgr.paper.campaigns import CampaignBudget, SettlementBudget, CampaignSpec, Catalog, TERMINAL
from sgr.paper.ledger import Ledger
from sgr.paper.models import AnalysisDecision
from sgr.paper import synthetic
from sgr.research.contracts import ContractDefinition
from sgr.research.opportunities import Analyzer, PointInTimeInputs
from sgr.research.schemas import PolymarketMarket, TokenBookSnapshot, Forecast, load_canonical_record, stable_record_id
from sgr.research.storage import ResearchStore

ROOT = Path(os.environ.get("SGR_PAPER_ROOT", "data/polymarket")).resolve()
RETRIES = restate.RunOptions(max_attempts=3, initial_retry_interval=timedelta(milliseconds=300), max_retry_interval=timedelta(seconds=1))
OPTIONS = dict(inactivity_timeout=timedelta(seconds=45), abort_timeout=timedelta(minutes=12), journal_retention=timedelta(days=30))
campaign = restate.Workflow("ScanCampaign", **OPTIONS)
candidate_analysis = restate.Workflow("CandidateAnalysis", **OPTIONS)
paper_trade = restate.Workflow("PaperTrade", **OPTIONS)
portfolio = restate.VirtualObject("PaperPortfolio", **OPTIONS)
_http_lock = asyncio.Lock()
_http_last = 0.0
_catalog = None
_input_cache = {}


def catalog():
    global _catalog
    if _catalog is None:
        _catalog = Catalog(ROOT)
    return _catalog


def evidence(spec):
    return ResearchStore(ROOT / "synthetic-evidence" / spec.id if spec.mode == "synthetic" else ROOT / "research")


class WorkerConnector(PolymarketConnector):
    """One process-wide HTTP admission gate, plus persisted campaign budget."""
    async def _get(self, *args, **kwargs):
        global _http_last
        async with _http_lock:
            wait = .2 - (time.monotonic()-_http_last)
            if wait > 0:
                await asyncio.sleep(wait)
            _http_last = time.monotonic()
            return await super()._get(*args, **kwargs)


def connector(spec, settlement_operation=None):
    budget = SettlementBudget(catalog(), settlement_operation) if settlement_operation else CampaignBudget(catalog(), spec.id)
    return WorkerConnector(evidence(spec), budget=budget)


def load_inputs(spec, round_number):
    key = (spec.id, round_number)
    cached = _input_cache.get(key)
    if cached is not None:
        if cached.digest != catalog().input_digest(spec.id, round_number):
            raise ValueError("Frozen manifest digest changed.")
        return cached
    manifest = catalog().inputs(spec.id, round_number)
    if manifest is None:
        raise ValueError("Frozen campaign inputs are missing.")
    records = {k: [load_canonical_record(r) for r in batch] for k, batch in manifest["records"].items()}
    inputs = PointInTimeInputs(records, datetime.fromisoformat(manifest["cutoff"]))
    if inputs.digest != manifest["digest"]:
        raise ValueError("Frozen manifest content/digest mismatch.")
    if len(_input_cache) >= 16:
        _input_cache.pop(next(iter(_input_cache)))
    _input_cache[key] = inputs
    return inputs


def freeze_inputs(spec, round_number, cutoff):
    old = catalog().inputs(spec.id, round_number)
    if old:
        return {"digest": old["digest"], "cutoff": old["cutoff"]}
    store = evidence(spec)
    if spec.mode == "synthetic" and round_number == 0:
        synthetic.seed(store, spec.id, cutoff)
    inputs = PointInTimeInputs.from_store(store, cutoff)
    payload = {"cutoff": cutoff.isoformat(), "digest": inputs.digest,
               "records": {k: [r.model_dump(mode="json") for r in records] for k, records in inputs.records.items()}}
    catalog().save_inputs(spec.id, round_number, payload)
    return {"digest": inputs.digest, "cutoff": cutoff.isoformat()}


async def discover(spec, page_number, cursor, tag_id, limit, cutoff):
    catalog().attempt(f"{spec.id}:page:{page_number}")
    if spec.mode == "synthetic":
        inputs = load_inputs(spec, 0)
        target = next(g for g in inputs.load_all("game") if not g.completed)
        source = target.source_snapshots[0]
        start = page_number * 100
        pairs = [synthetic.candidate(i, target, source) for i in range(start, min(start+limit, spec.max_candidates))]
        return {"markets": [m.model_dump(mode="json") for m, _ in pairs],
                "definitions": [d.model_dump(mode="json") for _, d in pairs],
                "next_cursor": str(start+limit) if start+limit < spec.max_candidates else None,
                "raw_count": len(pairs), "exclusions": []}
    async with connector(spec) as provider:
        page = await provider.page(cursor, limit=limit, tag_id=tag_id)
    return {"markets": [m.model_dump(mode="json") for m in page.markets],
            "definitions": [d.model_dump(mode="json") for d in spec.definitions], "next_cursor": page.next_cursor,
            "raw_count": page.raw_count, "exclusions": list(page.exclusions)}


def screen_page(spec, round_number, page, now):
    inputs = load_inputs(spec, round_number)
    definitions = [ContractDefinition.model_validate(d) for d in page["definitions"]]
    analyzer = Analyzer(inputs, spec.policy, definitions)
    promising = []
    for item in page["markets"]:
        market = PolymarketMarket.model_validate(item)
        key = f"{round_number}:{market.condition_id}"
        match = analyzer.screen(market, now)
        if match.eligible:
            promising.append({"market": item, "definitions": [match.definition.model_dump(mode="json")], "key": key})
        else:
            decision, _ = analyzer.analyze(market, [], now=now)
            catalog().save_analysis({"decision": decision.model_dump(mode="json"), "forecast": None, "market": item, "books": []})
            catalog().result(spec.id, key, {"decision": decision.model_dump(mode="json"), "trade": None})
    return promising


async def fetch_books(spec, market, now, *, refresh=True):
    if spec.mode == "synthetic":
        inputs = load_inputs(spec, 0)
        source = inputs.load_all("game")[0].source_snapshots[0]
        return {"market": market.model_dump(mode="json"), "books": [b.model_dump(mode="json") for b in synthetic.books(market, now, source)]}
    async with connector(spec) as provider:
        if refresh:
            market, _, _ = await provider.refresh(market)
        books = await asyncio.gather(*(provider.book(market, a) for a in market.asset_ids))
    return {"market": market.model_dump(mode="json"), "books": [b.model_dump(mode="json") for b in books]}


async def compute_analysis(spec, round_number, request, snapshots, now):
    operation = spec.id + ":analysis:" + request["key"]
    catalog().attempt(operation)
    if spec.test_delay_seconds:
        await asyncio.sleep(spec.test_delay_seconds)
    inputs = load_inputs(spec, round_number)
    analyzer = Analyzer(inputs, spec.policy, [ContractDefinition.model_validate(d) for d in request["definitions"]])
    market = PolymarketMarket.model_validate(snapshots["market"])
    books = [TokenBookSnapshot.model_validate(b) for b in snapshots["books"]]
    decision, forecast = await asyncio.to_thread(analyzer.analyze, market, books, now=now)
    result = {"decision": decision.model_dump(mode="json"), "forecast": forecast.model_dump(mode="json") if forecast else None,
              "definitions": request["definitions"], "round": round_number,
              "market": snapshots["market"], "books": snapshots["books"]}
    return catalog().save_analysis(result)


def portfolio_effect(key, request):
    spec = CampaignSpec.model_validate(request["spec"])
    if key != spec.portfolio:
        raise ValueError("Portfolio object key/policy mismatch.")
    ledger = Ledger(ROOT, spec.portfolio, spec.policy)
    action = request["action"]
    now = datetime.fromisoformat(request["now"])
    if action in ("reserve", "execute"):
        campaign_state = catalog().status(spec.id)
        if CampaignSpec.model_validate(campaign_state["spec"]) != spec:
            raise ValueError("Campaign policy and bounds changed after initialization.")
        decision = AnalysisDecision.model_validate(request["decision"])
        if action == "reserve":
            existing = ledger.existing_reservation(decision)
            if existing:
                return existing
        if action == "execute":
            catalog().attempt(spec.id+":reconcile-fill:"+decision.id)
            existing = ledger.existing_fill(decision.id)
            if existing:
                return ledger.execute(decision, TokenBookSnapshot.model_validate(request["book"]), now=now)
        expired_campaign = now >= datetime.fromisoformat(campaign_state["started"])+timedelta(seconds=spec.max_seconds)
        if campaign_state["paused"] or expired_campaign or campaign_state["status"] in TERMINAL:
            if action == "execute":
                return ledger.cancel(decision.id)
            return {"status": "rejected", "rejection_reason": "campaign_paused_or_expired"}
        if action == "reserve":
            return ledger.reserve(decision, now=now)
        catalog().attempt(spec.id+":fill:"+decision.id)
        # Reconcile a committed fill BEFORE evaluating freshness or retrying.
        existing = ledger.existing_fill(decision.id)
        if existing:
            return existing
        return ledger.execute(decision, TokenBookSnapshot.model_validate(request["book"]), now=now,
                              lose_ack=spec.test_lose_fill_ack, latency_already_elapsed=True)
    if action == "cancel":
        return ledger.cancel(request["decision_id"], expired=True)
    if action == "settle":
        return ledger.settle(request["decision_id"], payout=Decimal(request["payout"]), settled_at=now,
                             rule_version=request["rule_version"], source=request["source"], disputed=request.get("disputed", False))
    if action == "settlement_status":
        return {"settlement": ledger.settlement_for(request["decision_id"])}
    if action == "pause":
        ledger.pause(request.get("reason", "operator"))
    if action == "resume":
        ledger.resume()
    return ledger.reconcile()


@portfolio.handler()
async def apply(ctx: restate.ObjectContext, request: dict) -> dict:
    return await ctx.run_typed("ledger:"+request["action"], portfolio_effect, RETRIES, ctx.key(), request)


async def gate(ctx, spec, action, now, **kwargs):
    return await ctx.object_call(apply, key=spec.portfolio, arg={"spec": spec.model_dump(mode="json"), "action": action, "now": now.isoformat(), **kwargs})


@candidate_analysis.main()
async def analyze(ctx: restate.WorkflowContext, request: dict) -> dict:
    spec = CampaignSpec.model_validate(request["spec"])
    market = PolymarketMarket.model_validate(request["market"])
    now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
    try:
        snapshots = await ctx.run_typed("supported_books", fetch_books, RETRIES, spec, market, now)
        now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
        result = await ctx.run_typed("expensive_analysis", compute_analysis, RETRIES, spec, request["round"], request, snapshots, now)
        decision = result["decision"]
        if decision["eligible"]:
            trade_request = {"spec": request["spec"], "decision": decision, "market": result["market"], "definitions": request["definitions"]}
            await ctx.run_typed("archive_trade_request", catalog().save_trade_request, RETRIES, decision["id"], trade_request)
            ctx.workflow_send(trade, key=decision["id"], arg=trade_request)
            result = {"decision": decision, "trade": decision["id"], "trade_request": trade_request}
        else:
            result = {"decision": decision, "trade": None}
    except restate.TerminalError as error:
        result = {"error": "provider_or_analysis_failed", "trade": None}
    return await ctx.run_typed("save_candidate", catalog().result, RETRIES, spec.id, request["key"], result)


@paper_trade.main(workflow_retention=timedelta(days=30))
async def trade(ctx: restate.WorkflowContext, request: dict) -> dict:
    spec = CampaignSpec.model_validate(request["spec"])
    decision = AnalysisDecision.model_validate(request["decision"])
    market = PolymarketMarket.model_validate(request["market"])
    if ctx.key() != decision.id:
        raise restate.TerminalError("Paper-trade key differs from decision.")
    ctx.set("request", request)
    await ctx.run_typed("archive_trade_request", catalog().save_trade_request, RETRIES, decision.id, request)
    now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
    ctx.set("status", {"phase": "reserving"})
    reservation = await gate(ctx, spec, "reserve", now, decision=request["decision"])
    if reservation["status"] != "reserved":
        ctx.set("status", {"phase": reservation["status"]})
        return {"status": reservation["status"], "reservation": reservation}
    ctx.set("status", {"phase": "simulated_submission"})
    try:
        await ctx.sleep(timedelta(milliseconds=spec.policy.latency_ms), name="simulated-submission-latency")
        now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
        snapshots = await ctx.run_typed("revalidate_quote", fetch_books, RETRIES, spec, market, now)
        current = PolymarketMarket.model_validate(snapshots["market"])
        fields = ("rule_version", "asset_ids", "outcomes", "deadline_at", "game_start_at", "market_type", "negative_risk", "resolution_source")
        now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
        if (any(getattr(current, field) != getattr(market, field) for field in fields)
                or not current.accepting_orders or not current.active or current.game_start_at <= now):
            result = await gate(ctx, spec, "cancel", now, decision_id=decision.id)
        else:
            book = next(b for b in snapshots["books"] if b["asset_id"] == decision.asset_id)
            result = await gate(ctx, spec, "execute", now, decision=request["decision"], book=book)
    except restate.TerminalError:
        result = await gate(ctx, spec, "cancel", now, decision_id=decision.id)
        state = await ctx.run_typed("failure_budget_state", catalog().status, RETRIES, spec.id)
        if state["requests"] < spec.max_requests:
            await gate(ctx, spec, "pause", now, reason="quote_or_reconciliation_failure")
    ctx.set("status", {"phase": result["status"], "result": result})
    if result["status"] == "partial":
        current_time = datetime.fromtimestamp(await ctx.time(), timezone.utc)
        await ctx.sleep(timedelta(seconds=max(0, (decision.expires_at-current_time).total_seconds())), name="paper-order-expiry")
        result = await gate(ctx, spec, "cancel", decision.expires_at, decision_id=decision.id)
    report = await gate(ctx, spec, "reconcile", datetime.fromtimestamp(await ctx.time(), timezone.utc))
    ctx.set("status", {"phase": result["status"], "reconciled": report["ok"]})
    return {"status": result["status"], "reconciled": report["ok"], "decision_id": decision.id}


@paper_trade.handler()
async def status(ctx: restate.WorkflowSharedContext, _: dict) -> dict:
    return await ctx.get("status") or {"phase": "queued"}


async def poll_settlement(spec, market, decision, operation):
    async with connector(spec, operation) as provider:
        current, payload, source = await provider.refresh(market)
    if current.rule_version != decision.rule_version or current.asset_ids != market.asset_ids:
        raise ValueError("Settlement rules or asset identities changed.")
    if not payload.get("closed") or payload.get("umaResolutionStatus") != "resolved":
        return {"status": "disputed" if payload.get("umaResolutionStatus") == "disputed" else "pending",
                "source": {"url": source.source_url, "sha256": source.sha256, "path": source.path, "condition_id": current.condition_id},
                "observed_at": source.retrieved_at.isoformat()}
    values = json.loads(payload["outcomePrices"]) if isinstance(payload["outcomePrices"], str) else payload["outcomePrices"]
    payouts = [Decimal(x) for x in values]
    if len(payouts) != 2 or sum(payouts) != 1 or any(x not in (Decimal(0), Decimal(".5"), Decimal(1)) for x in payouts):
        raise ValueError("Final public payout is ambiguous.")
    return {"status": "resolved", "payout": str(payouts[current.asset_ids.index(decision.asset_id)]),
            "source": {"url": source.source_url, "sha256": source.sha256, "path": source.path, "condition_id": current.condition_id},
            "observed_at": source.retrieved_at.isoformat()}


@paper_trade.handler()
async def settlement(ctx: restate.WorkflowSharedContext, request: dict) -> dict:
    original = await ctx.get("request")
    if original is None:
        original = await ctx.run_typed("recover_archived_trade", catalog().trade_request, RETRIES, ctx.key())
    spec = CampaignSpec.model_validate(original["spec"])
    decision = AnalysisDecision.model_validate(original["decision"])
    if ctx.key() != decision.id:
        raise restate.TerminalError("Settlement key mismatch.")
    existing = await gate(ctx, spec, "settlement_status", datetime.fromtimestamp(await ctx.time(), timezone.utc), decision_id=decision.id)
    if existing["settlement"]:
        return existing["settlement"]
    if spec.mode == "synthetic":
        if not request.get("settled_at"):
            raise restate.TerminalError("Synthetic settlement needs an explicit labeled timestamp.", 400)
        result = {"status": "resolved", "payout": "1", "source": {"url": "https://example.org/sgr/synthetic-final", "sha256": "1"*64, "condition_id": decision.condition_id},
                  "observed_at": request["settled_at"]}
    else:
        operation = decision.id + ":" + request.get("settlement_pass", "single-pass")
        try:
            result = await ctx.run_typed("public_settlement", poll_settlement, RETRIES, spec, PolymarketMarket.model_validate(original["market"]), decision, operation)
        except restate.TerminalError:
            return {"status": "pending_data_unavailable"}
    if result["status"] not in ("resolved", "disputed"):
        return result
    return await gate(ctx, spec, "settle", datetime.fromisoformat(result["observed_at"]), decision_id=decision.id,
        payout=result.get("payout", "0"), rule_version=decision.rule_version,
        source=result["source"], disputed=result["status"] == "disputed")


async def control_point(ctx, spec, deadline, name):
    now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
    state = await ctx.run_typed(name, catalog().status, RETRIES, spec.id)
    while state["paused"] and now < deadline:
        await ctx.sleep(timedelta(seconds=1), name=name+":pause")
        now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
        state = await ctx.run_typed(name+":resume", catalog().status, RETRIES, spec.id)
    if now >= deadline:
        return "paused_at_deadline" if state["paused"] else "deadline_reached"
    return None


def project_evidence(spec):
    """Batch canonical forecasts into the existing DuckDB research foundation."""
    forecasts = [Forecast.model_validate(r["forecast"]) for r in catalog().analyzed_records(spec.id)
                 if r.get("forecast")]
    if forecasts:
        evidence(spec).write(forecasts)
    return {"forecasts": len(forecasts)}


async def run_campaign(ctx: restate.WorkflowContext, payload: dict) -> dict:
    spec = CampaignSpec.model_validate(payload)
    if ctx.key() != spec.id:
        raise restate.TerminalError("Campaign key differs from fixed specification.", 400)
    started = datetime.fromtimestamp(await ctx.time(), timezone.utc)
    initial = await ctx.run_typed("open_campaign", catalog().start, RETRIES, spec, started)
    if initial["status"] in TERMINAL:
        ctx.set("status", initial["detail"])
        return initial["detail"]
    await gate(ctx, spec, "reconcile", started)
    deadline = datetime.fromisoformat(initial["started"]) + timedelta(seconds=spec.max_seconds)
    detail = {"status": "running", "pages": 0, "cursor": None, "discovered": 0, "normalization_exclusions": [], "analyzed": 0}
    ctx.set("status", detail)
    await ctx.run_typed("freeze:0", freeze_inputs, RETRIES, spec, 0, started)
    tag_id = None
    if spec.mode == "public" and spec.nfl_only:
        async def get_tag():
            async with connector(spec) as provider:
                return await provider.nfl_tag()
        try:
            tag_id = await ctx.run_typed("nfl_tag", get_tag, RETRIES)
        except restate.TerminalError:
            detail["status"] = "partial_provider_failure"
    seen, trades, promising_for_rechecks = set(), {}, []
    for page_number in range(spec.max_pages):
        stopped = await control_point(ctx, spec, deadline, f"page-bound:{page_number}")
        if stopped:
            detail["status"] = stopped
            break
        now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
        if now >= deadline or detail["status"] == "partial_provider_failure" or detail["discovered"] >= spec.max_candidates:
            break
        try:
            page = await ctx.run_typed(f"page:{page_number}", discover, RETRIES, spec, page_number, detail["cursor"], tag_id,
                                       min(100, spec.max_candidates-detail["discovered"]), started)
        except restate.TerminalError:
            detail["status"] = "partial_provider_failure"
            break
        unique = []
        for item in page["markets"]:
            if item["condition_id"] not in seen:
                seen.add(item["condition_id"])
                unique.append(item)
        page["markets"] = unique
        now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
        supported = await ctx.run_typed(f"screen:{page_number}", screen_page, RETRIES, spec, 0, page, now)
        for offset in range(0, len(supported), spec.fanout):
            stopped = await control_point(ctx, spec, deadline, f"batch-bound:{page_number}:{offset}")
            if stopped:
                detail["status"] = stopped
                break
            batch = supported[offset:offset+spec.fanout]
            requests = [{**item, "spec": payload, "round": 0} for item in batch]
            outputs = await restate.gather(*[
                ctx.workflow_call(analyze, key=stable_record_id("candidate", spec.id, item["key"], item["market"]["id"]), arg=item)
                for item in requests])
            outputs = [await future for future in outputs]
            for item, output in zip(requests, outputs):
                if output.get("trade"):
                    trades[output["trade"]] = output["trade_request"]
                promising_for_rechecks.append(item)
            detail["analyzed"] += len(batch)
        detail["discovered"] += len(unique)
        detail["normalization_exclusions"].extend(page["exclusions"])
        detail.update(pages=page_number+1, cursor=page["next_cursor"], unique_markets=len(seen))
        ctx.set("status", detail)
        await ctx.run_typed(f"checkpoint:{page_number}", catalog().checkpoint, RETRIES, spec.id, detail)
        if page["next_cursor"] is None or detail["status"] in TERMINAL:
            break
    for round_number in range(1, spec.rechecks+1):
        if detail["status"] in TERMINAL:
            break
        now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
        if not promising_for_rechecks or now + timedelta(seconds=spec.recheck_seconds) >= deadline:
            break
        await ctx.sleep(timedelta(seconds=spec.recheck_seconds), name=f"campaign-recheck:{round_number}")
        now = datetime.fromtimestamp(await ctx.time(), timezone.utc)
        stopped = await control_point(ctx, spec, deadline, f"recheck-bound:{round_number}")
        if stopped:
            detail["status"] = stopped
            break
        await ctx.run_typed(f"freeze:{round_number}", freeze_inputs, RETRIES, spec, round_number, now)
        remaining = max(0, spec.max_candidates-detail["discovered"])
        selected = promising_for_rechecks[:min(128, remaining)]
        for offset in range(0, len(selected), spec.fanout):
            stopped = await control_point(ctx, spec, deadline, f"recheck-batch:{round_number}:{offset}")
            if stopped:
                detail["status"] = stopped
                break
            batch = [{**r, "round": round_number, "key": f"{round_number}:"+r["market"]["condition_id"]} for r in selected[offset:offset+spec.fanout]]
            outputs = await restate.gather(*[ctx.workflow_call(analyze, key=stable_record_id("candidate", spec.id, r["key"], r["market"]["id"]), arg=r) for r in batch])
            outputs = [await future for future in outputs]
            for output in outputs:
                if output.get("trade"):
                    trades[output["trade"]] = output["trade_request"]
            detail["discovered"] += len(batch)
    for offset in range(0, len(trades), spec.fanout):
        batch = list(trades.items())[offset:offset+spec.fanout]
        await restate.gather(*[ctx.workflow_call(trade, key=key, arg=request) for key, request in batch])
    report = await gate(ctx, spec, "reconcile", datetime.fromtimestamp(await ctx.time(), timezone.utc))
    detail["status"] = "completed" if detail["status"] == "running" else detail["status"]
    state = await ctx.run_typed("final_budget_state", catalog().status, RETRIES, spec.id)
    if detail["status"] == "partial_provider_failure" and state["requests"] >= spec.max_requests:
        detail["status"] = "request_budget_exhausted"
    detail["canonical_projection"] = await ctx.run_typed("project_forecasts", project_evidence, RETRIES, spec)
    detail["reconciled"] = report["ok"]
    detail["finished_at"] = datetime.fromtimestamp(await ctx.time(), timezone.utc).isoformat()
    ctx.set("status", detail)
    return await ctx.run_typed("final_checkpoint", catalog().checkpoint, RETRIES, spec.id, detail)


@campaign.main()
async def run(ctx: restate.WorkflowContext, payload: dict) -> dict:
    try:
        spec = CampaignSpec.model_validate(payload)
    except ValueError as error:
        raise restate.TerminalError("Invalid campaign specification: "+str(error), 400) from error
    if ctx.key() != spec.id:
        raise restate.TerminalError("Campaign key differs from fixed specification.", 400)
    try:
        return await run_campaign(ctx, payload)
    except (restate.TerminalError, ValueError) as error:
        state = await ctx.run_typed("failed_campaign_state", catalog().status, RETRIES, spec.id)
        if CampaignSpec.model_validate(state["spec"]) != spec or state["status"] in TERMINAL:
            raise restate.TerminalError("Campaign identity conflict; stored terminal evidence preserved.", 409) from error
        detail = {**state["detail"], "status": "failed", "error": str(error),
                  "completed_candidates": state["completed_candidates"]}
        await ctx.run_typed("failed_checkpoint", catalog().checkpoint, RETRIES, spec.id, detail)
        ctx.set("status", detail)
        return detail


@campaign.handler()
async def campaign_status(ctx: restate.WorkflowSharedContext, _: dict) -> dict:
    return await ctx.get("status") or {"status": "queued"}


app = restate.app([campaign, candidate_analysis, paper_trade, portfolio])
