import os
import pytest
from pytest_bdd import given, scenarios, then, when
from restate_harness import Harness

pytestmark = [pytest.mark.bdd, pytest.mark.skipif(os.environ.get("SGR_RUN_RESTATE_TESTS") != "1",
    reason="Set SGR_RUN_RESTATE_TESTS=1 with pinned runtime; bounded real-process checks are explicit.")]
scenarios("../features/restate_campaign.feature")


@pytest.fixture(scope="module")
def native(tmp_path_factory):
    h = Harness(tmp_path_factory.mktemp("native-paper"))
    try:
        yield h
    finally:
        h.close()


@given("a real pinned local Restate runtime with fictional data", target_fixture="runtime")
def pinned(native):
    return native


@when("a campaign loses a fill acknowledgement and its worker and runtime are killed")
def interruption(runtime):
    runtime.recovery()


@then("completed analysis is journaled once and portfolio replay and settlement reconcile")
def recovered(runtime):
    r = runtime.observations["recovery"]
    assert r["offline_unchanged"] and r["replay_unchanged"] and r["invariants"] and r["settlement_once"]
    assert r["after"]["completed_candidates"] == 600
    assert r["completed_page_attempts"] == 1 and r["completed_analysis_attempts"] == 1
    assert r["positions"] == 3 and all(v == 1 for v in r["fill_attempts"].values())
    assert r["fill_reconciliation_attempts"] and all(v >= 2 for v in r["fill_reconciliation_attempts"].values())


@when("a running campaign is paused and resumed within its deadline")
def control(runtime):
    runtime.pause_resume()


@then("checkpoints stop while paused and the campaign completes safely")
def paused(runtime):
    r = runtime.observations["pause_resume"]
    assert r["paused"] and r["checkpoint_stable"] and r["resumed_completed"] and r["invariants"]


@when("ten thousand synthetic candidates pass staged screening")
def stress(runtime):
    runtime.stress()


@then("counts throughput resources and capped paper positions are measured separately from live data")
def measured(runtime):
    r = runtime.observations["stress"]
    assert r["completed_candidates"] == 10000 and r["genuine_live_candidates"] == 0
    assert r["eligible_decisions"] == 50 and r["paper_positions"] <= 4 and r["invariants"]
    assert r["requests"] == 0 and r["seconds"] > 0 and all(v > 0 for v in r["sampled_peak_group_rss_kib"].values())


@when("at most five live market pages are screened")
def live(runtime):
    runtime.live()


@then("live counts exclusions and eligible trades are reported honestly")
def truthful(runtime):
    r = runtime.observations["live"]
    assert 0 < r["coverage"]["completed_candidates"] <= 500
    assert r["pages"] <= 5 and r["requests"] <= 20 and r["invariants"]
    assert r["coverage"]["eligible_decisions"] == 0 and r["paper_positions"] == 0
    assert r["missing_audited_contract_catalog"]
