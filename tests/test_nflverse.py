from __future__ import annotations

import asyncio
import hashlib
import ssl
from pathlib import Path

import pytest

from sgr.connectors.nflverse import NflverseConnector, NflverseSchemaError


SNAP_CSV = (
    b"season,game_type,player,pfr_player_id,position,team,offense_snaps,defense_snaps\n"
    b"2025,REG,Player One,P1,QB,BUF,100,0\n"
)


def _write_cached_snapshot(root: Path, raw: bytes = SNAP_CSV) -> Path:
    checksum = hashlib.sha256(SNAP_CSV).hexdigest()
    directory = root / "snap_counts" / "2025"
    directory.mkdir(parents=True)
    path = directory / f"20260826T120000000000Z-{checksum}.csv"
    path.write_bytes(raw)
    return path


def test_connector_uses_verified_system_tls_context(tmp_path):
    connector = NflverseConnector(cache_dir=tmp_path)

    assert isinstance(connector.verify, ssl.SSLContext)
    assert connector.verify.check_hostname is True
    assert connector.verify.verify_mode == ssl.CERT_REQUIRED


def test_cached_csv_retains_checksum_and_source_provenance(tmp_path):
    path = _write_cached_snapshot(tmp_path)
    connector = NflverseConnector(cache_dir=tmp_path)

    snapshot = asyncio.run(connector.snap_counts(2025))

    assert snapshot.rows[0]["pfr_player_id"] == "P1"
    assert snapshot.source.path == path.as_posix()
    assert snapshot.source.sha256 == hashlib.sha256(SNAP_CSV).hexdigest()
    assert snapshot.source.source_url.endswith("/snap_counts/snap_counts_2025.csv")


def test_tampered_cached_csv_is_rejected(tmp_path):
    _write_cached_snapshot(tmp_path, raw=SNAP_CSV + b"2025,REG,Player Two,P2,WR,BUF,1,0\n")
    connector = NflverseConnector(cache_dir=tmp_path)

    with pytest.raises(NflverseSchemaError, match="checksum does not match"):
        asyncio.run(connector.snap_counts(2025))
