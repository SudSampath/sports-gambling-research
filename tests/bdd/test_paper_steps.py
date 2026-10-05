from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
import pytest
from pytest_bdd import given, scenarios, then, when

from paper_cases import competing_decision, setup_paper
from paper_fixtures import NOW
from sgr.paper.models import PaperPolicy
from sgr.research.opportunities import PointInTimeInputs

pytestmark = pytest.mark.bdd
scenarios("../features/paper_portfolio.feature")


@given("a synthetic public HTTP market and historical NFL observations", target_fixture="http_case")
def public_fixture(tmp_path):
    return {"root": tmp_path}


@when("the read-only connector discovers books and the opportunity is paper executed")
def public_execution(http_case):
    from paper_cases import public_http_vertical_slice
    http_case["result"] = public_http_vertical_slice(http_case["root"])


@then("exact matching model lineage settlement and portfolio invariants are preserved")
def public_integrity(http_case):
    r = http_case["result"]
    assert all(method == "GET" for method, _ in r["requests"])
    assert r["decision"].forecast_id == r["forecast"].id
    assert r["report"]["ok"] and r["report"]["reserved"] == "0"
    assert len(r["report"]["settlements"]) == 1


@given("a supported fictional NFL opportunity", target_fixture="paper")
def opportunity(tmp_path):
    result = setup_paper(tmp_path)
    assert result["decision"].eligible
    return result


@when("the same decision is reserved twice and its committed fill acknowledgement is lost")
def lost_ack(paper):
    ledger, decision = paper["ledger"], paper["decision"]
    ledger.reserve(decision, now=NOW)
    ledger.reserve(decision, now=NOW)
    with pytest.raises(ConnectionError):
        ledger.execute(decision, paper["book"], now=NOW, lose_ack=True)
    paper["before_retry"] = ledger.reconcile()


@when("that paper execution is retried after decision expiry")
def retry(paper):
    paper["fill"] = paper["ledger"].execute(paper["decision"], paper["book"], now=NOW+timedelta(minutes=1))


@then("only one reservation and fill debit exist")
def once(paper):
    assert paper["ledger"].reconcile() == paper["before_retry"]
    assert paper["fill"] == paper["ledger"].existing_fill(paper["decision"].id)
    assert len(paper["before_retry"]["positions"]) == 1 and paper["before_retry"]["ok"]


@given("fifty fictional dollars and twenty eligible candidates", target_fixture="paper")
def limited(tmp_path):
    return setup_paper(tmp_path, PaperPolicy(initial_capital=50, stake=25, contract_cap=25, event_cap=25, correlated_cap=50, total_cap=50))


@when("all candidates request capital concurrently")
def reserve_concurrently(paper):
    with ThreadPoolExecutor(max_workers=8) as pool:
        paper["results"] = list(pool.map(lambda i: paper["ledger"].reserve(competing_decision(paper["decision"], i, group="correlated"), now=NOW), range(20)))


@then("exactly two fixed stakes are reserved and capital reconciles")
def capped(paper):
    assert sum(r["status"] == "reserved" for r in paper["results"]) == 2
    report = paper["ledger"].reconcile()
    assert report["ok"] and report["reserved"] == "50" and report["available"] == "0"


@when("a conservative taker fill consumes partial depth and the remainder expires")
def partial(paper):
    paper["ledger"].reserve(paper["decision"], now=NOW)
    paper["fill"] = paper["ledger"].execute(paper["decision"], paper["book"], now=NOW)
    paper["ledger"].cancel(paper["decision"].id, expired=True)


@then("the unfilled reservation is released while inventory awaits settlement")
def released(paper):
    report = paper["ledger"].reconcile()
    assert paper["fill"]["status"] == "partial"
    assert report["ok"] and report["reserved"] == "0"
    assert report["positions"][0]["status"] == "pending_settlement"
    assert Decimal(report["positions"][0]["quantity"]) > 0
    assert report["realized_pnl"] == "0"


@when("a reserved decision reaches execution with a stale book")
def stale(paper):
    paper["ledger"].reserve(paper["decision"], now=NOW)
    paper["result"] = paper["ledger"].execute(paper["decision"], paper["book"], now=NOW+timedelta(seconds=15))


@then("execution pauses and all unfilled capital is released")
def paused(paper):
    report = paper["ledger"].reconcile()
    assert paper["result"]["status"] == "cancelled"
    assert report["ok"] and report["reserved"] == "0" and report["paused"] == "data_freshness"


@when("duplicate games and a future-available score are added to model inputs")
def future(paper):
    unseen = paper["history"][0].model_copy(update={"retrieved_at": NOW+timedelta(seconds=1), "home_score": 0})
    paper["modified"] = PointInTimeInputs({"game": [paper["game"], *paper["history"], paper["history"][0], unseen], "availability_report": [], "player_game_statline": []}, NOW)


@then("the frozen forecast input digest stays identical")
def no_leak(paper):
    assert paper["modified"].digest == paper["inputs"].digest
    assert len(paper["modified"].load_all("game")) == 11


@when("filled inventory receives disputed settlement evidence")
def dispute(paper):
    paper["ledger"].reserve(paper["decision"], now=NOW)
    paper["ledger"].execute(paper["decision"], paper["book"], now=NOW)
    paper["result"] = paper["ledger"].settle(paper["decision"].id, payout=Decimal(0), settled_at=NOW+timedelta(days=2),
                                            rule_version=paper["decision"].rule_version,
                                            source={"url": "https://example.org/synthetic-final", "sha256": "1"*64, "condition_id": paper["decision"].condition_id}, disputed=True)


@then("the disputed position remains separate from realized settlement results")
def not_realized(paper):
    report = paper["ledger"].reconcile()
    assert paper["result"]["status"] == "disputed" and not paper["result"]["realized"]
    assert report["positions"][0]["status"] == "disputed"
    assert not report["settlements"] and report["realized_pnl"] == "0" and report["ok"]
