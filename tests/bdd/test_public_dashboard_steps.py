from copy import deepcopy
from datetime import timedelta
import json

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from paper_cases import setup_paper
from paper_fixtures import NOW
from sgr.paper.public_report import PublicCampaign, write_public_campaign
from sgr.paper.ledger import Ledger
from sgr.paper.campaigns import CampaignSpec

pytestmark = pytest.mark.bdd
scenarios("../features/public_dashboard.feature")


def fixture_report(tmp_path, *, synthetic=False, fill=False):
    case = setup_paper(tmp_path)
    if synthetic:
        case["ledger"] = Ledger(tmp_path, "synthetic-test", case["policy"])
    decision = case["decision"].model_dump(mode="json")
    unknown = deepcopy(decision)
    unknown.update(id="analysis:unknown", eligible=False, probability=None, net_edge=None,
                   limit_price=None, reasons=["unsupported_outcome"], model_version="unknown",
                   calibration_version="unknown", snapshot_ids=[])
    if fill:
        case["ledger"].reserve(case["decision"], now=NOW)
        case["ledger"].execute(case["decision"], case["book"], now=NOW)
        case["ledger"].cancel(case["decision"].id)
        case["ledger"].mark(case["decision"].id, case["book"], now=NOW)
    portfolio = case["ledger"].reconcile()
    decisions = [decision, unknown] if synthetic else [unknown]
    return {"campaign": {"id": "internal-id", "status": "completed", "started": NOW.isoformat(),
                         "pages": 1, "requests": 1 if not synthetic else 0,
                         "detail": {"finished_at": NOW.isoformat()},
                         "spec": {"mode": "synthetic" if synthetic else "public",
                                  "portfolio": case["ledger"].portfolio,
                                  "definitions": ["audited"] if synthetic else [],
                                  "policy": case["policy"].model_dump(mode="json")}},
            "coverage": {"completed_candidates": len(decisions), "eligible_decisions": int(synthetic),
                         "genuine_live_candidates": 0 if synthetic else 1,
                         "synthetic_candidates": len(decisions) if synthetic else 0,
                         "exclusions": {"unsupported_outcome": 1}},
            "results": [{"decision": d} for d in decisions], "rankings": [], "portfolio": portfolio}


@given("a reconciled public campaign report with an unsupported decision", target_fixture="case")
def public_case(tmp_path):
    return {"report": fixture_report(tmp_path), "public_id": "public-screen", "out": tmp_path / "public.json"}


@given("a synthetic campaign with filled paper inventory and a depth-limited mark", target_fixture="case")
def paper_case(tmp_path):
    return {"report": fixture_report(tmp_path, synthetic=True, fill=True),
            "public_id": "synthetic-test", "out": tmp_path / "public.json"}


@given("internal paths and private sentinel fields appear throughout the report")
def private_fields(case):
    for obj in (case["report"], case["report"]["campaign"], case["report"]["portfolio"],
                case["report"]["results"][0]["decision"], case["report"]["campaign"]["spec"]["policy"]):
        obj["private_sentinel"] = "/Users/private-person/private-vault/DO_NOT_PUBLISH"


@when("I export its public read model")
def export(case):
    case["model"] = write_public_campaign(case["report"], case["public_id"], case["out"])


@then("genuine market counts remain separate and the unsupported probability is unknown")
def unknown(case):
    model = PublicCampaign.model_validate_json(case["out"].read_text())
    assert model.mode == "public" and model.genuine_live_candidates == 1 and model.synthetic_candidates == 0
    assert model.eligible_decisions == 0 and model.candidates[0].probability is None
    assert model.candidates[0].model_version == "unknown"


@then("synthetic counts and fictional cash reconcile while marks remain unrealized")
def fictional(case):
    model = case["model"]
    assert model.mode == "synthetic" and model.synthetic_candidates == 2 and model.genuine_live_candidates == 0
    assert model.capital.fictional and model.capital.available == model.capital.cash - model.capital.reserved
    assert model.capital.realized_pnl == 0 and model.positions[0].shares > 0
    assert model.marked_estimates[0].liquidation_value > 0 and not model.settlements
    assert "unrealized_pnl" not in model.marked_estimates[0].model_dump()


@then("the exported file contains only the public allowlist")
def allowlist(case):
    text = case["out"].read_text()
    assert "private_sentinel" not in text and "DO_NOT_PUBLISH" not in text and "/Users/" not in text
    assert "spec" not in json.loads(text) and "source_snapshots" not in text
    assert "internal-id" not in text


@given(parsers.parse("the report contains {failure}"))
def invalid(case, failure):
    report = case["report"]
    decision = report["results"][0]["decision"]
    if failure == "mixed synthetic counts": report["coverage"]["synthetic_candidates"] = 1
    elif failure == "invented probability": decision["probability"] = "0.7"
    elif failure == "look-ahead information": decision["feature_cutoff_at"] = (NOW + timedelta(days=1)).isoformat()
    elif failure == "duplicate decisions": report["results"].append(deepcopy(report["results"][0]))
    elif failure == "unreconciled accounting": report["portfolio"]["ok"] = False
    elif failure == "contradictory coverage": report["coverage"]["eligible_decisions"] = 1
    elif failure == "an unsafe public label": case["public_id"] = "../../private-path"
    elif failure == "a synthetic portfolio": report["campaign"]["spec"]["portfolio"] = "synthetic-private"
    else: raise AssertionError(failure)


@when("I attempt a public export")
def reject(case):
    case["out"].write_text("previous reviewed publication")
    with pytest.raises(ValueError):
        write_public_campaign(case["report"], case["public_id"], case["out"])


@then("validation fails and the previous publication remains unchanged")
def unchanged(case):
    assert case["out"].read_text() == "previous reviewed publication"
    assert not case["out"].with_suffix(".json.tmp").exists()


@when("an edited publication understates eligible decisions or omits a marked position")
def edited_samples(case):
    case["edits"] = []
    data = case["model"].model_dump(mode="json")
    data["eligible_decisions"] = 0
    case["edits"].append(data)
    data = case["model"].model_dump(mode="json")
    data["positions"] = []
    data["position_total"] = 0
    case["edits"].append(data)
    data = case["model"].model_dump(mode="json")
    data["policy"]["stake"] = "20"
    case["edits"].append(data)
    data = case["model"].model_dump(mode="json")
    data["filled_position_total"] = 0
    case["edits"].append(data)
    data = case["model"].model_dump(mode="json")
    data["scan_finished_at"] = (NOW - timedelta(seconds=1)).isoformat()
    case["edits"].append(data)
    data = case["model"].model_dump(mode="json")
    data.update(mode="public", genuine_live_candidates=data["completed_candidates"], synthetic_candidates=0)
    from datetime import datetime
    data["marked_estimates"][0]["marked_at"] = (datetime.fromisoformat(data["exported_at"]) + timedelta(days=1)).isoformat()
    case["edits"].append(data)
    data = case["model"].model_dump(mode="json")
    data.update(mode="public", genuine_live_candidates=data["completed_candidates"], synthetic_candidates=0)
    future = datetime.fromisoformat(data["exported_at"]) + timedelta(days=1)
    data["candidates"][0]["decision_at"] = future.isoformat()
    data["candidates"][0]["expires_at"] = (future + timedelta(seconds=30)).isoformat()
    case["edits"].append(data)


@then("public schema validation rejects the contradictory samples")
def reject_samples(case):
    for data in case["edits"]:
        with pytest.raises(ValueError):
            PublicCampaign.model_validate(data)


@given("a public campaign spec naming a synthetic portfolio", target_fixture="spec_case")
def mixed_spec():
    return {"id": "public-screen", "portfolio": "synthetic-other", "mode": "public"}


@then("the campaign spec is rejected before execution")
def reject_spec(spec_case):
    with pytest.raises(ValueError, match="Public campaigns"):
        CampaignSpec.model_validate(spec_case)
