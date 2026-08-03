# Sports Gambling Research

A public, research-only Python project for evaluating NFL event-market signals. The first milestone combines a transparent Pythagorean-expectation baseline with read-only ESPN game data and Kalshi market snapshots.

## Goals

- Build reproducible, point-in-time NFL win-probability research—not an autonomous execution system.
- Compare independently generated model probabilities with equivalent Kalshi contracts after accounting for quotes, fees, liquidity, timing, and settlement rules.
- Backtest using only information available before a simulated decision time; measure calibration, log loss, Brier score, and friction-aware paper-trade performance.
- Record data provenance, model version, inputs, and rejection reasons so every recommendation can be recreated.

## Non-goals for the first milestone

- Real-money order submission, modification, cancellation, or settlement.
- Storage of API keys, private keys, tokens, cookies, account IDs, or other credentials in source control.
- Claims of guaranteed profit or betting advice.

See [DEVELOPMENT.md](DEVELOPMENT.md) for contribution, test, security, and Claude Code review expectations.

## What is included

- Kalshi connector for market snapshots.
- Sports API connectors (The Odds API + SportsData starter).
- Strategy stubs:
  - `ValueStrategy`
  - `ArbitrageStrategy`
  - `ConsensusDivergenceStrategy`
  - `MomentumStrategy`
- Basic binary-outcome backtest engine.
- Risk sizing helpers (Kelly fraction + position sizing).
- CLI commands for demo pipelines and live Kalshi fetch.
- Pytest starter tests.

## Quickstart

```powershell
cd sports-gambling-research
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Keep `.env` local: it is ignored by Git and must never be committed. The placeholder values in `.env.example` deliberately fail before an authenticated request is attempted.

### Kalshi credentials

Kalshi signs every request rather than accepting a static token, so creating an API key gives you two things:

1. an **API key ID** — set it as `KALSHI_API_KEY`
2. an **RSA private key**, downloaded once as a `.pem` file — point `KALSHI_PRIVATE_KEY_PATH` at it

The private key signs `timestamp + method + path` on each request. Store the `.pem` outside the repository at an absolute path; `.gitignore` is only a backstop, never a storage strategy. It is only downloadable at creation time, so losing it means issuing a new key. Set `KALSHI_PRIVATE_KEY_PEM` instead of the path when loading from a managed secret store, and `KALSHI_PRIVATE_KEY_PASSPHRASE` if you encrypted the key yourself.

For research and paper trading, point `KALSHI_API_BASE_URL` at the demo environment (`https://demo-api.kalshi.co/trade-api/v2`) so no production account is reachable. The connector layer only implements `GET`, so there is no order-submission path.

Run demos:

```bash
python -m sgr.cli demo-value
python -m sgr.cli demo-momentum
python -m sgr.cli demo-backtest
```

Run live Kalshi market fetch:

```bash
python -m sgr.cli kalshi-markets --limit 10
```

Run tests:

```bash
pytest
```

## Project structure

```text
sports-gambling-research/
  src/sgr/
    connectors/
    algorithms/
    backtest/
    risk/
    cli.py
  tests/
  .env.example
  pyproject.toml
  requirements.txt
```

## Notes on production usage

- This repository is for research and experimentation.
- Real-money betting has legal, regulatory, and financial risk.
- Validate API terms, market rules, and regional laws before deployment.
- Add robust monitoring, execution safeguards, slippage modeling, and strict bankroll controls before live use.
