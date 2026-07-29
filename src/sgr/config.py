from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(RuntimeError):
    """Raised when a command cannot run safely with the current local settings."""


_PLACEHOLDER_VALUES = frozenset({"", "replace_me", "changeme", "your_key_here", "your_secret_here"})


def _is_placeholder(value: SecretStr) -> bool:
    return value.get_secret_value().strip().casefold() in _PLACEHOLDER_VALUES


def _require_secret(value: SecretStr, provider: str, environment_variable: str) -> str:
    if _is_placeholder(value):
        raise ConfigurationError(
            f"{provider} credentials are required. Set {environment_variable} in your local .env; "
            "placeholder values are not accepted."
        )
    return value.get_secret_value()


class Settings(BaseSettings):
    kalshi_api_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)
    kalshi_api_secret: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)

    theodds_api_base_url: str = "https://api.the-odds-api.com/v4"
    theodds_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)

    sportsdata_api_base_url: str = "https://api.sportsdata.io/v3"
    sportsdata_api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), repr=False)

    default_bankroll: float = 10000.0
    risk_per_trade: float = 0.01

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def require_kalshi_read_only_credentials(self) -> tuple[str, str]:
        """Return Kalshi credentials or fail before an authenticated request is attempted."""
        if _is_placeholder(self.kalshi_api_key) or _is_placeholder(self.kalshi_api_secret):
            raise ConfigurationError(
                "Kalshi read-only credentials are required. Set KALSHI_API_KEY and "
                "KALSHI_API_SECRET in your local .env; placeholder values are not accepted."
            )

        return (
            self.kalshi_api_key.get_secret_value(),
            self.kalshi_api_secret.get_secret_value(),
        )

    def require_theodds_api_key(self) -> str:
        return _require_secret(self.theodds_api_key, "The Odds API", "THEODDS_API_KEY")

    def require_sportsdata_api_key(self) -> str:
        return _require_secret(self.sportsdata_api_key, "SportsData", "SPORTSDATA_API_KEY")


settings = Settings()
