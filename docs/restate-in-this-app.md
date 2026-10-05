# Durable Polymarket research and fictional paper execution

This tool uses public market data and a fictional bankroll. There are no live
order endpoints, signing, wallet funding, account imports, or location bypasses.
Eligible decisions paper-execute within a fixed policy without per-candidate
approval. Eligibility does not guarantee a fill.

## Bounded operation

```sh
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/bootstrap_restate.py
export RESTATE_SERVER="$PWD/.tools/restate-server-aarch64-apple-darwin/restate-server"
.venv/bin/python -m sgr.paper.runtime --seconds 180
```

Use the platform path printed by bootstrap on other platforms. In a second
terminal, with the same checkout and environment:

```sh
.venv/bin/python -m sgr.cli campaign start --id public-screen \
  --candidates 500 --pages 5 --requests 20 --seconds 60 --wait
.venv/bin/python -m sgr.cli campaign status public-screen
.venv/bin/python -m sgr.cli campaign report public-screen \
  --out data/reports/public-screen.json
.venv/bin/python -m sgr.cli campaign pause public-screen
.venv/bin/python -m sgr.cli campaign resume public-screen
.venv/bin/python -m sgr.cli campaign replay public-screen
```

The runner stops its own processes at the deadline or on Ctrl-C. Restart with
the same `--state` and `--data` to recover. It refuses occupied ports; it never
terminates another session. No unattended service or paid feed is started.
Status/reporting work offline. Resume reconciles through the serialized risk
gate and needs Restate running. Replay retrieves existing decisions/checkpoints;
it does not reopen expired decisions or collect new evidence. A new analysis
requires a new campaign ID and observations. Journals and completed trade state
have 30-day retention;
independent catalog and ledger deduplication continue beyond it.

Campaign limits: 10,000 evaluations, 100 pages, 1,000 public attempts, 600 seconds,
fan-out 16, and two scheduled rechecks. Rechecks admit at most 128 previously
supported contracts and consume remaining candidate budget. Public attempts
consume a persistent budget even if the response is lost. These are admission
bounds: admitted RPCs, bounded retries, and reconciliation can finish afterward,
but new capital/fills are blocked at the deadline. Pause is checked between
batches and at the risk gate. Provider failure, budget exhaustion, and deadlines
produce explicit partial/terminal statuses.

Use `--spec` with an ignored JSON file containing `CampaignSpec` and audited
`definitions` to price supported contracts. `ContractDefinition` in
`src/sgr/research/contracts.py` gives the exact schema; `tests/paper_fixtures.py`
provides a synthetic example. Required evidence includes condition/assets,
complete settlement-text hash, cited rule quote/URL, outcome labels, resolution
source, game/team IDs, kickoff/deadline, overtime/tie/cancellation semantics, and
interpretation availability time. Titles do not establish equivalence.

Existing canonical games, teams, players, availability reports, and statlines in
`research/` beneath the campaign data directory supply the model. Each round freezes them with a
content digest. DuckDB retains observation versions, rejects same-time rewrites,
and selects only information available by cutoff. Conflicting final scores are
rejected even through intervening scheduled versions. Forecasts retain source,
model/calibration/strategy/rule versions and feature cutoff, and successful
campaigns batch-project forecasts into canonical storage.

The default public store is `data/polymarket/research/`. To reuse the existing
`data/research/` foundation, set `SGR_PAPER_ROOT=data` in both terminals and start
the runner with `--data data`. Keep the same root across restart and CLI calls.
Provider/revalidation failures pause execution until reconciliation and operator
resume; ordinary expiry or contract closure releases the remainder without a
portfolio-wide pause.

Current calibration excludes regular-season ties: only audited postseason game
winners are supported. Spreads, totals, props, champions, negative-risk markets,
ambiguous rules, and missing inputs remain unknown. The default discovery-only
campaign has no audited interpretations and cannot trade. Quotes/rules and
kickoff are checked again after simulated submission latency.

The research module evaluates calibrated NFL prices, audited equivalent
de-vigged/other-venue benchmark hypotheses, exhaustive-outcome costs after fees
and depth, and confirmed injury effects. Benchmark ingestion is not automated;
the analyzer accepts timestamped `Benchmark` inputs, and absent comparisons are
unknown. Injury adjustments reuse existing corroboration rules; no causal
price-response claim is made without timestamped history. Exhaustive pricing
discrepancies remain hypotheses; atomic multi-leg execution is unsupported.

```sh
.venv/bin/python -m sgr.cli campaign feed CANONICAL_MARKET_ID --seconds 20
.venv/bin/python -m sgr.cli campaign settlements public-screen --pass-id pass-1
```

The selected active binary market feed runs outside Restate, archives coherent
snapshots, and stops within 30 seconds. Gaps, invalid depth, reverse timestamps,
and changed metadata fail closed; reseed before reuse. Ticks do not invoke
workflows. Campaign decision intervals use bounded REST snapshots and execution
revalidation. Settlement makes one bounded pass of at most 20 positions, with
four public attempts per position/pass independent of discovery budget. Reuse a
pass ID to retry; a new pass rechecks unresolved outcomes. Durable trade state
supplies the decision and contract; the independent immutable trade-request
archive supplies them after workflow state expires. Only explicit final outcomes with matching
rule/source lineage realize P&L; disputed inventory retains exposure.

## Restate responsibilities

| Component | Durable responsibility | Independent safeguard |
| --- | --- | --- |
| ScanCampaign | Pagination/checkpoints, frozen inputs, bounded child analysis, recheck timers, partial completion | Immutable spec, persistent request budget, deduplicated reports |
| CandidateAnalysis | Snapshots, expensive forecasts, eligibility/rejection lineage | Stable IDs and immutable evidence |
| PaperTrade | Reservation, submission latency, partial expiry, reconciliation, settlement | Independently idempotent ledger effects |
| PaperPortfolio | Serialized risk/accounting per fictional portfolio | SQLite `BEGIN IMMEDIATE`, frozen policy, cash/exposure invariants |

External provider/catalog/compute/ledger effects are journaled; calls and timers
use Restate primitives. A lost fill acknowledgement triggers existing-fill
reconciliation before another simulated submission. Portfolio objects serialize
capital decisions, not ticks. Public HTTP intentionally uses a process-wide
serial cap plus connector cache/backoff; analysis fan-out is separately bounded.
See [execution assumptions](paper-execution.md) and [verified APIs](polymarket-data.md).

## Reproduce validation

```sh
.venv/bin/python -m pytest tests/bdd
.venv/bin/python -m pytest
.venv/bin/python -m sgr.cli --help
git diff --check
SGR_RUN_RESTATE_TESTS=1 .venv/bin/python -m pytest \
  tests/bdd/test_restate_steps.py -vv --tb=short
```

Set `RESTATE_SERVER` first. Native tests use fresh temporary state, stop their
own process groups in `finally`, and write ignored `.research/restate-evidence.json`.
Only the bounded public discovery case needs internet. Ordinary tests skip the
four explicit native scenarios; run both suites. HTTP fixture execution is
synthetic evidence, not a live trade. For a separate synthetic campaign:

```sh
.venv/bin/python -m sgr.cli campaign start --id stress-check --synthetic \
  --candidates 10000 --pages 100 --requests 1 --seconds 120 --fanout 8 --wait
.venv/bin/python -m sgr.cli campaign report stress-check
```

Synthetic portfolios/evidence are separate from public ones. No synthetic rows
count as live markets. Eligible fixture contracts share one event, intentionally
testing its exposure cap.

## Measurements and candid assessment

On an Apple Silicon Mac, Restate 1.7.13 / SDK 1.0.5 screened 10,000 synthetic
candidates in 30.18 seconds (331.3/second), found 50 eligible decisions, and opened
four paper positions. Public requests: zero; reserved capital returned to zero
and all accounting invariants held. Sampled peak process-group RSS: worker
262,112 KiB, runtime 383,136 KiB. Sampled CPU deltas: 14.12 and 1.45 seconds.
These are sampled process-group figures, not whole-machine peaks. Catalog size
63,594,496 bytes includes earlier recovery/pause/stress cases; it is not an
isolated per-campaign size. Performance varies with hardware/journal size.

Recovery killed both worker and runtime after a committed fill/lost response.
It resumed to 600 results; completed first-page/analysis effects remained one
external attempt. Fill operations committed once, with two reconciliation
attempts after response loss. Replay preserved catalog results and capital;
duplicate settlement credited once. Settlement uses an explicitly synthetic
future timestamp in this proof. Pause held page progress, cash and fill costs
stable, and CLI resume completed the campaign.

The live run screened 500 unique public markets, five pages, six GET attempts,
in 4.81 seconds: 476 unsupported outcomes, 24 unavailable/expired, zero eligible
trades, zero positions. No audited live interpretation catalog or suitable live
model inputs were supplied. Live evidence therefore proves bounded discovery
and screening; synthetic HTTP/native tests prove the remaining lifecycle. This
does not prove a live forecast-to-fill trade, postseason calibration, or profit.

Restate materially recovered workflow progress, child operations, and timers
across process loss. It still required independent ledger transactions,
reconciliation, immutable data, and source validation. It adds journal storage,
memory, latency, and deployment/state-compatibility work. There is no benchmark
against a competing non-Restate implementation. Observed results justify
prospective data collection and exact model validation, not profitability claims.
SUD-198 tracks tie-aware pricing; SUD-199 tracks prospective books/injury snapshots
and an untouched chronological holdout. Prior 2023–2025 strategy selection is not
a fresh holdout; disclose multiple testing. Price-history-only backtests cannot
establish realistic fills without historical depth, fees and availability data.

Raw data, reports, runtime state, and credentials stay outside tracked files in
ignored `data/`, `.research/`, `.runs/`, and `.tools/`.
