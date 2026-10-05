from __future__ import annotations
from collections import Counter
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import time
import uuid

import httpx
import typer

from sgr.paper.campaigns import CampaignSpec, Catalog, TERMINAL
from sgr.paper.ledger import Ledger

app = typer.Typer(help="Bounded public research and fictional paper campaigns.")
INGRESS = "http://127.0.0.1:8080"


@app.command()
def feed(market_id: str, seconds: int = 20):
    """Archive selected public book updates outside Restate for at most 30 seconds."""
    import asyncio
    from sgr.connectors.polymarket import PolymarketConnector, RequestBudget
    from sgr.connectors.polymarket_feed import BookFeed
    from sgr.research.storage import ResearchStore
    if not 1 <= seconds <= 30:
        raise typer.BadParameter("Feed duration must be 1–30 seconds.")
    store = ResearchStore(root()/"research")
    matches = [m for m in store.load_all("polymarket_market") if m.id == market_id]
    if len(matches) != 1:
        raise typer.BadParameter("Choose one canonical market ID from a public campaign report.")
    async def collect():
        async with PolymarketConnector(store, budget=RequestBudget(maximum=10)) as provider:
            market, _, _ = await provider.refresh(matches[0])
            if not market.active or not market.accepting_orders or len(market.asset_ids) > 2:
                raise typer.BadParameter("Select an active binary contract.")
            books = [await provider.book(market, asset) for asset in market.asset_ids]
            return await BookFeed(books).collect(store, seconds=seconds)
    typer.echo(json.dumps(asyncio.run(collect())))


def root():
    return Path(os.environ.get("SGR_PAPER_ROOT", "data/polymarket")).resolve()


def submit(spec):
    try:
        old = Catalog(root()).status(spec.id)
    except KeyError:
        old = None
    if old and CampaignSpec.model_validate(old["spec"]) != spec:
        raise typer.BadParameter("Campaign ID already has a different frozen specification.")
    if old and old["status"] in TERMINAL:
        return {"id": spec.id, "status": old["status"], "replay": "existing terminal checkpoint"}
    response = httpx.post(f"{INGRESS}/ScanCampaign/{spec.id}/run/send", json=spec.model_dump(mode="json"), timeout=15)
    if response.status_code != 409:
        response.raise_for_status()
    return {"id": spec.id, "submission_status": response.status_code}


def wait_for(spec):
    until = time.monotonic()+spec.max_seconds+60
    while time.monotonic() < until:
        try:
            status = Catalog(root()).status(spec.id)
            if status["status"] in TERMINAL:
                return status
        except KeyError:
            pass
        time.sleep(.2)
    raise typer.Exit(2)


@app.command()
def start(
    spec_file: Path | None = typer.Option(None, "--spec"),
    campaign_id: str | None = typer.Option(None, "--id"),
    synthetic: bool = typer.Option(False, "--synthetic"),
    candidates: int = 500, pages: int = 5, requests: int = 50, seconds: int = 120,
    fanout: int = 4, rechecks: int = 0, wait: bool = False,
):
    """Freeze a bounded policy and autonomously evaluate eligible opportunities."""
    if spec_file:
        spec = CampaignSpec.model_validate_json(spec_file.read_text())
    else:
        name = campaign_id or "campaign-"+uuid.uuid4().hex[:12]
        if synthetic and len(name) > 54:
            raise typer.BadParameter("Synthetic campaign IDs must be at most 54 characters.")
        spec = CampaignSpec(id=name, mode="synthetic" if synthetic else "public",
                            portfolio="synthetic-"+name if synthetic else "fictional-nfl",
                            max_candidates=candidates, max_pages=pages, max_requests=requests,
                            max_seconds=seconds, fanout=fanout, rechecks=rechecks)
    typer.echo(json.dumps(submit(spec)))
    if wait:
        typer.echo(json.dumps(wait_for(spec)))


@app.command()
def status(campaign_id: str):
    """Read application checkpoints; works while the runtime is stopped."""
    typer.echo(json.dumps(Catalog(root()).status(campaign_id), indent=2))


@app.command()
def pause(campaign_id: str):
    """Stop new decisions; durable lifecycle reconciliation continues."""
    typer.echo(json.dumps(Catalog(root()).control(campaign_id, True)))


@app.command()
def resume(campaign_id: str):
    """Resume within original bounds after verifying portfolio reconciliation."""
    current = Catalog(root()).status(campaign_id)
    spec = CampaignSpec.model_validate(current["spec"])
    response = httpx.post(f"{INGRESS}/PaperPortfolio/{spec.portfolio}/apply", json={
        "spec": spec.model_dump(mode="json"), "action": "resume",
        "now": datetime.now().astimezone().isoformat()}, timeout=15)
    response.raise_for_status()
    if not response.json()["ok"]:
        raise typer.BadParameter("Portfolio reconciliation failed; campaign remains paused.")
    typer.echo(json.dumps(Catalog(root()).control(campaign_id, False)))


@app.command()
def replay(campaign_id: str, wait: bool = False):
    """Resubmit the same frozen workflow key; never reopen expired decisions."""
    spec = CampaignSpec.model_validate(Catalog(root()).status(campaign_id)["spec"])
    typer.echo(json.dumps(submit(spec)))
    if wait:
        typer.echo(json.dumps(wait_for(spec)))


def report_data(campaign_id):
    report = Catalog(root()).report(campaign_id)
    spec = CampaignSpec.model_validate(report["campaign"]["spec"])
    report["portfolio"] = Ledger(root(), spec.portfolio, spec.policy, read_only=True).reconcile()
    ids = {r["decision"]["id"] for r in report["results"] if r.get("decision")}
    for field in ("positions", "settlements", "marked_estimates", "settlement_events"):
        values = report["portfolio"].get(field, [])
        report["portfolio"][field] = [p for p in values if p.get("decision", p.get("decision_id")) in ids]
    report["portfolio"]["capital_scope"] = "shared portfolio; displayed positions belong to this campaign"
    report["coverage"]["genuine_live_candidates"] = report["campaign"]["detail"].get("unique_markets", 0) if spec.mode == "public" else 0
    report["coverage"]["synthetic_candidates"] = report["coverage"]["completed_candidates"] if spec.mode == "synthetic" else 0
    return report


@app.command()
def export_public(campaign_id: str, public_id: str, out: Path):
    """Export a validated public dashboard read model; review it before publishing."""
    from sgr.paper.public_report import write_public_campaign

    try:
        model = write_public_campaign(report_data(campaign_id), public_id, out)
    except (ValueError, KeyError) as exc:
        raise typer.BadParameter("Public report validation failed; inspect the local report schema and accounting.") from exc
    typer.echo(json.dumps({"public_id": model.public_id, "mode": model.mode,
                           "completed_candidates": model.completed_candidates,
                           "candidate_sample": len(model.candidates), "out": str(out)}))


@app.command()
def report(campaign_id: str, out: Path | None = None):
    """Coverage, exclusions, rankings, reservations and distinct outcomes."""
    data = report_data(campaign_id)
    if out:
        allowed = [Path(p).resolve() for p in ("data", ".research", ".runs")]
        resolved = out.resolve()
        if not any(resolved.is_relative_to(p) for p in allowed):
            raise typer.BadParameter("Detailed evidence reports must stay under ignored data/, .research/, or .runs/.")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=2)+"\n")
    p = data["portfolio"]
    data["coverage"]["risk_exclusions"] = dict(Counter(x["reason"] for x in p["positions"] if x["status"] == "rejected"))
    typer.echo(json.dumps({
        "campaign_id": campaign_id, "status": data["campaign"]["status"], "data_label": data["data_label"],
        "coverage": data["coverage"], "rankings": data["rankings"][:20],
        "portfolio": {k: p[k] for k in ("fictional", "cash", "reserved", "available", "realized_pnl", "paused", "ok")},
        "position_states": dict(Counter(x["status"] for x in p["positions"])),
        "positions": [{"decision_id": x["decision"], "contract": x["contract"], "state": x["status"],
                       "shares": x["quantity"], "spent": str(Decimal(x["spent"])/1000000),
                       "reservation": str(Decimal(x["remaining"])/1000000), "reason": x["reason"]}
                      for x in p["positions"][:20]],
        "settlements": p["settlements"], "marked_estimates": p["marked_estimates"],
    }, indent=2))


@app.command()
def settlements(campaign_id: str, pass_id: str = typer.Option("pass-1", "--pass-id")):
    """One bounded pass over existing filled inventory using public final rules."""
    data = report_data(campaign_id)
    spec = CampaignSpec.model_validate(data["campaign"]["spec"])
    positions = {p["decision"]: p for p in data["portfolio"]["positions"] if p["status"] in ("filled", "partial", "pending_settlement", "disputed")}
    if spec.mode == "synthetic":
        raise typer.BadParameter("Synthetic final outcomes are supplied only by labeled recovery fixtures.")
    if not pass_id or len(pass_id) > 64:
        raise typer.BadParameter("Settlement pass ID must have 1–64 characters; reuse it to retry.")
    submitted = set()
    for result in data["results"]:
        decision = result.get("decision")
        if decision and decision["id"] in positions and decision["id"] not in submitted and len(submitted) < 20:
            submitted.add(decision["id"])
            request = {"settlement_pass": pass_id}
            try:
                response = httpx.post(f"{INGRESS}/PaperTrade/{decision['id']}/settlement", json=request, timeout=30)
            except httpx.HTTPError:
                typer.echo(json.dumps({"decision_id": decision["id"], "status": "settlement_network_error"}))
                continue
            if response.is_error:
                typer.echo(json.dumps({"decision_id": decision["id"], "status": "settlement_failed",
                                       "http_status": response.status_code}))
                continue
            typer.echo(json.dumps(response.json()))
