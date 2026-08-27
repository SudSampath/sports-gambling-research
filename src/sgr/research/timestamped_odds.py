from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable

from sgr.connectors.theoddsapi import HistoricalOddsSnapshot
from sgr.research.schemas import RawSnapshotRef, TimestampedOdds, stable_record_id

MARKETS_WITH_POINT = frozenset({"spreads", "totals"})


class TimestampedOddsError(RuntimeError):
    """Base error for timestamped-odds normalization."""


@dataclass(frozen=True)
class HistoricalOddsCoverage:
    requested_at: datetime
    snapshot_timestamp: datetime | None
    previous_timestamp: datetime | None
    next_timestamp: datetime | None
    events: int
    observations_written: int
    bookmakers: tuple[str, ...]
    markets: tuple[str, ...]


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise TimestampedOddsError(f"Non-numeric odds price/point: {value!r}.") from error


def normalize_historical_odds(
    snapshot: HistoricalOddsSnapshot,
) -> tuple[tuple[TimestampedOdds, ...], HistoricalOddsCoverage]:
    """Normalize one historical-odds API response into TimestampedOdds records.

    The provider returns the closest available snapshot at or before the
    requested date, plus that snapshot's own timestamp -- distinct from
    each bookmaker's own last_update (``event_time`` below) and from when
    this project fetched the response (``retrieved_at``). A response with
    an empty events list is valid (no games at that point in the season)
    and yields zero records, not an error.
    """
    payload = snapshot.payload
    snapshot_timestamp = _parse_timestamp(payload.get("timestamp"))
    previous_timestamp = _parse_timestamp(payload.get("previous_timestamp"))
    next_timestamp = _parse_timestamp(payload.get("next_timestamp"))
    events = payload.get("data") or []

    source = RawSnapshotRef(
        provider="theoddsapi",
        path=f"historical-odds/{snapshot.retrieved_at.strftime('%Y%m%dT%H%M%S%fZ')}.json",
        source_url=snapshot.source_url,
        retrieved_at=snapshot.retrieved_at,
        sha256=snapshot.sha256,
    )

    records: list[TimestampedOdds] = []
    bookmakers_seen: set[str] = set()
    markets_seen: set[str] = set()

    for event in events:
        provider_event_id = str(event.get("id") or "")
        sport_key = str(event.get("sport_key") or "")
        home_team = str(event.get("home_team") or "")
        away_team = str(event.get("away_team") or "")
        commence_time = _parse_timestamp(event.get("commence_time"))
        if not provider_event_id or not home_team or not away_team or commence_time is None:
            continue

        for bookmaker in event.get("bookmakers") or []:
            bookmaker_key = str(bookmaker.get("key") or "")
            last_update = _parse_timestamp(bookmaker.get("last_update"))
            if not bookmaker_key or last_update is None:
                continue
            bookmakers_seen.add(bookmaker_key)

            for market in bookmaker.get("markets") or []:
                market_key = str(market.get("key") or "")
                if market_key not in {"h2h", "spreads", "totals"}:
                    continue
                markets_seen.add(market_key)

                for outcome in market.get("outcomes") or []:
                    outcome_name = str(outcome.get("name") or "")
                    price = outcome.get("price")
                    point = outcome.get("point")
                    if not outcome_name or price is None:
                        continue
                    if market_key in MARKETS_WITH_POINT and point is None:
                        continue

                    identity = (
                        provider_event_id,
                        bookmaker_key,
                        market_key,
                        outcome_name,
                        (snapshot_timestamp or last_update).isoformat(),
                    )
                    records.append(
                        TimestampedOdds(
                            id=stable_record_id("timestamped_odds", *identity),
                            provider_ids={"theoddsapi": ":".join(identity[:4])},
                            event_time=last_update,
                            retrieved_at=snapshot.retrieved_at,
                            source_snapshots=(source,),
                            provider_event_id=provider_event_id,
                            sport_key=sport_key,
                            home_team=home_team,
                            away_team=away_team,
                            commence_time=commence_time,
                            bookmaker_key=bookmaker_key,
                            market_key=market_key,
                            outcome_name=outcome_name,
                            point=_decimal(point) if point is not None else None,
                            price=_decimal(price),
                            provider_timestamp=last_update,
                            snapshot_timestamp=snapshot_timestamp or last_update,
                        )
                    )

    coverage = HistoricalOddsCoverage(
        requested_at=snapshot.requested_at,
        snapshot_timestamp=snapshot_timestamp,
        previous_timestamp=previous_timestamp,
        next_timestamp=next_timestamp,
        events=len(events),
        observations_written=len(records),
        bookmakers=tuple(sorted(bookmakers_seen)),
        markets=tuple(sorted(markets_seen)),
    )
    return tuple(records), coverage


def select_odds_at_or_before(
    records: Iterable[TimestampedOdds],
    *,
    provider_event_id: str,
    bookmaker_key: str,
    market_key: str,
    outcome_name: str,
    prediction_cutoff: datetime,
) -> TimestampedOdds | None:
    """The latest snapshot at or before prediction_cutoff for one exact
    (event, bookmaker, market, outcome) -- or None, left explicitly missing
    rather than backfilled with a later or closing price."""
    if prediction_cutoff.tzinfo is None or prediction_cutoff.utcoffset() is None:
        raise ValueError("prediction_cutoff must be timezone-aware.")
    candidates = [
        record
        for record in records
        if record.provider_event_id == provider_event_id
        and record.bookmaker_key == bookmaker_key
        and record.market_key == market_key
        and record.outcome_name == outcome_name
        and record.snapshot_timestamp <= prediction_cutoff
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda record: record.snapshot_timestamp)
