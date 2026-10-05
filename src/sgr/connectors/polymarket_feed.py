"""Bounded public subscriptions. Market ticks never invoke Restate."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal

from sgr.connectors.polymarket import MARKET_STREAM
from sgr.research.schemas import PriceLevel, TokenBookSnapshot, stable_record_id
from sgr.research.storage import ResearchStore


class BookFeed:
    def __init__(self, seeds: list[TokenBookSnapshot], *, max_age_seconds: float = 10):
        if not seeds or len(seeds) > 100 or max_age_seconds <= 0:
            raise ValueError("Subscribe to 1..100 selected assets with a bounded freshness window.")
        self.seeds = {b.asset_id: b for b in seeds}
        self.depth = {b.asset_id: {"bids": {}, "asks": {}} for b in seeds}
        self.updated: dict[str, datetime] = {}
        self.seen: dict[str, float] = {}
        self.invalid = set(self.seeds)
        self.metadata_invalid: set[str] = set()
        self.metadata_at = {b.asset_id: b.metadata_at or b.available_at for b in seeds}
        self.history: dict[str, list[dict]] = {b.asset_id: [] for b in seeds}
        self.max_age_seconds = max_age_seconds
        self.messages = 0

    def gap(self):
        self.invalid.update(self.seeds)

    def ingest(self, message: dict, *, received_at: datetime):
        self.messages += 1
        if not isinstance(message, dict):
            self.gap()
            return
        kind = message.get("event_type")
        if kind in ("best_bid_ask", "last_trade_price", "new_market"):
            return
        try:
            timestamp = datetime.fromtimestamp(int(message["timestamp"]) / 1000, timezone.utc)
            changes = message.get("price_changes", []) if kind == "price_change" else [message]
            for change in changes:
                asset = change["asset_id"]
                if asset not in self.seeds:
                    continue
                if message.get("market") != self.seeds[asset].condition_id:
                    self.invalid.add(asset)
                    continue
                if timestamp > received_at or timestamp < self.updated.get(asset, timestamp):
                    self.invalid.add(asset)
                    continue
                if kind == "book":
                    if any(len(message[side]) != len({Decimal(x["price"]) for x in message[side]}) for side in ("bids", "asks")):
                        self.invalid.add(asset)
                        continue
                    self.depth[asset] = {
                        side: {Decimal(x["price"]): Decimal(x["size"]) for x in message[side]}
                        for side in ("bids", "asks")
                    }
                    if asset not in self.metadata_invalid:
                        self.invalid.discard(asset)
                    self.history[asset].clear()
                elif kind == "price_change" and asset not in self.invalid:
                    side = {"BUY": "bids", "SELL": "asks"}[change["side"]]
                    price, size = Decimal(change["price"]), Decimal(change["size"])
                    if size == 0:
                        self.depth[asset][side].pop(price, None)
                    else:
                        self.depth[asset][side][price] = size
                else:
                    # Tick changes and lifecycle updates need a new REST seed.
                    self.invalid.add(asset)
                    if kind == "tick_size_change":
                        self.metadata_invalid.add(asset)
                self.history[asset].append(message)
                if len(self.history[asset]) > 1000:
                    self.history[asset].clear()
                    self.gap()
                self.updated[asset] = timestamp
                self.seen[asset] = time.monotonic()
        except (KeyError, TypeError, ValueError, ArithmeticError):
            self.gap()

    def coherent(self, asset: str, now: datetime) -> bool:
        observed = self.updated.get(asset)
        return (
            asset not in self.invalid and observed is not None
            and 0 <= (now - observed).total_seconds() <= self.max_age_seconds
            and time.monotonic() - self.seen[asset] <= self.max_age_seconds
            and 0 <= (now - self.metadata_at[asset]).total_seconds() <= 30
        )

    def persist(self, store: ResearchStore, asset: str, *, now: datetime) -> TokenBookSnapshot:
        if not self.coherent(asset, now):
            raise ValueError("Feed gap or stale book; resynchronize first.")
        seed = self.seeds[asset]
        payload = {s: [{"price": str(p), "size": str(q)} for p, q in self.depth[asset][s].items()] for s in ("bids", "asks")}
        source = store.retain_raw_snapshot(
            "polymarket_derived", {"kind": "reconstructed_public_stream", "events": self.history[asset],
                                  "asset_id": asset, "observed_at": self.updated[asset].isoformat(), **payload},
            source_url=MARKET_STREAM, retrieved_at=now,
        )
        updates = dict(
            id=stable_record_id("token_book_snapshot", asset, source.sha256, now.isoformat()),
            event_time=self.updated[asset], observed_at=self.updated[asset], retrieved_at=now,
            available_at=now, source_snapshots=(*seed.source_snapshots, source),
            bids=tuple(PriceLevel(price_dollars=p, contracts=q) for p, q in sorted(self.depth[asset]["bids"].items(), reverse=True)),
            asks=tuple(PriceLevel(price_dollars=p, contracts=q) for p, q in sorted(self.depth[asset]["asks"].items())),
        )
        # model_copy skips validation, so explicitly revalidate the complete book.
        book = TokenBookSnapshot.model_validate({**seed.model_dump(), **updates})
        store.write([book])
        self.history[asset].clear()
        self.seeds[asset] = book
        return book

    async def collect(self, store: ResearchStore, *, seconds: float = 30, snapshot_interval: float = 5) -> dict:
        import websockets
        if not 0 < seconds <= 30 or not 1 <= snapshot_interval <= seconds:
            raise ValueError("Feed sessions must be explicitly bounded (<=30 seconds); reseed metadata between sessions.")
        snapshots, started, last = 0, time.monotonic(), time.monotonic()
        try:
            async with websockets.connect(MARKET_STREAM, max_queue=32, max_size=2**20) as socket:
                await socket.send(json.dumps({"assets_ids": list(self.seeds), "type": "market", "custom_feature_enabled": True}))
                while time.monotonic() - started < seconds:
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=min(5, seconds - (time.monotonic() - started)))
                    except asyncio.TimeoutError:
                        await socket.send("PING")
                        continue
                    if raw == "PONG":
                        continue
                    now = datetime.now(timezone.utc)
                    try:
                        messages = json.loads(raw)
                    except (ValueError, TypeError):
                        self.gap()
                        continue
                    for message in messages if isinstance(messages, list) else [messages]:
                        self.ingest(message, received_at=now)
                    if time.monotonic() - last >= snapshot_interval:
                        for asset in self.seeds:
                            if self.coherent(asset, now):
                                try:
                                    self.persist(store, asset, now=now)
                                    snapshots += 1
                                except ValueError:
                                    self.invalid.add(asset)
                        last = time.monotonic()
        finally:
            self.gap()
        return {"messages": self.messages, "coherent_snapshots": snapshots, "seconds": time.monotonic() - started}
