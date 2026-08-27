from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

import duckdb

from sgr.research.schemas import (
    CanonicalLineage,
    CanonicalRecord,
    MatchDecision,
    Outcome,
    PaperRecommendation,
    RawSnapshotRef,
    load_canonical_record,
)


TABLES = {
    "team": "teams",
    "game": "games",
    "team_strength_snapshot": "team_strength_snapshots",
    "roster_continuity_signal": "roster_continuity_signals",
    "closing_line": "closing_lines",
    "team_game_efficiency": "team_game_efficiencies",
    "kalshi_event": "kalshi_events",
    "kalshi_market": "kalshi_markets",
    "orderbook_snapshot": "orderbook_snapshots",
    "forecast": "forecasts",
    "match_decision": "match_decisions",
    "paper_recommendation": "paper_recommendations",
    "outcome": "outcomes",
    "availability_report": "availability_reports",
    "player_game_statline": "player_game_statlines",
}


class ResearchStore:
    """Local evidence and query layer: immutable JSON, DuckDB, and Parquet."""

    def __init__(self, root: Path | str = "data/research") -> None:
        self.root = Path(root).resolve()
        self.raw_dir = self.root / "raw"
        self.canonical_dir = self.root / "canonical"
        self.database_path = self.root / "research.duckdb"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.canonical_dir.mkdir(parents=True, exist_ok=True)

    def retain_raw_snapshot(
        self,
        provider: str,
        payload: dict,
        *,
        source_url: str,
        retrieved_at: datetime,
    ) -> RawSnapshotRef:
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", provider):
            raise ValueError("Provider names must be lowercase filesystem-safe identifiers.")
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("Raw snapshot retrieval timestamps must be timezone-aware.")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        checksum = hashlib.sha256(canonical).hexdigest()
        timestamp = retrieved_at.strftime("%Y%m%dT%H%M%S%fZ")
        relative_path = (
            Path("raw")
            / provider
            / retrieved_at.date().isoformat()
            / f"{timestamp}-{checksum}.json"
        )
        path = (self.root / relative_path).resolve()
        if self.root not in path.parents:
            raise ValueError("Raw snapshot path escaped the research store.")
        path.parent.mkdir(parents=True, exist_ok=True)
        reference = RawSnapshotRef(
            provider=provider,
            path=relative_path.as_posix(),
            source_url=source_url,
            retrieved_at=retrieved_at,
            sha256=checksum,
        )
        envelope = json.dumps(
            {
                "provider": provider,
                "source_url": source_url,
                "retrieved_at": retrieved_at.isoformat(),
                "sha256": checksum,
                "payload": payload,
            },
            sort_keys=True,
            indent=2,
        ).encode()
        try:
            with path.open("xb") as output:
                output.write(envelope)
        except FileExistsError:
            if path.read_bytes() != envelope:
                raise ValueError("An immutable raw snapshot path contains different content.")
        return reference

    def write(self, records: Iterable[CanonicalRecord]) -> None:
        grouped: dict[str, list[CanonicalRecord]] = {}
        for record in records:
            grouped.setdefault(record.entity_type, []).append(record)
        if not grouped:
            raise ValueError("At least one canonical record is required.")
        with duckdb.connect(str(self.database_path)) as connection:
            for entity_type, batch in grouped.items():
                table = TABLES[entity_type]
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    "id VARCHAR PRIMARY KEY, entity_type VARCHAR, schema_version VARCHAR, "
                    "event_time TIMESTAMPTZ, retrieved_at TIMESTAMPTZ, payload_json JSON)"
                )
                rows = [
                    (
                        record.id,
                        record.entity_type,
                        record.schema_version,
                        record.event_time,
                        record.retrieved_at,
                        record.model_dump_json(),
                    )
                    for record in batch
                ]
                connection.executemany(
                    f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?, ?, ?, ?)", rows
                )
                parquet_path = (self.canonical_dir / f"{table}.parquet").as_posix().replace("'", "''")
                connection.execute(
                    f"COPY (SELECT * FROM {table} ORDER BY id) TO '{parquet_path}' "
                    "(FORMAT PARQUET, OVERWRITE_OR_IGNORE TRUE)"
                )

    def load(self, entity_type: str, record_id: str) -> CanonicalRecord:
        table = TABLES[entity_type]
        with duckdb.connect(str(self.database_path), read_only=True) as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {table} WHERE id = ?", [record_id]
            ).fetchone()
        if row is None:
            raise KeyError(f"No {entity_type} record exists with ID {record_id}.")
        return load_canonical_record(row[0])

    def load_all(self, entity_type: str) -> list[CanonicalRecord]:
        """Load every record of one entity type, for callers that need to filter
        in Python (e.g. by kickoff time) rather than by an indexed SQL column.

        Returns [] both when the database file has never been created yet
        (nothing has ever been written to this store) and when the file
        exists but this entity's table hasn't -- a caller ingesting one
        entity type (e.g. closing lines) before any of another type (e.g.
        games) has ever been written must see an empty list, not a crash.
        """
        table = TABLES[entity_type]
        if not self.database_path.exists():
            return []
        with duckdb.connect(str(self.database_path), read_only=True) as connection:
            try:
                rows = connection.execute(f"SELECT payload_json FROM {table}").fetchall()
            except duckdb.CatalogException:
                return []
        return [load_canonical_record(row[0]) for row in rows]

    def lineage_for_recommendation(self, recommendation_id: str) -> CanonicalLineage:
        recommendation = self.load("paper_recommendation", recommendation_id)
        assert isinstance(recommendation, PaperRecommendation)
        match_decision = self.load("match_decision", recommendation.match_decision_id)
        assert isinstance(match_decision, MatchDecision)
        quote = self.load("orderbook_snapshot", recommendation.quote_snapshot_id)
        forecast = self.load("forecast", match_decision.forecast_id)
        game = self.load("game", match_decision.game_id)
        market = self.load("kalshi_market", match_decision.market_id)
        outcome = self._find_outcome(match_decision.game_id, match_decision.market_id)
        return CanonicalLineage(
            game=game,
            forecast=forecast,
            market=market,
            quote=quote,
            match_decision=match_decision,
            recommendation=recommendation,
            outcome=outcome,
        )

    def _find_outcome(self, game_id: str, market_id: str) -> Outcome | None:
        try:
            with duckdb.connect(str(self.database_path), read_only=True) as connection:
                row = connection.execute(
                    "SELECT payload_json FROM outcomes "
                    "WHERE json_extract_string(payload_json, '$.game_id') = ? "
                    "AND json_extract_string(payload_json, '$.market_id') = ?",
                    [game_id, market_id],
                ).fetchone()
        except duckdb.CatalogException:
            return None
        if row is None:
            return None
        outcome = load_canonical_record(row[0])
        assert isinstance(outcome, Outcome)
        return outcome
