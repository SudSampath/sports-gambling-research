# Sports Gambling Research

A public, research-only Python project for evaluating NFL event-market signals. The first milestone combines a transparent Pythagorean-expectation baseline with read-only ESPN game data and Kalshi market snapshots.

## Goals

- Build reproducible, point-in-time NFL win-probability research—not an autonomous execution system.
- Compare independently generated model probabilities with equivalent Kalshi contracts after accounting for quotes, fees, liquidity, timing, and settlement rules.
- Backtest using only information available before a simulated decision time; measure calibration, log loss, Brier score, and friction-aware paper-trade performance.
- Record data provenance, model version, inputs, and rejection reasons so every recommendation can be recreated.

## Model and methodology

The win-probability model is a Pythagorean expectation (`src/sgr/research/pythagorean.py`): each team's strength is `points_for^x / (points_for^x + points_against^x)`, blended toward its prior season early on so a small in-season sample doesn't dominate. Two teams' strengths combine into a win probability via the log5 formula, plus a home-field adjustment. Every forecast is generated from a required, explicit `feature_cutoff_at` and only ever reads games completed strictly before it (`src/sgr/research/evaluation.py` walk-forward evaluates this chronologically, so a rerun with a later cutoff never changes an earlier prediction).

An opt-in roster-continuity variant weights returning players by prior-season
snaps and regresses extreme prior scoring rates toward league average when
continuity is low. It leaves the default model unchanged because its 2025 game
probability gain was not statistically significant. See the
[2026-08-26 research note](docs/research/roster-continuity-2026-08-26.md) and
the `ingest-roster-continuity`, `compare-roster-continuity`, and
`project-roster-continuity` CLI commands.

Built on top of that baseline:

- **Win totals** (`win_totals.py`): each team's expected season win total is the exact sum of its per-game win probabilities (linearity of expectation, no simulation), with a variance-based confidence band.
- **Expected margin** (`margin.py`): a point-spread estimate from the same blended points-for/against, plus a home-field points term calibrated from real data (not assumed).
- **Season simulation** (`season_simulation.py`): a seeded Monte Carlo simulation for playoff odds, division-win odds, and the joint probability of a user-specified set of outcomes -- reserved for the questions a single team's expected value can't answer.
- **Player-impact / injuries** (`player_impact.py`, `injury_adjustment.py`): a replacement-aware estimate of a missing usual starter's win-probability impact, wired into every forecast by default. It only fires when a player resolves to a *confirmed* OUT/INACTIVE status from independently corroborating sources (a single uncorroborated report, e.g. from one provider, is treated as unconfirmed and does not trigger it) -- see `injury_ingest.py` for how current injury reports are fetched, always for not-yet-played games only, never backfilled against completed ones (that would misrepresent today's report as having been knowable in the past).

### What was tried and did not help

Two further adjustments were researched, implemented, and walk-forward evaluated against real 2023-2025 data, and are kept in the codebase as documented, tested, but **unused** baselines (`evaluation.py`'s `turnover_normalized` and `sos_adjusted` entries) rather than defaults, because they measurably hurt out-of-sample accuracy:

- **Turnover normalization** (`turnover_adjustment.py`): discounting scoring by a real-data-calibrated points-per-turnover-margin rate. Roughly flat to slightly worse than the unadjusted baseline on held-out 2025 data.
- **Strength-of-schedule adjustment** (`sos_adjustment.py`): scaling points-for/against by opponent strength relative to the league average. Clearly worse than baseline on held-out 2025 data -- current-season-only opponent samples are too small (often 1-4 games) for the adjustment to separate real signal from noise.

A combined "blend" of injuries + turnover + SOS was also evaluated (`candidate_comparison.py`) and does not beat the injury adjustment alone, since SOS's damage outweighs the other two. See the closed SUD-108/109/110/111 tickets for the full real-data comparison tables and reasoning.

## Non-goals for the first milestone

- Real-money order submission, modification, cancellation, or settlement.
- Storage of API keys, private keys, tokens, cookies, account IDs, or other credentials in source control.
- Claims of guaranteed profit or betting advice.

See [DEVELOPMENT.md](DEVELOPMENT.md) for contribution, test, security, and Claude Code review expectations.

The current implementation PRD (draft) is at [docs/PRD.md](docs/PRD.md).

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
