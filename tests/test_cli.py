from typer.testing import CliRunner

from sgr.cli import app
from sgr.config import ConfigurationError


def test_kalshi_configuration_error_is_safe_and_has_no_traceback(monkeypatch):
    class MissingCredentialsConnector:
        def __init__(self) -> None:
            raise ConfigurationError("Set KALSHI_API_KEY in your local .env")

    monkeypatch.setattr("sgr.cli.KalshiConnector", MissingCredentialsConnector)

    result = CliRunner().invoke(app, ["kalshi-markets"])

    assert result.exit_code == 2
    assert "KALSHI_API_KEY" in result.output
    assert "Traceback" not in result.output
