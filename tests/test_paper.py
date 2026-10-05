from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest

from paper_cases import competing_decision, setup_paper
from paper_fixtures import NOW
from sgr.paper.models import PaperPolicy
from sgr.research.opportunities import PointInTimeInputs, devig_decimal_odds


def test_kickoff_between_reservation_and_execution_releases_capital(tmp_path):
    c = setup_paper(tmp_path)
    ledger, d = c["ledger"], c["decision"]
    kickoff = NOW+timedelta(milliseconds=100)
    d = d.model_copy(update={"kickoff_at": kickoff})
    assert ledger.reserve(d, now=NOW)["status"] == "reserved"
    assert ledger.execute(d, c["book"], now=NOW)["status"] == "cancelled"
    assert ledger.reconcile()["reserved"] == "0"
    other = competing_decision(d, 123)
    assert ledger.reserve(other, now=kickoff)["reason"] == "pregame_window_closed"


def test_reporting_audit_is_read_only(tmp_path):
    c = setup_paper(tmp_path)
    audit = type(c["ledger"])(tmp_path, read_only=True)
    assert audit.reconcile()["ok"]
    with pytest.raises(Exception):
        audit.pause("must-fail")


def test_another_reservation_can_expire_an_order_before_its_execution(tmp_path):
    c = setup_paper(tmp_path)
    ledger, d = c["ledger"], c["decision"]
    ledger.reserve(d, now=NOW)
    other = competing_decision(d, 122)
    later = d.expires_at+timedelta(seconds=1)
    ledger.reserve(other, now=later)
    result = ledger.execute(d, c["book"], now=later)
    assert result["status"] == "expired"
    assert result == ledger.execute(d, c["book"], now=later+timedelta(seconds=5))
    assert ledger.reconcile()["ok"] and not ledger.reconcile()["paused"]


def test_expired_decision_with_old_book_does_not_pause_portfolio(tmp_path):
    c = setup_paper(tmp_path)
    ledger, d = c["ledger"], c["decision"]
    ledger.reserve(d, now=NOW)
    assert ledger.execute(d, c["book"], now=d.expires_at)["status"] == "expired"
    assert not ledger.reconcile()["paused"]


def test_archived_json_key_order_preserves_decision_identity(tmp_path):
    import json
    from sgr.paper.models import AnalysisDecision
    c = setup_paper(tmp_path)
    ledger, d = c["ledger"], c["decision"]
    archived = AnalysisDecision.model_validate_json(json.dumps(d.model_dump(mode="json"), sort_keys=True))
    original = ledger.reserve(d, now=NOW)
    assert ledger.reserve(archived, now=NOW) == original
    fill = ledger.execute(d, c["book"], now=NOW)
    assert ledger.execute(archived, c["book"], now=NOW) == fill
    changed = archived.model_copy(update={"input_digest": "f"*64})
    with pytest.raises(ValueError, match="identity"):
        ledger.reserve(changed, now=NOW)


def test_vertical_forecast_reservation_fill_reconcile_settlement(tmp_path):
    c = setup_paper(tmp_path)
    ledger, d = c["ledger"], c["decision"]
    assert d.eligible and c["forecast"] and d.probability is not None
    assert ledger.reserve(d, now=NOW)["status"] == "reserved"
    fill = ledger.execute(d, c["book"], now=NOW)
    assert fill["status"] == "partial" and Decimal(fill["cost"]) > 0 and Decimal(fill["fees"]) > 0
    ledger.cancel(d.id, expired=True)
    assert ledger.reconcile()["reserved"] == "0"
    assert ledger.mark(d.id, c["book"], now=NOW)["label"].startswith("marked estimate")
    evidence = {"url": "https://example.org/synthetic-final", "sha256": "1"*64, "condition_id": d.condition_id}
    result = ledger.settle(d.id, payout=Decimal(1), settled_at=NOW+timedelta(days=2), rule_version=d.rule_version, source=evidence)
    assert ledger.settle(d.id, payout=Decimal(1), settled_at=NOW+timedelta(days=2), rule_version=d.rule_version, source=evidence) == result
    status = ledger.reconcile()
    assert status["ok"] and status["settlements"][0]["realized"]
    assert status["positions"][0]["status"] == "settled"
    assert not status["marked_estimates"]
    assert c["analyzer"].analyze(c["market"], c["books"], now=NOW)[0] == d


def test_lost_ack_and_duplicate_submission_do_not_double_spend(tmp_path):
    c = setup_paper(tmp_path)
    ledger, d = c["ledger"], c["decision"]
    ledger.reserve(d, now=NOW)
    ledger.reserve(d, now=NOW)
    with pytest.raises(ConnectionError):
        ledger.execute(d, c["book"], now=NOW, lose_ack=True)
    before = ledger.reconcile()
    existing = ledger.existing_fill(d.id)
    assert existing == ledger.execute(d, c["book"], now=NOW+timedelta(minutes=10))
    assert ledger.reconcile() == before
    assert len(before["positions"]) == 1 and before["ok"]
    with pytest.raises(ValueError, match="identity reused"):
        ledger.reserve(d.model_copy(update={"probability": Decimal(".99")}), now=NOW)


def test_concurrent_candidates_and_correlated_caps(tmp_path):
    policy = PaperPolicy(initial_capital=50, stake=25, contract_cap=25, event_cap=25, correlated_cap=50, total_cap=50)
    c = setup_paper(tmp_path, policy)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: c["ledger"].reserve(competing_decision(c["decision"], i, group="correlated"), now=NOW), range(20)))
    assert sum(r["status"] == "reserved" for r in results) == 2
    report = c["ledger"].reconcile()
    assert report["ok"] and Decimal(report["reserved"]) == 50
    c["ledger"].cancel(next(r["decision"] for r in results if r["status"] == "reserved"))
    assert c["ledger"].reconcile()["available"] == "25"


def test_stale_book_and_expired_decision_release_reservations(tmp_path):
    c = setup_paper(tmp_path)
    c["ledger"].reserve(c["decision"], now=NOW)
    result = c["ledger"].execute(c["decision"], c["book"], now=NOW+timedelta(seconds=15))
    assert result["status"] == "cancelled"
    status = c["ledger"].reconcile()
    assert status["ok"] and status["reserved"] == "0" and status["paused"] == "data_freshness"
    c["ledger"].resume()
    d = competing_decision(c["decision"], 2)
    assert c["ledger"].reserve(d, now=NOW+timedelta(minutes=1))["status"] == "rejected"


def test_disputed_settlement_and_loss_stop_are_distinct(tmp_path):
    c = setup_paper(tmp_path, PaperPolicy(loss_limit=10))
    ledger, d = c["ledger"], c["decision"]
    ledger.reserve(d, now=NOW)
    ledger.execute(d, c["book"], now=NOW)
    evidence = {"url": "https://example.org/synthetic-final", "sha256": "1"*64, "condition_id": d.condition_id}
    assert ledger.settle(d.id, payout=Decimal(0), settled_at=NOW+timedelta(days=2), rule_version=d.rule_version, source=evidence, disputed=True)["status"] == "disputed"
    assert not ledger.reconcile()["settlements"]
    ledger.settle(d.id, payout=Decimal(0), settled_at=NOW+timedelta(days=2), rule_version=d.rule_version, source=evidence)
    rejected = ledger.reserve(competing_decision(d, 5), now=NOW)
    assert rejected["rejection_reason"] == "realized_loss_limit"


def test_future_available_data_and_duplicates_cannot_change_forecast(tmp_path):
    c = setup_paper(tmp_path)
    future = c["history"][0].model_copy(update={"retrieved_at": NOW+timedelta(seconds=1), "home_score": 0, "away_score": 100})
    view = PointInTimeInputs({"game": [c["game"], *c["history"], c["history"][0], future], "availability_report": [], "player_game_statline": []}, NOW)
    assert view.digest == c["inputs"].digest
    assert devig_decimal_odds(Decimal("1.91"), Decimal("1.91")) == Decimal(".5")
    conflict = c["history"][0].model_copy(update={"home_score": 0})
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        PointInTimeInputs({"game": [*c["history"], conflict]}, NOW)


def test_committed_depth_is_not_reused_and_minimum_fill_rollback(tmp_path):
    c = setup_paper(tmp_path, depth="4")
    assert c["decision"].eligible
    c["ledger"].reserve(c["decision"], now=NOW)
    c["ledger"].execute(c["decision"], c["book"], now=NOW)
    d = c["decision"].model_copy(update={"id": "second-synthetic-decision"})
    c["ledger"].reserve(d, now=NOW)
    assert c["ledger"].execute(d, c["book"], now=NOW)["reason"] == "depth_below_minimum"
    c["ledger"].cancel(d.id)
    assert c["ledger"].reconcile()["ok"]


def test_model_cutoff_cannot_follow_decision_and_historical_versions_survive(tmp_path):
    c = setup_paper(tmp_path)
    from sgr.research.opportunities import Analyzer
    from sgr.research.storage import ResearchStore
    view = PointInTimeInputs(c["inputs"].records, NOW+timedelta(seconds=10))
    decision, forecast = Analyzer(view, c["policy"], [c["definition"]]).analyze(c["market"], c["books"], now=NOW)
    assert not decision.eligible and forecast is None and decision.reasons == ("future_model_input_cutoff",)
    store = ResearchStore(tmp_path/"history")
    old = c["history"][0]
    store.write([old])
    newer = old.model_copy(update={"retrieved_at": NOW+timedelta(seconds=10), "home_score": 0})
    store.write([newer])
    assert store.load_all_as_of("game", NOW)[0].home_score == old.home_score


def test_cancelled_execution_and_rejection_acknowledgements_are_idempotent(tmp_path):
    c = setup_paper(tmp_path)
    ledger, d = c["ledger"], c["decision"]
    ledger.reserve(d, now=NOW)
    cancelled = ledger.execute(d, c["book"], now=NOW+timedelta(seconds=20))
    assert cancelled == ledger.execute(d, c["book"], now=NOW+timedelta(seconds=40))
    other = competing_decision(d, 44)
    rejected = ledger.reserve(other, now=NOW)
    assert rejected == ledger.reserve(other, now=NOW)


def test_changed_retrieval_timestamp_does_not_replenish_depth(tmp_path):
    c = setup_paper(tmp_path, depth="4")
    ledger, d = c["ledger"], c["decision"]
    ledger.reserve(d, now=NOW)
    ledger.execute(d, c["book"], now=NOW)
    second = d.model_copy(update={"id": "synthetic-second-snapshot"})
    ledger.reserve(second, now=NOW)
    newer = c["book"].model_copy(update={"observed_at": NOW+timedelta(seconds=1), "available_at": NOW+timedelta(seconds=1)})
    assert ledger.execute(second, newer, now=NOW+timedelta(seconds=1))["reason"] == "depth_below_minimum"
    ledger.cancel(second.id)
    assert ledger.reconcile()["ok"]


def test_store_backed_conflicting_finals_and_same_time_rewrite_are_rejected(tmp_path):
    from sgr.research.storage import ResearchStore
    c = setup_paper(tmp_path)
    store = ResearchStore(tmp_path/"canonical")
    old = c["history"][0]
    store.write([old])
    with pytest.raises(ValueError, match="same retrieval time"):
        store.write([old.model_copy(update={"home_score": 0})])
    intermediate = old.model_copy(update={"retrieved_at": NOW-timedelta(hours=1), "completed": False, "home_score": None, "away_score": None})
    later = old.model_copy(update={"retrieved_at": NOW, "home_score": 0})
    store.write([intermediate])
    store.write([later])
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        PointInTimeInputs.from_store(store, NOW)
