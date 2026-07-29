from __future__ import annotations

import pytest

from sgr.config import ConfigurationError, Settings
from sgr.connectors.kalshi import KalshiConnector

# Distinctive so a leak into an error message is unambiguous.
_LEAK_SENTINEL = "sentinel-should-never-appear-in-an-error"


def test_missing_kalshi_api_key_names_the_variable():
    configured_settings = Settings(_env_file=None)

    with pytest.raises(ConfigurationError, match="KALSHI_API_KEY"):
        configured_settings.require_kalshi_key_material()


def test_placeholder_api_key_is_rejected():
    configured_settings = Settings(_env_file=None, kalshi_api_key="replace_me")

    with pytest.raises(ConfigurationError, match="placeholder values are not accepted"):
        configured_settings.require_kalshi_key_material()


def test_api_key_without_private_key_explains_both_options():
    configured_settings = Settings(_env_file=None, kalshi_api_key="key-id-1")

    with pytest.raises(ConfigurationError, match="KALSHI_PRIVATE_KEY_PATH") as error:
        configured_settings.require_kalshi_key_material()

    assert "KALSHI_PRIVATE_KEY_PEM" in str(error.value)


def test_placeholder_private_key_path_is_treated_as_unset():
    configured_settings = Settings(
        _env_file=None,
        kalshi_api_key="key-id-1",
        kalshi_private_key_path="path/to/kalshi-key.pem",
    )

    # The value shipped in .env.example must not be mistaken for a real path.
    with pytest.raises(ConfigurationError, match="Kalshi private key not found"):
        configured_settings.require_kalshi_key_material()


def test_missing_private_key_file_reports_the_path(tmp_path):
    missing = tmp_path / "absent.pem"
    configured_settings = Settings(
        _env_file=None, kalshi_api_key="key-id-1", kalshi_private_key_path=missing
    )

    with pytest.raises(ConfigurationError, match="Kalshi private key not found"):
        configured_settings.require_kalshi_key_material()


def test_private_key_file_material_is_read(tmp_path):
    key_file = tmp_path / "kalshi-key.pem"
    key_file.write_bytes(b"pem-content")
    configured_settings = Settings(
        _env_file=None, kalshi_api_key="key-id-1", kalshi_private_key_path=key_file
    )

    api_key_id, pem, passphrase = configured_settings.require_kalshi_key_material()

    assert (api_key_id, pem, passphrase) == ("key-id-1", b"pem-content", None)


def test_inline_pem_takes_precedence_over_path(tmp_path):
    key_file = tmp_path / "kalshi-key.pem"
    key_file.write_bytes(b"from-disk")
    configured_settings = Settings(
        _env_file=None,
        kalshi_api_key="key-id-1",
        kalshi_private_key_path=key_file,
        kalshi_private_key_pem="from-secret-store",
    )

    _, pem, _ = configured_settings.require_kalshi_key_material()

    assert pem == b"from-secret-store"


def test_placeholder_provider_key_is_not_echoed():
    configured_settings = Settings(_env_file=None, theodds_api_key="replace_me")

    with pytest.raises(ConfigurationError, match="THEODDS_API_KEY") as error:
        configured_settings.require_theodds_api_key()

    assert "replace_me" not in str(error.value)


def test_plaintext_secret_is_never_echoed_in_errors():
    """Guards the plaintext, not SecretStr's mask.

    Asserting the mask is absent would pass even if the raw value leaked, so this
    checks for the secret itself: SportsData is asked for a key it does not have
    while a real-looking value sits in a sibling field.
    """
    configured_settings = Settings(
        _env_file=None,
        theodds_api_key=_LEAK_SENTINEL,
        sportsdata_api_key="replace_me",
    )

    with pytest.raises(ConfigurationError) as error:
        configured_settings.require_sportsdata_api_key()

    assert _LEAK_SENTINEL not in str(error.value)
    assert _LEAK_SENTINEL not in repr(configured_settings)


def test_connector_fails_before_any_request_without_credentials():
    configured_settings = Settings(_env_file=None)

    with pytest.raises(ConfigurationError):
        KalshiConnector(configured_settings=configured_settings)
