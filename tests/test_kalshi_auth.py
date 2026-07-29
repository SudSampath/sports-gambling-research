from __future__ import annotations

import asyncio
import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from sgr.config import Settings
from sgr.connectors.base import APIConnector
from sgr.connectors.kalshi import KalshiConnector
from sgr.connectors.kalshi_auth import KalshiSignatureError, KalshiSigner

MARKETS_PATH = "/trade-api/v2/markets"

# Generated once: 2048-bit RSA keygen is the slowest part of this module.
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_bytes(private_key: rsa.RSAPrivateKey, passphrase: bytes | None = None) -> bytes:
    encryption = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=encryption,
    )


def _verify(headers: dict[str, str], method: str, path: str) -> None:
    """Verify a signature the way Kalshi's server would."""
    message = KalshiSigner.signed_message(headers["KALSHI-ACCESS-TIMESTAMP"], method, path)
    _PRIVATE_KEY.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_signature_verifies_against_public_key():
    signer = KalshiSigner.from_pem("key-id-1", _pem_bytes(_PRIVATE_KEY))

    headers = signer.headers("GET", MARKETS_PATH)

    assert headers["KALSHI-ACCESS-KEY"] == "key-id-1"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    _verify(headers, "GET", MARKETS_PATH)


def test_encrypted_pem_loads_with_passphrase():
    pem = _pem_bytes(_PRIVATE_KEY, passphrase=b"local-passphrase")

    signer = KalshiSigner.from_pem("key-id-1", pem, b"local-passphrase")

    _verify(signer.headers("GET", MARKETS_PATH), "GET", MARKETS_PATH)


def test_encrypted_pem_without_passphrase_is_rejected():
    pem = _pem_bytes(_PRIVATE_KEY, passphrase=b"local-passphrase")

    with pytest.raises(KalshiSignatureError, match="KALSHI_PRIVATE_KEY_PASSPHRASE"):
        KalshiSigner.from_pem("key-id-1", pem, None)


def test_unparseable_pem_error_excludes_key_material():
    pem = b"-----BEGIN PRIVATE KEY-----\nnot-a-real-key-sentinel\n-----END PRIVATE KEY-----\n"

    with pytest.raises(KalshiSignatureError) as error:
        KalshiSigner.from_pem("key-id-1", pem)

    assert "not-a-real-key-sentinel" not in str(error.value)


def test_non_rsa_key_is_rejected():
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    with pytest.raises(KalshiSignatureError, match="requires an RSA private key"):
        KalshiSigner.from_pem("key-id-1", pem)


def test_different_paths_produce_different_signatures():
    signer = KalshiSigner.from_pem("key-id-1", _pem_bytes(_PRIVATE_KEY))
    balance_path = "/trade-api/v2/portfolio/balance"

    markets = signer.headers("GET", MARKETS_PATH)
    balance = signer.headers("GET", balance_path)

    assert markets["KALSHI-ACCESS-SIGNATURE"] != balance["KALSHI-ACCESS-SIGNATURE"]
    _verify(markets, "GET", MARKETS_PATH)
    _verify(balance, "GET", balance_path)


def test_repr_redacts_key_id():
    signer = KalshiSigner.from_pem("key-id-1", _pem_bytes(_PRIVATE_KEY))

    assert "key-id-1" not in repr(signer)


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, list[str]]:
        return {"markets": []}


class _FakeClient:
    """Captures the outbound request instead of performing it."""

    last_call: dict[str, object] = {}

    def __init__(self, **_: object) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None):
        type(self).last_call = {"url": url, "params": params, "headers": headers}
        return _FakeResponse()


class _RecordingConnector(APIConnector):
    def __init__(self, base_url: str) -> None:
        super().__init__(base_url=base_url)
        self.signed: list[tuple[str, str]] = []

    def request_headers(self, method: str, path: str) -> dict[str, str]:
        self.signed.append((method, path))
        return {}


def test_signed_path_excludes_query_string(monkeypatch):
    monkeypatch.setattr("sgr.connectors.base.httpx.AsyncClient", _FakeClient)
    connector = _RecordingConnector("https://api.elections.kalshi.com/trade-api/v2")

    asyncio.run(connector.get_json("markets", params={"limit": 5}))

    # Kalshi signs the path only; params must travel outside the signed URL.
    assert connector.signed == [("GET", MARKETS_PATH)]
    assert _FakeClient.last_call["params"] == {"limit": 5}
    assert "limit" not in str(_FakeClient.last_call["url"])


def test_connector_signs_outbound_request(monkeypatch):
    monkeypatch.setattr("sgr.connectors.base.httpx.AsyncClient", _FakeClient)
    configured_settings = Settings(
        _env_file=None,
        kalshi_api_key="key-id-1",
        kalshi_private_key_pem=_pem_bytes(_PRIVATE_KEY).decode(),
    )
    connector = KalshiConnector(configured_settings=configured_settings)

    asyncio.run(connector.list_markets(limit=5))

    headers = _FakeClient.last_call["headers"]
    assert headers["Accept"] == "application/json"
    _verify(headers, "GET", MARKETS_PATH)
