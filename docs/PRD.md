> Source of truth: [Linear PRD](https://linear.app/sudsampath/document/prd-nfl-market-signals-82a747cfd599)
>
> This repository copy is the version published on 2026-07-29 for implementation and review. Update the Linear document first, then synchronize this file in a dedicated documentation pull request.

# Header

| Field | Value |
| -- | -- |
| Product area | Quantitative sports-market research |
| Date | 2026-07-29 |
| Author | Sudarshan Sampath |
| Status | Draft |
| Project | NFL Market Signals |
| Initial sport | NFL |
| Future sports | NBA, EPL |
| Decision | Phase 1 is forecast research and paper trading only |

# Problem

Sports-event contracts often expose a market-implied probability that differs from a simple, transparent estimate of team quality. Today there is no repeatable internal workflow that:

* converts NFL scoring performance into an expected win probability,
* joins that estimate to a market contract representing the same event,
* accounts for data quality, time of prediction, and market liquidity,
* evaluates whether an apparent edge is real through historical backtesting, and
* produces an auditable paper-trade recommendation without exposing credentials.

This gap makes it easy to confuse hindsight with predictive performance, compare mismatched markets, or take actions without a record of the underlying inputs and assumptions.

## Problem evidence to collect during the spike

| Question | Evidence required | Acceptance signal |
| -- | -- | -- |
| Is Pythagorean expectation informative beyond market price? | Season-level and pre-game backtest | Positive out-of-sample calibration improvement or documented rejection |
| Are ESPN and market entities reliably matchable? | Sampled historical and current game/contract join audit | ≥95% correct joins after normalization, with failures classified |
| Can observed edge survive execution assumptions? | Paper-trade P&L including fees, spread, and liquidity constraints | Expected edge remains positive after conservative assumptions |
| Can the process be reproduced? | Versioned inputs, model parameters, and decision logs | A past recommendation can be recreated exactly from stored artifacts |

# Hypothesis

For NFL games, a calibrated Pythagorean-expectation model based on points scored and allowed—combined with opponent, recency, and availability adjustments—will produce a well-calibrated pre-game win probability. When that probability materially differs from the comparable Kalshi contract price after fees, spread, liquidity, and uncertainty haircuts, the system can surface a paper-trade recommendation with positive expected value more consistently than a naive market-following baseline.

# Why this works

| Component | Role | Rationale |
| -- | -- | -- |
| Pythagorean expectation | Transparent team-strength prior | Uses scoring margin information that raw win-loss record discards |
| ESPN data ingestion | Games, teams, scores, schedules, and event identifiers | Establishes a consistent pre-game and post-game dataset |
| Kalshi market ingestion | Contract price, bid/ask, volume, status, and settlement metadata | Supplies the market-implied probability and executable constraints |
| Calibrator | Converts strength differential to win probability | Reduces overconfidence and allows out-of-sample evaluation |
| Decision policy | Selects or rejects a paper trade | Enforces a conservative threshold after costs and uncertainty |
| Audit log | Stores inputs, model version, market snapshot, and outcome | Makes research reproducible and supports later review |

# Difference from the status quo

| Status quo | Proposed system |
| -- | -- |
| Ad hoc review of standings, headlines, and prices | Scheduled, normalized game and market data |
| Win-loss record treated as team quality | Scoring-derived expected-win prior with calibrated adjustments |
| Market price viewed without explicit friction | Price compared after fees, spread, liquidity, timing, and uncertainty |
| No reliable historical reconstruction | Point-in-time snapshots, feature versions, and decision logs |
| Potentially action-oriented from day one | Research and paper trades first; real-money capability requires separate approval |

# Architecture

```
 ESPN data                  Kalshi data
 (schedule, scores)         (contracts, quotes, status)
      |                            |
      +--------> Normalization & entity matching
                            |
                            v
                    Versioned data store
                            |
                            v
 Pythagorean features --> Probability model --> Calibration
                            |                      |
                            +----------+-----------+
                                       v
                         Market comparison / guardrails
                                       |
                     +-----------------+------------------+
                     |                                    |
                     v                                    v
            Paper-trade recommendation              Rejection reason
                     |
                     v
         Outcome ingestion -> backtest & monitoring
```

## Model specification for the spike

| Layer | Initial approach | Validation |
| -- | -- | -- |
| Scoring prior | `expected_win_pct = PF^x / (PF^x + PA^x)` using a season- and era-calibrated exponent `x` | Walk-forward season backtest |
| Recency | Prior-season blend early in season; current-season rolling window thereafter | Compare fixed, rolling, and shrinkage variants |
| Matchup | Home-field and rest-day features; only use injury/roster data if point-in-time availability is proven | Ablation study |
| Probability | Map team-strength differential to home-win probability with logistic calibration | Brier score, log loss, reliability curve |
| Edge | Model probability minus conservative executable market probability | Expected value after all known costs |
| Sizing | Fixed paper stake only in Phase 1 | No live sizing until separately approved |

# Validated constraints

> RULE: The first release must create paper-trade recommendations only. It must not submit, modify, cancel, or settle real-money orders.

> RULE: No API key, private key, token, cookie, account identifier, or personally identifying data may be committed to the public repository, included in test fixtures, or placed in Linear documents.

> RULE: Runtime secrets must be injected through local environment variables for development and a managed secret store for deployed services; logs must redact them.

> RULE: Each recommendation must record the model version, feature timestamp, data source timestamps, market identifier, bid/ask or executable quote, assumed costs, and rejection/selection reason.

> RULE: Backtests must use only data available before the game and market snapshot at the simulated decision time. No post-game data may leak into features.

> RULE: A recommendation is invalid if the event, outcome side, settlement rule, timing, or market type cannot be matched unambiguously.

> RULE: The system must gate market access by jurisdiction, platform terms, legal review, age/eligibility requirements, and user authorization before any future real-money capability is considered.

> RULE: Public documentation must clearly state that outputs are research signals, not financial, betting, or legal advice.

# Risks

| Risk | Impact | Mitigation | Owner / decision gate |
| -- | -- | -- | -- |
| Pythagorean expectation has no durable market edge | High | Treat it as a benchmark; use strict walk-forward tests and stop if it fails | Product / research |
| Data leakage in historical testing | High | Feature timestamps, immutable snapshots, leakage tests, code review | Engineering |
| Contract-to-game mismatch | High | Canonical event/outcome mapping and manual exception queue | Engineering |
| Market friction erases apparent edge | High | Use bid/ask, fees, liquidity limits, stale-quote checks, and conservative haircuts | Research |
| Regulatory or platform-policy breach | High | Compliance review and hard feature gate before any real-money work | Product / legal |
| Credentials exposed in a public repo | High | Git ignore, secret scanner, CI checks, rotation procedure | Engineering |
| Small NFL sample / regime change | Medium | Multi-season walk-forward validation, confidence intervals, season-specific monitoring | Research |
| ESPN or Kalshi interface changes | Medium | Adapter boundary, contract tests, freshness alerts, graceful degradation | Engineering |
| Responsible-use harm | High | No autonomous live execution; human review and loss-limit design deferred to approval phase | Product |

# Open questions

| Question | Why it matters | Proposed next step |
| -- | -- | -- |
| Does “create wages” mean “create wagers,” and is the goal research, paper trading, or actual order submission? | Determines legal, compliance, and system scope | Confirm before any live-trading design |
| Which Kalshi markets are in scope: winner-only, spreads, totals, season futures, or others? | Settlement semantics and model target differ | Start with NFL game-winner contracts only |
| What jurisdiction and user eligibility constraints apply? | May block future market interaction entirely | Obtain legal/compliance guidance before Phase 2 |
| Which historical ESPN fields and retention limits are available? | Determines model richness and backtest reproducibility | Run data-availability spike |
| What market data cadence and historical snapshots are available? | Required for executable backtests | Validate access and quote fidelity |
| What is the conservative fee/slippage model? | Required before measuring expected value | Document assumptions with the venue terms |
| What approval and loss-limit policy would govern a future live workflow? | Required for responsible operation | Define only after validated paper results |

# Success criteria

## Spike (NFL, research and paper trading)

| Area | Measure | Target |
| -- | -- | -- |
| Data | NFL seasons and current schedule ingest reproducibly | ≥3 completed seasons plus current season, with source timestamps |
| Matching | Game-to-contract mapping correctness | ≥95% audited correctness; unmatched cases explicitly rejected |
| Model | Out-of-sample probability quality | Beat win-loss baseline on Brier score and log loss, or document falsification |
| Calibration | Reliability | No material overconfidence across reported probability bins |
| Backtest | Friction-aware paper-trade evaluation | Positive expected value under predefined conservative costs, with uncertainty reported |
| Operations | Reproducibility | Regenerate any recommendation from versioned inputs and code |
| Security | Secret hygiene | Secret scan passes; no credentials in repository or Linear |
| Safety | Execution boundary | No live trading endpoint or order-submission path exists in the spike |

## Production readiness (requires separate approval)

| Area | Measure | Target |
| -- | -- | -- |
| Compliance | Eligibility, jurisdiction, platform-policy, and legal sign-off | Documented approval before access is enabled |
| Security | Secret management, least privilege, auditability, incident response | Review complete and tested |
| Execution | Quote freshness, idempotency, limits, failure recovery, reconciliation | Simulated adverse-path tests pass |
| Risk | User-approved exposure, daily loss, and stop-trading limits | Enforced server-side |
| Reliability | Monitoring and alerting | Data freshness, error, drift, and reconciliation alerts active |
| Governance | Human approval workflow | Every live action attributable and reviewable |

# Deferred items and path to production

| Deferred item | Why deferred | Path to production |
| -- | -- | -- |
| Real-money order submission | Regulatory, platform, security, and responsible-use risk | Separate PRD and approval gate after successful paper-trade validation |
| Automated stake sizing | Requires validated distributional assumptions and risk policy | Research proposal, simulations, user limits, then human-approved rollout |
| NBA and EPL support | Different schedules, scoring regimes, data mapping, and market semantics | Reuse adapters and add sport-specific model/calibration tracks after NFL MVP |
| Injury/news models | Point-in-time provenance is difficult and leakage-prone | Add only with timestamped, licensed source and ablation evidence |
| Complex market types | Settlement and pricing semantics vary | Add one contract type at a time with explicit mapping tests |
| Hosted public API/UI | Expands abuse, security, and support surface | Build after the internal research workflow has stable contracts and governance |

# Initial delivery sequence

1. Define canonical NFL game, team, and market-outcome schemas.
2. Build ESPN and Kalshi read-only adapters using environment-configured credentials where required.
3. Implement versioned storage and point-in-time snapshots.
4. Implement Pythagorean baseline, walk-forward backtester, and calibration report.
5. Add market-matching and friction-aware paper-trade policy.
6. Add secret scanning, audit logs, data freshness checks, and rejection monitoring.
7. Review spike results and decide whether to expand, revise the model, or stop.

# ESPN data-source decision

## Reference implementation

The initial ESPN adapter will use the endpoint documentation in [pseudo-r/Public-ESPN-API](<https://github.com/pseudo-r/Public-ESPN-API>) as a development reference. It documents a public NFL scoreboard endpoint and related schedules, teams, standings, summaries, play-by-play, and competitor statistics. The repository explicitly describes these as undocumented ESPN interfaces, warns that they may change without notice, and recommends caching, respectful request rates, and error handling. [Source](<https://github.com/pseudo-r/Public-ESPN-API>)

| Adapter concern | Spike decision |
| -- | -- |
| Initial endpoint | Use the documented NFL scoreboard endpoint for schedule/event discovery; obtain game details through the documented event-summary path only as needed |
| API status | Treat ESPN as an unofficial, best-effort source—not a contractual production dependency |
| Rate limiting | Cache responses, use bounded polling, exponential backoff, and a circuit breaker; never scrape at high frequency |
| Schema changes | Store raw payloads, validate normalized schemas, maintain contract fixtures, and emit freshness/schema-drift alerts |
| Historical backtest | Validate date-range coverage and point-in-time availability before declaring ESPN fit for historical reconstruction |
| Source isolation | Put all calls behind an `EspnProvider` interface so a licensed/official substitute can replace it without affecting models |
| Terms and attribution | Review current ESPN terms before deployment; document provenance and avoid representing ESPN data as independently verified |

> RULE: ESPN-derived data may be used for the research spike only through a read-only, cache-first adapter. Any sustained production use must pass a source licensing, terms, reliability, and retention review.

> RULE: ESPN-provided odds, win probabilities, predictors, or power-index fields must not be used as model features in the Pythagorean baseline. They may be stored separately for benchmark analysis only, preventing target or vendor-model leakage.

# Professional NFL handicapping research

## What the research supports

Professional practice is best represented as a repeatable pricing and evaluation process, not as a set of narrative rules. Industry descriptions emphasize independent model projections, player/injury information, and market prices; published work finds that information content typically increases as NFL markets approach kickoff and that apparent biases must be evaluated after transaction costs. [PFF: timing, player data, injuries, and market information](<https://www.pff.com/news/bet-why-betting-early-critical-beating-nfl-markets>) · [Intra-week NFL market study](<https://www.sciencedirect.com/science/article/abs/pii/S0927539813000509>) · [NFL market-efficiency study](<https://doi.org/10.1111/j.1540-6261.1997.tb01129.x>)

## Handicapping framework for the NFL spike

| Workstream | Professional-style question | Spike implementation | Guardrail |
| -- | -- | -- | -- |
| Independent fair price | What probability does the model assign before considering the traded price? | Pythagorean scoring prior, recency shrinkage, calibrated home field, and a logistic probability layer | Never train on the target market’s price when measuring the standalone baseline |
| Team quality beyond record | Are points for/against, strength of schedule, and game states more informative than wins alone? | Compare raw Pythagorean expectation with opponent-adjusted and garbage-time-aware variants | Every added feature needs a walk-forward ablation result |
| Availability | Which confirmed roster changes alter expected team strength? | Point-in-time injury/inactive feed; position and player-value adjustment only after historical coverage is proven | Timestamp every report; exclude hindsight and unverified news |
| Game context | Do rest, travel/body-clock, venue, surface, and weather alter the fair price? | Add rest and venue first; weather only for outdoor games with archived forecast/observation timestamps | Test interactions and stability; do not assume a generic “weather edge” |
| Matchup | Does a team’s offense/defense profile interact with the opponent’s weaknesses? | Deferred feature track using point-in-time efficiency statistics | Avoid double-counting signals already captured by scoring strength |
| Market microstructure | Can a position actually be entered at the displayed price, in size, before it becomes stale? | Record Kalshi bid/ask, depth/available liquidity where available, fees, contract rules, timestamp, and quote age | Use executable side of the spread, not midpoint or last trade |
| Market learning | Does the system beat the later/closing consensus price on average? | Calculate probability- and price-based closing-line value (CLV) for every paper recommendation | CLV is a diagnostic, not proof of profit; evaluate alongside proper scoring rules and realized results |
| Selection discipline | Is the model’s residual edge larger than forecast uncertainty and all friction? | Require a precommitted edge threshold, minimum liquidity, freshness, and event-match confidence | Rejections are first-class outputs; no “action” merely because a game exists |
| Bankroll/risk | Is exposure controlled when estimates are wrong or correlated? | Fixed notional paper stakes and per-week exposure caps | Real-money sizing is deferred; no Kelly or autonomous scaling in Phase 1 |

## Testable claims—not default truths

| Candidate signal | Why it is considered | Required test before use |
| -- | -- | -- |
| Opening-to-closing market movement | Published NFL research indicates information content can rise through the betting week. | Compare model and market snapshots at fixed offsets; prohibit use of post-decision movement as a feature. |
| Injury/inactive news | Practitioners explicitly incorporate player data and injury information. | Prove point-in-time source coverage, then test incremental calibration and CLV value by position group. |
| Rest, travel, and venue | Plausible physical/contextual mechanisms. | Multi-season, season-held-out ablation with interaction controls. |
| Weather | Published NFL work has examined weather-related price efficiency. | Use timestamped observed/forecast data and test only prespecified outdoor-game segments. [Weather-market study](<https://www.sciencedirect.com/science/article/abs/pii/S0148619506001019>) |
| Public bet share / reverse line movement | Industry tools present it as a proxy for sharp action, but it is venue-specific and incomplete. | Treat as exploratory only; require provenance, coverage, and a pre-registered out-of-sample result. [Industry methodology](<https://www.actionnetwork.com/how-to-bet-on-sports/general/sports-betting-data-how-to-win-action-network/>) |

## Revised data requirements

| Dataset | Decision-time fields | Retention / quality requirement |
| -- | -- | -- |
| ESPN | event ID, teams, kickoff, schedule, score, official status, summary | Raw response plus normalized record and retrieval timestamp |
| Roster/injury source | report status, source timestamp, player/team identifier | Preserve original timestamp and source; unresolved identity means no feature |
| Weather source | forecast/observation time, venue coordinates, temperature, wind, precipitation, roof status | Store decision-time forecast separately from final observed weather |
| Kalshi | market/contract ID, title, rules, status, bid/ask, trade/quote time, fees, liquidity if exposed | Immutable decision-time snapshot; validate event/outcome mapping |
| Benchmark market data (optional) | comparable price, market type, quote time | Only include when contract semantics are equivalent and licensed/allowed |

> RULE: The team must make its independent probability available before reading any decision-time Kalshi price. The price comparison may influence a paper-trade decision, but may not contaminate the baseline probability.

> RULE: Every proposed feature must clear a predeclared, walk-forward incremental-value test on calibration, log loss, and friction-aware paper-trade performance. Narrative plausibility or an in-sample result is insufficient.

> RULE: CLV must be measured for every paper recommendation against a documented later benchmark, but it must never be reported as a guarantee of positive returns.

## New spike deliverables

1. **Fair-price notebook:** pre-game Pythagorean baseline, probability calibration, and season-held-out evaluation.
2. **Point-in-time feature audit:** availability, rest, venue, and weather provenance; explicit “excluded” register for missing or leaky fields.
3. **Market-snapshot ledger:** decision-time Kalshi quote, executable price, contract-rule match, fees, liquidity, and later benchmark snapshot.
4. **Decision report:** model probability, conservative edge, uncertainty, accept/reject result, and post-game calibration/CLV review.
