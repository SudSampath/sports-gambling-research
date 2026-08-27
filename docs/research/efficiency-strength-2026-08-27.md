# Play-level efficiency-strength model — 2026-08-27

Linear: [SUD-124](https://linear.app/sudsampath/issue/SUD-124/model-efficiency-strength)

## Decision

**Reject as default; keep opt-in for future ablation.** The efficiency-strength
model, fit and evaluated on real data, does **not** beat the shipped
Pythagorean baseline on any of the three real rolling test seasons
(2023-2025). The shipped default (`pythagorean-v1`) is unchanged.

## What was built

A shrinkage-based, opponent-adjusted offense/defense rating
(`efficiency_strength.py`) built from SUD-123's play-level EPA:

1. **Team efficiency** (`compute_team_efficiency`): plays-weighted mean
   offense EPA/play, blended current-season-toward-prior-season the same
   way the Pythagorean baseline blends points-for/against
   (`shrink_toward_prior`, `SHRINKAGE_PSEUDOPLAYS = 260` -- a fixed,
   documented constant, not fit per fold, at the same status as the
   baseline's own `HOME_FIELD_LOGIT_BUMP`). A team's defense-allowed figure
   is read from its opponent's offense figure in the same game, per
   `TeamGameEfficiency`'s own design (no duplicated defense fields).
2. **Opponent adjustment** (`compute_opponent_adjusted_efficiencies`): a
   single joint pass across every team in the season, re-centering each
   team's raw efficiency by how the opponents it has actually played
   compare to league average. A genuinely different mechanism from
   SUD-108's rejected scalar strength-of-schedule adjustment (play-level
   EPA, not final scoring margin; jointly estimated across all 32 teams,
   not a single per-team win-percentage scalar).
3. **Win probability / margin**: `net_efficiency_differential` combines
   both teams' offense-vs-opponent-defense edges into one number; a
   logistic transform (fit slope) produces win probability, a linear
   transform (fit points-per-EPA) produces expected margin. Both
   coefficients are fit inside the training fold only
   (`select_efficiency_coefficients_on_training_fold`), mirroring
   `pythagorean.fit_exponent`'s golden-section-search pattern for the
   slope and an OLS closed form for points-per-EPA.

## A real bug found and fixed before evaluating

The initial implementation defaulted to 2 iterations of opponent
adjustment. Hand-verified on a small synthetic 4-team schedule: repeating
the update (re-anchoring each team to its own *raw* value while using
opponents' *previous-iteration adjusted* values) **oscillates with growing
amplitude** rather than converging -- an undamped Jacobi-style update with
no convergence guarantee on a small/sparse schedule graph. Fixed by
defaulting to a single joint pass (`OPPONENT_ADJUSTMENT_ITERATIONS = 1`);
more than 1 remains available for experimentation but is explicitly
documented as unverified, not a recommended default.

## Real data expansion required for this ticket

SUD-123 had only ingested play-level features for 2023-2025. A genuine
rolling, multi-season comparison needs deeper history, so this ticket ran
`ingest-play-level-features` for 2015-2022 as well (real network calls
against nflverse, ~46,000 real plays/season) -- local coverage is now
2015-2025 (11 seasons, 8,316 real `TeamGameEfficiency` records total).

## Real result

`evaluate-efficiency-strength`, three rolling folds (each fit on all
2015-through-prior-season data, tested on the named season):

| Test season | Train seasons | Slope (fit) | Points/EPA (fit) | Efficiency Brier | Baseline Brier | Efficiency log loss | Baseline log loss | Efficiency margin MAE |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 2023 | 2015-2022 | 1.426 | 10.60 | 0.2423 | **0.2313** | 0.6775 | **0.6569** | 11.09 |
| 2024 | 2015-2023 | 1.520 | 11.61 | 0.2411 | **0.2179** | 0.6750 | **0.6282** | 10.84 |
| 2025 | 2015-2024 | 1.716 | 13.09 | 0.2514 | **0.2206** | 0.6962 | **0.6300** | 11.20 |

The shipped Pythagorean baseline (uncalibrated `pythagorean-v1`, no injury
adjustment on either side for a clean model-vs-model comparison) wins on
every metric, every season. Both models were scored from the same
`FEATURE_CUTOFF_HOURS_BEFORE_KICKOFF` cutoff on the same real games (272 or
271 per season; one 2025 tie excluded from both, zero
`InsufficientPlayDataError` abstentions given the 2015-2022 backfill).

Plausible reasons this research signal did not translate into a stronger
forecast, offered as hypotheses, not established facts: the single-pass
opponent adjustment is a coarse joint estimate compared to a properly
converged rating system; the fixed 260-play shrinkage prior is not itself
fit; garbage-time filtering, red-zone/explosive-play/special-teams signal,
and quarterback-level attribution are all left as unused candidate inputs
in `TeamGameEfficiency` for a future refinement rather than incorporated
into this first pass's win-probability/margin transform.

## What this ticket does not do

- Does not change the shipped default model or its exponent.
- Does not incorporate market/betting-line data anywhere in the model --
  an independently priced football model, per the ticket's own scope.
- Does not claim the single-pass opponent adjustment is the ceiling for
  this feature family; SUD-126 (Model Matchup Interactions) and SUD-129
  (Decompose Scoring Luck) are the tickets that extend this data layer
  further, not this one.

## Reproduction

```powershell
python -m sgr.cli ingest-play-level-features --season 2015 --season 2016 --season 2017 --season 2018 --season 2019 --season 2020 --season 2021 --season 2022
python -m sgr.cli evaluate-efficiency-strength --train-season 2015 ... --train-season 2022 --test-season 2023
python -m sgr.cli evaluate-efficiency-strength --train-season 2015 ... --train-season 2023 --test-season 2024
python -m sgr.cli evaluate-efficiency-strength --train-season 2015 ... --train-season 2024 --test-season 2025
```
