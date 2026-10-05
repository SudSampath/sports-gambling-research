"""Real pinned runtime evidence; independent catalog and ledger effect counts."""
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import time

import httpx

from sgr.paper.campaigns import Catalog, CampaignSpec
from sgr.paper.ledger import Ledger
from sgr.paper.models import PaperPolicy
from sgr.paper.runtime import NativeSession


class Harness:
    def __init__(self, tmp_path):
        self.root = Path(tmp_path)/"evidence"
        self.session = NativeSession(Path(tmp_path)/"runtime-session", self.root)
        self.session.start()
        self.catalog = Catalog(self.root)
        self.observations = {}

    def send(self, spec):
        response = httpx.post(f"http://127.0.0.1:8080/ScanCampaign/{spec.id}/run/send", json=spec.model_dump(mode="json"), timeout=15)
        if response.status_code != 409:
            response.raise_for_status()
        return response

    def completed(self, spec, timeout=120):
        def done():
            status = self.catalog.status(spec.id)
            from sgr.paper.campaigns import TERMINAL
            return status if status["status"] in TERMINAL else False
        return self.session.wait(done, timeout=timeout)

    def recovery(self):
        spec = CampaignSpec(id="recovery", mode="synthetic", portfolio="synthetic-recovery",
                            max_candidates=600, max_pages=6, max_seconds=90,
                            policy=PaperPolicy(decision_ttl_seconds=6),
                            test_delay_seconds=.5, test_lose_fill_ack=True)
        self.send(spec)
        ledger = Ledger(self.root, spec.portfolio, spec.policy)
        self.session.wait(lambda: self.catalog.status(spec.id)["completed_candidates"] >= 100 and
                          any(p["spent"] for p in ledger.reconcile()["positions"]), timeout=20)
        before = self.catalog.report(spec.id)
        before_ledger = ledger.reconcile()
        self.session.stop("worker", kill=True)
        self.session.stop("runtime", kill=True)
        # Nothing polls/fills while both processes are down.
        offline = ledger.reconcile()
        self.session.start_runtime()
        self.session.start_worker()
        self.completed(spec, timeout=90)
        after = self.catalog.report(spec.id)
        result = ledger.reconcile()
        self.send(spec)
        replay = ledger.reconcile()
        replay_report = self.catalog.report(spec.id)
        assert replay_report["campaign"]["detail"] == after["campaign"]["detail"]
        assert replay_report["results"] == after["results"]
        first = [r for r in after["results"] if r.get("trade_request")][0]
        settled_at = (datetime.fromisoformat(first["decision"]["kickoff_at"].replace("Z", "+00:00"))+timedelta(hours=4)).isoformat()
        request = {**first["trade_request"], "settled_at": settled_at}
        url = f"http://127.0.0.1:8080/PaperTrade/{first['decision']['id']}/settlement"
        settlement = httpx.post(url, json=request, timeout=15)
        settlement.raise_for_status()
        duplicate = httpx.post(url, json=request, timeout=15)
        duplicate.raise_for_status()
        final = ledger.reconcile()
        self.observations["recovery"] = {
            "before": before["coverage"], "after": after["coverage"],
            "completed_page_attempts": after["operation_attempts"].get("recovery:page:0"),
            "completed_analysis_attempts": after["operation_attempts"].get("recovery:analysis:0:synthetic-condition-0"),
            "fill_attempts": {k:v for k,v in after["operation_attempts"].items() if ":fill:" in k},
            "fill_reconciliation_attempts": {k:v for k,v in after["operation_attempts"].items() if ":reconcile-fill:" in k},
            "offline_unchanged": offline == before_ledger, "replay_unchanged": replay == result,
            "invariants": final["ok"], "positions": len(final["positions"]),
            "settlement_once": settlement.json() == duplicate.json() and len(final["settlements"]) == 1,
            "synthetic_virtual_settlement_time": settled_at,
        }
        return self.observations["recovery"]

    def pause_resume(self):
        spec = CampaignSpec(id="pause", mode="synthetic", portfolio="synthetic-pause", max_candidates=600,
                            max_pages=6, max_seconds=60, test_delay_seconds=.5, policy=PaperPolicy(decision_ttl_seconds=4))
        self.send(spec)
        self.session.wait(lambda: self.catalog.status(spec.id)["completed_candidates"] >= 100, timeout=20)
        self.catalog.control(spec.id, True)
        # Allow already admitted work to reconcile; then compare stable checkpoints.
        time.sleep(1)
        paused = self.catalog.status(spec.id)
        paused_ledger = Ledger(self.root, spec.portfolio, spec.policy).reconcile()
        time.sleep(1)
        stable = self.catalog.status(spec.id)
        stable_ledger = Ledger(self.root, spec.portfolio, spec.policy).reconcile()
        assert stable_ledger["cash"] == paused_ledger["cash"]
        assert [(p["decision"], p["spent"]) for p in stable_ledger["positions"]] == [
            (p["decision"], p["spent"]) for p in paused_ledger["positions"]]
        from unittest.mock import patch
        from sgr.paper.cli import resume
        with patch.dict(os.environ, {"SGR_PAPER_ROOT": str(self.root)}):
            resume(spec.id)
        final = self.completed(spec, timeout=60)
        self.observations["pause_resume"] = {"paused": bool(stable["paused"]),
            "checkpoint_stable": paused["pages"] == stable["pages"], "resumed_completed": final["status"] == "completed",
            "invariants": Ledger(self.root, spec.portfolio, spec.policy).reconcile()["ok"]}
        return self.observations["pause_resume"]

    def stress(self, count=10000):
        spec = CampaignSpec(id="stress", mode="synthetic", portfolio="synthetic-stress", max_candidates=count,
                            max_pages=100, max_seconds=600, fanout=8, policy=PaperPolicy(decision_ttl_seconds=5))
        started, rss = time.monotonic(), {"worker": 0, "runtime": 0}
        cpu = {name: 0.0 for name in rss}
        def process_groups():
            rows = subprocess.check_output(["ps", "-axo", "pgid=,rss=,time="], text=True).splitlines()
            groups = {name: {"rss": 0, "cpu": 0.0} for name in self.session.children}
            for row in rows:
                pgid, resident, duration = row.split()
                for name, child in self.session.children.items():
                    if int(pgid) == child.pid:
                        parts = [float(x) for x in duration.split(":")]
                        seconds = sum(v * 60**i for i, v in enumerate(reversed(parts)))
                        groups[name]["rss"] += int(resident)
                        groups[name]["cpu"] += seconds
            return groups
        baseline = process_groups()
        self.send(spec)
        def measured_done():
            for name, values in process_groups().items():
                rss[name] = max(rss[name], values["rss"])
                cpu[name] = max(cpu[name], values["cpu"]-baseline[name]["cpu"])
            status = self.catalog.status(spec.id)
            return status if status["status"] in ("completed", "partial_provider_failure") else False
        result = self.session.wait(measured_done, timeout=600)
        report = self.catalog.report(spec.id)
        ledger = Ledger(self.root, spec.portfolio, spec.policy).reconcile()
        elapsed = time.monotonic()-started
        self.observations["stress"] = {
            "label": "synthetic, not live", "requested_candidates": count, "completed_candidates": report["coverage"]["completed_candidates"],
            "genuine_live_candidates": 0, "eligible_decisions": report["coverage"]["eligible_decisions"],
            "paper_positions": sum(Decimal(p["quantity"]) > 0 for p in ledger["positions"]),
            "seconds": elapsed, "candidates_per_second": report["coverage"]["completed_candidates"]/elapsed,
            "sampled_peak_group_rss_kib": rss, "sampled_group_cpu_seconds": cpu,
            "catalog_bytes": self.catalog.path.stat().st_size,
            "requests": result["requests"], "invariants": ledger["ok"], "reserved": ledger["reserved"],
        }
        return self.observations["stress"]

    def live(self):
        spec = CampaignSpec(id="live", max_candidates=500, max_pages=5, max_requests=20, max_seconds=60)
        started = time.monotonic()
        self.send(spec)
        status = self.completed(spec, timeout=90)
        report = self.catalog.report(spec.id)
        ledger = Ledger(self.root, spec.portfolio, spec.policy).reconcile()
        self.observations["live"] = {"label": "public live, not synthetic", "coverage": report["coverage"],
                                     "discovered_unique": status["detail"]["discovered"], "pages": status["pages"],
                                     "requests": status["requests"], "seconds": time.monotonic()-started,
                                     "paper_positions": sum(Decimal(p["quantity"]) > 0 for p in ledger["positions"]),
                                     "invariants": ledger["ok"], "status": status["status"],
                                     "missing_audited_contract_catalog": not spec.definitions}
        return self.observations["live"]

    def close(self):
        self.session.close()
        output = Path(".research/restate-evidence.json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps({"runtime": self.session.version, "sdk": "1.0.5", "evidence_root": str(self.root),
                                     "observations": self.observations}, indent=2)+"\n")
