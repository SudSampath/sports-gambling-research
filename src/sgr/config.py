from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    kalshi_api_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_api_key: str = ""
    kalshi_api_secret: str = ""

    theodds_api_base_url: str = "https://api.the-odds-api.com/v4"
    theodds_api_key: str = ""

    sportsdata_api_base_url: str = "https://api.sportsdata.io/v3"
    sportsdata_api_key: str = ""

    default_bankroll: float = 10000.0
    risk_per_trade: float = 0.01

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
