from __future__ import annotations

import base64
import time

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from sgr.config import ConfigurationError

ACCESS_KEY_HEADER = "KALSHI-ACCESS-KEY"
ACCESS_TIMESTAMP_HEADER = "KALSHI-ACCESS-TIMESTAMP"
ACCESS_SIGNATURE_HEADER = "KALSHI-ACCESS-SIGNATURE"


class KalshiSignatureError(ConfigurationError):
    """Raised when a Kalshi private key cannot be loaded or used for signing.

    Subclasses ConfigurationError so the CLI reports an unusable key as a
    configuration problem rather than an unhandled traceback.
    """


class KalshiSigner:
    """Signs Kalshi trade-api/v2 requests.

    Kalshi authenticates each request individually rather than with a static
    token: the caller signs ``timestamp + method + path`` with the RSA private
    key issued alongside the API key ID. Because the timestamp and path are part
    of the signed message, these headers cannot be computed once in a constructor
    and reused -- see ``APIConnector.request_headers``.
    """

    def __init__(self, api_key_id: str, private_key: rsa.RSAPrivateKey) -> None:
        self._api_key_id = api_key_id
        self._private_key = private_key

    @classmethod
    def from_pem(
        cls,
        api_key_id: str,
        pem_data: bytes,
        passphrase: bytes | None = None,
    ) -> KalshiSigner:
        """Load a signer from PEM bytes.

        Raises KalshiSignatureError without echoing key material -- a traceback
        containing the private key would defeat the point of protecting it.
        """
        try:
            private_key = serialization.load_pem_private_key(pem_data, password=passphrase)
        except (ValueError, TypeError, UnsupportedAlgorithm) as error:
            raise KalshiSignatureError(
                "Kalshi private key could not be loaded. Confirm the file is the unmodified "
                "PEM issued by Kalshi, and that KALSHI_PRIVATE_KEY_PASSPHRASE is set if the "
                f"key is encrypted ({type(error).__name__})."
            ) from None

        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise KalshiSignatureError(
                "Kalshi requires an RSA private key; the supplied PEM contains a "
                f"{type(private_key).__name__}."
            )

        return cls(api_key_id=api_key_id, private_key=private_key)

    @staticmethod
    def signed_message(timestamp_ms: str, method: str, path: str) -> bytes:
        """Build the exact byte string Kalshi expects to have been signed.

        ``path`` must be the full request path including the ``/trade-api/v2``
        prefix, and must exclude the query string.
        """
        return f"{timestamp_ms}{method.upper()}{path}".encode()

    def headers(self, method: str, path: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        message = self.signed_message(timestamp_ms, method, path)
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            ACCESS_KEY_HEADER: self._api_key_id,
            ACCESS_TIMESTAMP_HEADER: timestamp_ms,
            ACCESS_SIGNATURE_HEADER: base64.b64encode(signature).decode("ascii"),
        }

    def __repr__(self) -> str:
        """Redacted so a logged connector never carries key material."""
        return f"{type(self).__name__}(api_key_id=<redacted>)"
