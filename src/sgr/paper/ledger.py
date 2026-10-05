"""Transactional, independently idempotent fictional portfolio ledger.

Restate serializes the portfolio object. BEGIN IMMEDIATE additionally protects
capital and fill effects when responses are lost or clients bypass orchestration.
Amounts use integer microdollars; quantities use exact decimal text.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import re
from pathlib import Path
import sqlite3

from sgr.paper.models import AnalysisDecision, PaperPolicy
from sgr.research.schemas import TokenBookSnapshot, stable_record_id

UNIT = Decimal("1000000")


def micros(value: Decimal) -> int:
    return int((value * UNIT).to_integral_value(rounding=ROUND_CEILING))


def fee_per_share(price: Decimal, bps: int) -> Decimal:
    return Decimal(bps) / 10000 * price * (1 - price)


def same_decision(payload: str, decision: AnalysisDecision) -> bool:
    """JSON object order is not evidence; validated values and array order are."""
    return AnalysisDecision.model_validate_json(payload) == decision


def fresh(book: TokenBookSnapshot, now: datetime, policy: PaperPolicy) -> bool:
    return (
        book.feed_ok and book.observed_at <= book.available_at <= now
        and 0 <= (now - book.observed_at).total_seconds() <= policy.quote_max_age_seconds
        and (now - book.available_at).total_seconds() <= policy.quote_max_age_seconds
        and 0 <= (now - (book.metadata_at or book.available_at)).total_seconds() <= 30
    )


class Ledger:
    def __init__(self, root: Path | str, portfolio: str = "fictional-nfl", policy: PaperPolicy | None = None, *, read_only=False):
        self.root = Path(root)
        self.read_only = read_only
        self.path = self.root / "paper.sqlite"
        self.portfolio, self.policy = portfolio, policy or PaperPolicy()
        if read_only:
            if not self.path.is_file():
                raise ValueError("Paper portfolio has not been initialized.")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "paper.sqlite"
        self.portfolio, self.policy = portfolio, policy or PaperPolicy()
        with self.transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS portfolios (
                  id TEXT PRIMARY KEY, policy TEXT, fingerprint TEXT, initial INTEGER,
                  cash INTEGER, reserved INTEGER DEFAULT 0, pnl INTEGER DEFAULT 0,
                  paused TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS reservations (
                  portfolio TEXT, decision TEXT, contract TEXT, event TEXT, groups_json TEXT,
                  payload TEXT, amount INTEGER, remaining INTEGER, spent INTEGER DEFAULT 0,
                  quantity TEXT DEFAULT '0', status TEXT, reason TEXT DEFAULT '', PRIMARY KEY(portfolio,decision));
                CREATE TABLE IF NOT EXISTS fills (
                  id TEXT PRIMARY KEY, portfolio TEXT, decision TEXT, cost INTEGER,
                  fee INTEGER, quantity TEXT, book TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS depth (
                  portfolio TEXT, snapshot TEXT, price TEXT, used TEXT,
                  PRIMARY KEY(portfolio,snapshot,price));
                CREATE TABLE IF NOT EXISTS settlements (
                  id TEXT PRIMARY KEY, portfolio TEXT, decision TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS marks (
                  portfolio TEXT, decision TEXT, payload TEXT, PRIMARY KEY(portfolio,decision));
                CREATE TABLE IF NOT EXISTS execution_results(id TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS settlement_events(
                  id TEXT PRIMARY KEY, portfolio TEXT, decision TEXT, payload TEXT);
            """)
            initial = micros(self.policy.initial_capital)
            conn.execute("INSERT OR IGNORE INTO portfolios(id,policy,fingerprint,initial,cash) VALUES (?,?,?,?,?)",
                         (portfolio, self.policy.model_dump_json(), self.policy.fingerprint, initial, initial))
            existing = conn.execute("SELECT fingerprint FROM portfolios WHERE id=?", (portfolio,)).fetchone()[0]
            if existing != self.policy.fingerprint:
                raise ValueError("Portfolio policy is frozen. Use a new portfolio for a different policy.")

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro" if self.read_only else self.path,
                               timeout=30, uri=self.read_only)
        conn.row_factory = sqlite3.Row
        if not self.read_only:
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("BEGIN" if self.read_only else "BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _portfolio(self, conn):
        return conn.execute("SELECT * FROM portfolios WHERE id=?", (self.portfolio,)).fetchone()

    def reserve(self, decision: AnalysisDecision, *, now: datetime) -> dict:
        payload = decision.model_dump_json()
        with self.transaction() as conn:
            old = conn.execute("SELECT * FROM reservations WHERE portfolio=? AND decision=?", (self.portfolio, decision.id)).fetchone()
            if old:
                if not same_decision(old["payload"], decision):
                    raise ValueError("Decision identity reused with different immutable evidence.")
                return {**dict(old), "rejection_reason": old["reason"] or None}
            self._expire(conn, now)
            p = self._portfolio(conn)
            amount = micros(decision.requested_notional)
            reason = None
            if decision.policy_fingerprint != self.policy.fingerprint:
                reason = "policy_mismatch"
            elif not decision.eligible:
                reason = "analysis_rejected"
            elif not decision.decision_at <= now < decision.expires_at:
                reason = "decision_expired_or_future"
            elif decision.kickoff_at is None or now >= decision.kickoff_at:
                reason = "pregame_window_closed"
            elif p["paused"]:
                reason = "portfolio_paused:" + p["paused"]
            elif -p["pnl"] >= micros(self.policy.loss_limit):
                reason = "realized_loss_limit"
            elif amount > micros(self.policy.stake) or amount <= 0:
                reason = "fixed_stake_limit"
            else:
                active = conn.execute("SELECT * FROM reservations WHERE portfolio=? AND status NOT IN ('settled','rejected','expired','cancelled')", (self.portfolio,)).fetchall()
                def exposure(predicate):
                    return sum(r["spent"] + r["remaining"] for r in active if predicate(r))
                if amount > p["cash"] - p["reserved"] or exposure(lambda r: True) + amount > micros(self.policy.total_cap):
                    reason = "capital_or_total_exposure"
                elif exposure(lambda r: r["contract"] == decision.condition_id) + amount > micros(self.policy.contract_cap):
                    reason = "contract_exposure"
                elif exposure(lambda r: r["event"] == decision.event_id) + amount > micros(self.policy.event_cap):
                    reason = "event_exposure"
                elif any(exposure(lambda r: group in json.loads(r["groups_json"])) + amount > micros(self.policy.correlated_cap) for group in decision.groups):
                    reason = "correlated_exposure"
            status = "rejected" if reason else "reserved"
            conn.execute("INSERT INTO reservations(portfolio,decision,contract,event,groups_json,payload,amount,remaining,status,reason) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (self.portfolio, decision.id, decision.condition_id, decision.event_id, json.dumps(decision.groups), payload, amount, 0 if reason else amount, status, reason or ""))
            if reason is None:
                conn.execute("UPDATE portfolios SET reserved=reserved+? WHERE id=?", (amount, self.portfolio))
            result = dict(conn.execute("SELECT * FROM reservations WHERE portfolio=? AND decision=?", (self.portfolio, decision.id)).fetchone())
            result["rejection_reason"] = reason
            return result

    def existing_reservation(self, decision: AnalysisDecision) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM reservations WHERE portfolio=? AND decision=?", (self.portfolio, decision.id)).fetchone()
            if row is not None and not same_decision(row["payload"], decision):
                raise ValueError("Reservation identity/evidence mismatch.")
            return dict(row) if row is not None else None

    def existing_fill(self, decision_id: str) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT payload FROM fills WHERE portfolio=? AND decision=?", (self.portfolio, decision_id)).fetchone()
            return json.loads(row[0]) if row else None

    def execute(self, decision: AnalysisDecision, book: TokenBookSnapshot, *, now: datetime, lose_ack: bool = False, latency_already_elapsed: bool = False) -> dict:
        operation = stable_record_id("paper_fill", self.portfolio, decision.id, "taker-v1")
        with self.transaction() as conn:
            r = conn.execute("SELECT * FROM reservations WHERE portfolio=? AND decision=?", (self.portfolio, decision.id)).fetchone()
            if r is None or not same_decision(r["payload"], decision):
                raise ValueError("Paper execution identity/evidence mismatch.")
            old = conn.execute("SELECT payload FROM execution_results WHERE id=?", (operation,)).fetchone()
            if old:
                return json.loads(old[0])
            r = conn.execute("SELECT * FROM reservations WHERE portfolio=? AND decision=?", (self.portfolio, decision.id)).fetchone()
            if r is not None and same_decision(r["payload"], decision) and r["status"] in ("expired", "cancelled", "rejected"):
                result = {"status": r["status"], "reason": r["reason"] or "reservation_already_released"}
                conn.execute("INSERT INTO execution_results VALUES (?,?)", (operation, json.dumps(result)))
                return result
            if r is None or r["status"] != "reserved" or not same_decision(r["payload"], decision):
                raise ValueError("Paper fill requires the original active reservation.")
            effective_time = now if latency_already_elapsed else now + timedelta(milliseconds=self.policy.latency_ms)
            p = self._portfolio(conn)
            if p["paused"] or effective_time >= decision.expires_at or effective_time >= decision.kickoff_at or not fresh(book, effective_time, self.policy):
                self._release(conn, r, "expired" if effective_time >= decision.expires_at else "cancelled")
                if effective_time < min(decision.expires_at, decision.kickoff_at) and not fresh(book, effective_time, self.policy):
                    conn.execute("UPDATE portfolios SET paused=CASE WHEN paused='' THEN 'data_freshness' ELSE paused END WHERE id=?", (self.portfolio,))
                result = {"status": "expired" if effective_time >= decision.expires_at else "cancelled", "reason": "paused_or_stale_quote_or_expiry"}
                conn.execute("INSERT INTO execution_results VALUES (?,?)", (operation, json.dumps(result)))
                return result
            if book.condition_id != decision.condition_id or book.asset_id != decision.asset_id:
                self._release(conn, r, "cancelled")
                conn.execute("UPDATE portfolios SET paused=CASE WHEN paused='' THEN 'identity_reconciliation' ELSE paused END WHERE id=?", (self.portfolio,))
                result = {"status": "cancelled", "reason": "book_identity_mismatch"}
                conn.execute("INSERT INTO execution_results VALUES (?,?)", (operation, json.dumps(result)))
                return result
            # Deduplicate displayed liquidity independently of retrieval snapshot IDs.
            # Carry consumption forward across unchanged/retrieved snapshots.
            # New liquidity is usable only when displayed size exceeds prior use.
            depth_key = book.asset_id
            conn.execute("SAVEPOINT consume_depth")
            left, cost, fees, qty, levels = r["remaining"], 0, 0, Decimal(0), []
            for level in sorted(book.asks, key=lambda x: x.price_dollars):
                price = level.price_dollars
                fee = fee_per_share(price, book.fee_rate_bps)
                # Current fee changes or a worse quote must still clear the policy.
                if price > decision.limit_price or decision.probability - price - fee - decision.uncertainty_cost < self.policy.min_net_edge:
                    break
                price_key = str(price.normalize())
                consumed = conn.execute("SELECT used FROM depth WHERE portfolio=? AND snapshot=? AND price=?", (self.portfolio, depth_key, price_key)).fetchone()
                used = Decimal(consumed[0]) if consumed else Decimal(0)
                available = max(Decimal(0), level.contracts * self.policy.depth_fraction - used)
                affordable = Decimal(left) / UNIT / (price + fee)
                units = min(available, affordable).quantize(Decimal(".000001"), rounding=ROUND_FLOOR)
                charged = micros(units * (price + fee))
                if units <= 0 or charged > left:
                    continue
                left -= charged
                cost += charged
                fees += micros(units * fee)
                qty += units
                levels.append({"price": str(price), "shares": str(units), "fee": str(units * fee)})
                conn.execute("INSERT OR REPLACE INTO depth VALUES (?,?,?,?)", (self.portfolio, depth_key, price_key, str(used + units)))
            if qty < book.minimum_order_size:
                conn.execute("ROLLBACK TO consume_depth")
                conn.execute("RELEASE consume_depth")
                self._release(conn, r, "cancelled")
                result = {"status": "cancelled", "reason": "depth_below_minimum"}
                conn.execute("INSERT INTO execution_results VALUES (?,?)", (operation, json.dumps(result)))
                return result
            conn.execute("RELEASE consume_depth")
            released_dust = left if left <= 1 else 0
            left -= released_dust
            result = {"id": operation, "decision_id": decision.id, "status": "filled" if left <= 1 else "partial",
                      "fictional": True, "cost": str(Decimal(cost)/UNIT), "fees": str(Decimal(fees)/UNIT),
                      "shares": str(qty), "book_id": book.id, "observed_at": book.observed_at.isoformat(),
                      "simulated_at": effective_time.isoformat(), "levels": levels}
            conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)", (operation, self.portfolio, decision.id, cost, fees, str(qty), book.id, json.dumps(result, sort_keys=True)))
            conn.execute("INSERT INTO execution_results VALUES (?,?)", (operation, json.dumps(result, sort_keys=True)))
            conn.execute("UPDATE reservations SET remaining=?, spent=?, quantity=?, status=? WHERE portfolio=? AND decision=?",
                         (left, cost, str(qty), result["status"], self.portfolio, decision.id))
            conn.execute("UPDATE portfolios SET cash=cash-?, reserved=reserved-? WHERE id=?", (cost, cost+released_dust, self.portfolio))
        if lose_ack:
            raise ConnectionError("Synthetic acknowledgement lost after fill commit.")
        return result

    def _expire(self, conn, now):
        rows = conn.execute("SELECT * FROM reservations WHERE portfolio=? AND status IN ('reserved','partial')", (self.portfolio,)).fetchall()
        for row in rows:
            if AnalysisDecision.model_validate_json(row["payload"]).expires_at <= now:
                self._release(conn, row, "pending_settlement" if Decimal(row["quantity"]) else "expired")

    def _release(self, conn, row, status):
        conn.execute("UPDATE portfolios SET reserved=reserved-? WHERE id=?", (row["remaining"], self.portfolio))
        conn.execute("UPDATE reservations SET remaining=0, status=? WHERE portfolio=? AND decision=?",
                     (status, self.portfolio, row["decision"]))

    def cancel(self, decision_id: str, *, expired: bool = False) -> dict:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM reservations WHERE portfolio=? AND decision=?", (self.portfolio, decision_id)).fetchone()
            if row is None:
                return {"status": "absent"}
            if row["status"] in ("reserved", "partial"):
                # Filled inventory survives cancellation of its unfilled remainder.
                status = "pending_settlement" if Decimal(row["quantity"]) > 0 else ("expired" if expired else "cancelled")
                self._release(conn, row, status)
            return dict(conn.execute("SELECT * FROM reservations WHERE portfolio=? AND decision=?", (self.portfolio, decision_id)).fetchone())

    def reconcile(self) -> dict:
        with self.transaction() as conn:
            p = self._portfolio(conn)
            rows = conn.execute("SELECT * FROM reservations WHERE portfolio=?", (self.portfolio,)).fetchall()
            committed = conn.execute("SELECT coalesce(sum(cost),0) FROM fills WHERE portfolio=?", (self.portfolio,)).fetchone()[0]
            payouts = sum(json.loads(x[0])["payout_micros"] for x in conn.execute("SELECT payload FROM settlements WHERE portfolio=?", (self.portfolio,)))
            bad = p["reserved"] != sum(r["remaining"] for r in rows) or p["cash"] != p["initial"] - committed + payouts or p["cash"] < p["reserved"] or p["reserved"] < 0
            bad = bad or committed != sum(r["spent"] for r in rows)
            final_events = [json.loads(x[0]) for x in conn.execute("SELECT payload FROM settlements WHERE portfolio=?", (self.portfolio,))]
            bad = bad or p["pnl"] != sum(x["pnl_micros"] for x in final_events)
            for row in rows:
                fills = conn.execute("SELECT cost,quantity FROM fills WHERE portfolio=? AND decision=?", (self.portfolio, row["decision"])).fetchall()
                bad = bad or row["spent"] != sum(f["cost"] for f in fills) or Decimal(row["quantity"]) != sum((Decimal(f["quantity"]) for f in fills), Decimal(0))
            if bad and not self.read_only:
                conn.execute("UPDATE portfolios SET paused='reconciliation_failed' WHERE id=?", (self.portfolio,))
            result = {"ok": not bad, "portfolio": self.portfolio, "fictional": True, "policy": self.policy.model_dump(mode="json"),
                      "cash": str(Decimal(p["cash"])/UNIT), "reserved": str(Decimal(p["reserved"])/UNIT),
                      "available": str(Decimal(p["cash"]-p["reserved"])/UNIT), "realized_pnl": str(Decimal(p["pnl"])/UNIT),
                      "paused": "reconciliation_failed" if bad else p["paused"],
                      "positions": [dict(r) for r in rows],
                      "settlements": [json.loads(x[0]) for x in conn.execute("SELECT payload FROM settlements WHERE portfolio=?", (self.portfolio,))],
                      "marked_estimates": [json.loads(x[0]) for x in conn.execute("SELECT m.payload FROM marks m JOIN reservations r ON r.portfolio=m.portfolio AND r.decision=m.decision WHERE m.portfolio=? AND r.status!='settled'", (self.portfolio,))],
                      "settlement_events": [json.loads(x[0]) for x in conn.execute("SELECT payload FROM settlement_events WHERE portfolio=?", (self.portfolio,))]}
            return result

    def pause(self, reason: str = "operator"):
        with self.transaction() as conn:
            conn.execute("UPDATE portfolios SET paused=? WHERE id=?", (reason, self.portfolio))

    def resume(self):
        if not self.reconcile()["ok"]:
            raise ValueError("Reconciliation failed; repair ledger before resuming.")
        with self.transaction() as conn:
            conn.execute("UPDATE portfolios SET paused='' WHERE id=?", (self.portfolio,))

    def settle(self, decision_id: str, *, payout: Decimal, settled_at: datetime, rule_version: str, source: dict, disputed: bool = False) -> dict:
        if not 0 <= payout <= 1 or settled_at.tzinfo is None or not re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")) or not source.get("url", "").startswith("https://"):
            raise ValueError("Settlement requires bounded payout, aware time and cited source evidence.")
        operation = stable_record_id("paper_settlement", self.portfolio, decision_id)
        with self.transaction() as conn:
            r = conn.execute("SELECT * FROM reservations WHERE portfolio=? AND decision=?", (self.portfolio, decision_id)).fetchone()
            if r is None or Decimal(r["quantity"]) <= 0:
                raise ValueError("There is no filled position to settle.")
            decision = AnalysisDecision.model_validate_json(r["payload"])
            if source.get("condition_id") != decision.condition_id:
                raise ValueError("Settlement evidence is not bound to the contract.")
            if rule_version != decision.rule_version or settled_at <= max(decision.decision_at, decision.kickoff_at):
                raise ValueError("Settlement lineage/time mismatch.")
            body = {"id": operation, "decision_id": decision_id, "payout": str(payout), "settled_at": settled_at.isoformat(), "rule_version": rule_version, "source": source}
            old = conn.execute("SELECT payload FROM settlements WHERE id=?", (operation,)).fetchone()
            if old:
                result = json.loads(old[0])
                if any(result[k] != v for k, v in body.items()):
                    raise ValueError("Final settlement identity reused with different evidence.")
                return result
            if disputed:
                self._release(conn, r, "disputed")
                body.update(status="disputed", realized=False)
                event_id = stable_record_id("settlement_event", operation, source["sha256"], settled_at.isoformat(), "disputed")
                conn.execute("INSERT OR IGNORE INTO settlement_events VALUES (?,?,?,?)", (event_id, self.portfolio, decision_id, json.dumps(body, sort_keys=True)))
                return body
            amount = int((Decimal(r["quantity"]) * payout * UNIT).to_integral_value(rounding=ROUND_FLOOR))
            body.update(status="settled", realized=True, payout_micros=amount, pnl_micros=amount-r["spent"])
            self._release(conn, r, "settled")
            conn.execute("INSERT INTO settlements VALUES (?,?,?,?)", (operation, self.portfolio, decision_id, json.dumps(body, sort_keys=True)))
            conn.execute("INSERT INTO settlement_events VALUES (?,?,?,?)", (operation, self.portfolio, decision_id, json.dumps(body, sort_keys=True)))
            conn.execute("UPDATE portfolios SET cash=cash+?, pnl=pnl+? WHERE id=?", (amount, amount-r["spent"], self.portfolio))
            return body

    def mark(self, decision_id: str, book: TokenBookSnapshot, *, now: datetime) -> dict:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM reservations WHERE portfolio=? AND decision=?", (self.portfolio, decision_id)).fetchone()
            if row is None:
                raise ValueError("Unknown position.")
            decision = AnalysisDecision.model_validate_json(row["payload"])
            if book.asset_id != decision.asset_id or not fresh(book, now, self.policy):
                raise ValueError("Mark requires a matching fresh public book.")
            left, value = Decimal(row["quantity"]), Decimal(0)
            for level in sorted(book.bids, key=lambda x: x.price_dollars, reverse=True):
                qty = min(left, level.contracts * self.policy.depth_fraction)
                value += qty * (level.price_dollars - fee_per_share(level.price_dollars, book.fee_rate_bps))
                left -= qty
            body = {"decision_id": decision_id, "label": "marked estimate; unrealized", "liquidation_value": str(value), "unpriced_shares": str(left), "book_id": book.id, "marked_at": now.isoformat()}
            conn.execute("INSERT OR REPLACE INTO marks VALUES (?,?,?)", (self.portfolio, decision_id, json.dumps(body)))
            return body

    def settlement_for(self, decision_id: str) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT payload FROM settlements WHERE portfolio=? AND decision=?", (self.portfolio, decision_id)).fetchone()
            return json.loads(row[0]) if row else None
