from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class KalshiSnapshotRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_path: str = Field(min_length=1)
    metadata_path: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    retrieved_at: datetime
    normalization_version: str = Field(min_length=1)


class KalshiSnapshotStore:
    """Content-addressed raw response and normalization metadata storage."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def persist(
        self,
        *,
        endpoint: str,
        payload: dict[str, Any],
        retrieved_at: datetime,
        normalization_version: str,
    ) -> KalshiSnapshotRef:
        if retrieved_at.tzinfo is None or retrieved_at.utcoffset() is None:
            raise ValueError("retrieved_at must be timezone-aware")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        raw_dir = self.root / "raw"
        metadata_dir = self.root / "metadata"
        raw_dir.mkdir(parents=True, exist_ok=True)
        metadata_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{digest}.json"
        observation = hashlib.sha256(
            f"{digest}\x1f{endpoint}\x1f{retrieved_at.isoformat()}".encode("utf-8")
        ).hexdigest()
        metadata_path = metadata_dir / f"{observation}.json"
        if raw_path.exists() and raw_path.read_bytes() != raw:
            raise RuntimeError("Content-addressed Kalshi snapshot collision.")
        if not raw_path.exists():
            raw_path.write_bytes(raw)
        metadata = {
            "endpoint": endpoint,
            "normalization_version": normalization_version,
            "retrieved_at": retrieved_at.isoformat(),
            "sha256": digest,
        }
        encoded_metadata = json.dumps(metadata, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        if metadata_path.exists() and metadata_path.read_bytes() != encoded_metadata:
            raise RuntimeError("A Kalshi observation cannot have conflicting normalization metadata.")
        if not metadata_path.exists():
            metadata_path.write_bytes(encoded_metadata)
        return KalshiSnapshotRef(
            sha256=digest,
            raw_path=str(raw_path),
            metadata_path=str(metadata_path),
            endpoint=endpoint,
            retrieved_at=retrieved_at,
            normalization_version=normalization_version,
        )
