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

The current implementation PRD (draft) is at [docs/PRD.md](docs/PRD.md).

## What is included

- Unauthenticated, read-only Kalshi connector for NFL series, events, markets,
  executable orderbooks, and historical market snapshots.
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

Keep `.env` local: it is ignored by Git and must never be committed. Kalshi's
public REST market-data endpoints require no credentials, and the Phase 1
connector never reads the reserved Kalshi credential placeholders. Other
providers still require their documented local keys.

See [docs/kalshi-market-data.md](docs/kalshi-market-data.md) for the public API
contract, replay storage, and quote rejection rules.

### Kalshi credentials (reserved, not used in Phase 1)

Kalshi signs authenticated requests rather than accepting a static token, so creating an API key gives you an **API key ID** (`KALSHI_API_KEY`) and an **RSA private key** downloaded once as a `.pem` file (`KALSHI_PRIVATE_KEY_PATH`). The signing implementation lives in `sgr.connectors.kalshi_auth` and is retained for a future authenticated milestone; nothing on the Phase 1 read-only path calls it.

If that milestone is enabled, store the `.pem` outside the repository at an absolute path — `.gitignore` is a backstop, never a storage strategy — and point `KALSHI_API_BASE_URL` at the demo environment (`https://demo-api.kalshi.co/trade-api/v2`) so no production account is reachable. Use `KALSHI_PRIVATE_KEY_PEM` instead of the path when loading from a managed secret store, and `KALSHI_PRIVATE_KEY_PASSPHRASE` if the key is encrypted.

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
pytest -m bdd
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
