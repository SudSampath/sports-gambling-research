from __future__ import annotations

import pytest

from sgr.config import ConfigurationError, Settings
from sgr.connectors.kalshi import KalshiConnector


def test_missing_kalshi_credentials_fail_without_revealing_values():
    configured_settings = Settings(_env_file=None)

    with pytest.raises(ConfigurationError, match="KALSHI_API_KEY and KALSHI_API_SECRET") as error:
        configured_settings.require_kalshi_read_only_credentials()

    assert "**********" not in str(error.value)


def test_placeholder_kalshi_credentials_are_rejected():
    configured_settings = Settings(
        _env_file=None,
        kalshi_api_key="replace_me",
        kalshi_api_secret="your_secret_here",
    )

    with pytest.raises(ConfigurationError, match="placeholder values are not accepted"):
        configured_settings.require_kalshi_read_only_credentials()


def test_other_provider_placeholders_are_rejected_without_exposing_values():
    configured_settings = Settings(_env_file=None, theodds_api_key="replace_me")

    with pytest.raises(ConfigurationError, match="THEODDS_API_KEY") as error:
        configured_settings.require_theodds_api_key()

    assert "replace_me" not in str(error.value)


def test_valid_kalshi_credentials_can_initialize_read_only_connector():
    configured_settings = Settings(
        _env_file=None,
        kalshi_api_key="local-read-only-key",
        kalshi_api_secret="local-read-only-secret",
    )

    connector = KalshiConnector(configured_settings=configured_settings)

    assert connector.headers["KALSHI-ACCESS-KEY"] == "local-read-only-key"


def test_connector_rejects_missing_credentials_before_any_request():
    configured_settings = Settings(_env_file=None)

    with pytest.raises(ConfigurationError):
        KalshiConnector(configured_settings=configured_settings)
