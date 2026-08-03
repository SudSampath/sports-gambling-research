from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when a command cannot run safely with the current local settings."""


_PLACEHOLDER_VALUES = frozenset(
    {"", "replace_me", "changeme", "your_key_here", "your_secret_here", "path/to/kalshi-key.pem"}
)


def _is_placeholder(value: SecretStr | str) -> bool:
    raw_value = value.get_secret_value() if isinstance(value, SecretStr) else value
    return raw_value.strip().replace("\\", "/").casefold() in _PLACEHOLDER_VALUES


def _require_secret(value: SecretStr, provider: str, environment_variable: str) -> str:
    if _is_placeholder(value):
        raise ConfigurationError(
            f"{provider} credentials are required. Set {environment_variable} in your local .env; "
            "placeholder values are not accepted."
        )
    return value.get_secret_value()


class Settings(BaseSettings):
    # Demo is the safe default. A production endpoint requires an explicit local
    # override after the user has intentionally chosen to use one.
    kalshi_api_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"

    # Kalshi issues an API key ID plus an RSA private key. The key ID is not
    # secret, but the private key is: it signs every authenticated request.
    kalshi_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    kalshi_private_key_path: Path | None = None
    kalshi_private_key_pem: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    kalshi_private_key_passphrase: SecretStr = Field(
        default_factory=lambda: SecretStr(""), repr=False
    )

    theodds_api_base_url: str = "https://api.the-odds-api.com/v4"
    theodds_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)

    sportsdata_api_base_url: str = "https://api.sportsdata.io/v3"
    sportsdata_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)

    default_bankroll: float = 10000.0
    risk_per_trade: float = 0.01

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def require_kalshi_key_material(self) -> tuple[str, bytes, bytes | None]:
        """Return (api_key_id, private_key_pem, passphrase) for request signing.

        Fails before any authenticated request is attempted. Returns raw material
        rather than a signer so this module stays free of crypto imports, which
        also keeps ``sgr.config`` importable without ``sgr.connectors``.
        """
        api_key_id = _require_secret(self.kalshi_api_key, "Kalshi", "KALSHI_API_KEY")

        if not _is_placeholder(self.kalshi_private_key_pem):
            pem = self.kalshi_private_key_pem.get_secret_value().encode()
        elif self.kalshi_private_key_path is not None:
            key_path = self.kalshi_private_key_path.expanduser()
            if _is_placeholder(str(key_path)):
                raise ConfigurationError(
                    "Kalshi private-key paths must not use placeholder values. Set "
                    "KALSHI_PRIVATE_KEY_PATH to an absolute path outside this repository."
                )
            if not key_path.is_absolute():
                raise ConfigurationError(
                    "KALSHI_PRIVATE_KEY_PATH must be an absolute path outside this repository."
                )
            if not key_path.is_file():
                raise ConfigurationError(
                    f"Kalshi private key not found at {key_path}. Point "
                    "KALSHI_PRIVATE_KEY_PATH at the PEM file downloaded when you created "
                    "the API key."
                )
            pem = key_path.read_bytes()
        else:
            raise ConfigurationError(
                "Kalshi request signing requires a private key. Set KALSHI_PRIVATE_KEY_PATH "
                "to the PEM file issued with your API key, or KALSHI_PRIVATE_KEY_PEM to its "
                "contents when loading from a managed secret store."
            )

        passphrase = (
            None
            if _is_placeholder(self.kalshi_private_key_passphrase)
            else self.kalshi_private_key_passphrase.get_secret_value().encode()
        )
        return api_key_id, pem, passphrase

    def require_theodds_api_key(self) -> str:
        return _require_secret(self.theodds_api_key, "The Odds API", "THEODDS_API_KEY")

    def require_sportsdata_api_key(self) -> str:
        return _require_secret(self.sportsdata_api_key, "SportsData", "SPORTSDATA_API_KEY")


settings = Settings()
