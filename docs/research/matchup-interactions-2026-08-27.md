# Model matchup interactions — 2026-08-27

Linear: [SUD-126](https://linear.app/sudsampath/issue/SUD-126/model-matchup-interactions)

## Decision

**Reject as default; keep opt-in for future ablation.** Pass-vs-pass and
rush-vs-rush matchup differentials, fit as an additive logit adjustment on
top of the shipped Pythagorean baseline, do **not** consistently beat the
baseline across the three real rolling test seasons (2023-2025) -- one
season improves marginally, two are flat-to-worse. The shipped default
(`pythagorean-v1`) is unchanged.

## What was built

`matchup_interactions.py` adds two matchup-specific differentials on top of
SUD-124's play-level efficiency data layer, rather than building a new
team-strength model:

1. **Pass and rush differentials** (`compute_matchup_differential`): each
   team's pass-specific and rush-specific EPA/play (offense and
   defense-allowed, the latter read from the opponent's own split in the
   same game, per `TeamGameEfficiency`'s no-duplicate-defense-fields
   design) combined via `efficiency_strength.net_efficiency_differential`,
   restricted to the pass or rush split instead of the aggregate.
2. **Aggregate fallback** (`_team_epa_with_fallback`): when a team's
   pass- or rush-specific coverage is insufficient
   (`InsufficientPlayDataError`), the differential falls back to that
   team's aggregate offense/defense rating rather than aborting the whole
   matchup term or fabricating a split value -- flagged per team, per
   split, so evaluation can report how often the fallback actually fired.
3. **Additive logit adjustment** (`matchup_adjusted_probability`):
   `sigmoid(logit(baseline_probability) + pass_coefficient * pass_differential + rush_coefficient * rush_differential)`
   -- the same pattern SUD-127's context-effects ablation uses, and in fact
   reuses `context_effects.fit_context_coefficient` directly for fitting
   both coefficients rather than a separate implementation.

A generic `compute_team_epa_by_field` was extracted from
`efficiency_strength.compute_team_efficiency` (which now calls it with
`offense_epa_per_play`/`offense_plays` as its default field pair) so the
point-in-time filtering, shrinkage, and opponent-defense-lookup logic is
written once and shared by both the aggregate model and this ticket's
pass/rush splits.

### Scope deliberately left un-built

Only pass and rush interactions are wired into a fitted coefficient.
Pressure/sack tendency, explosive-play rate, early-down profile, and
red-zone profile are all real, already-ingested `TeamGameEfficiency` fields
(SUD-123) that could form additional matchup dimensions, but adding four
more fit-per-fold coefficients was not validated one at a time within this
ticket's time box. A future ticket can extend `matchup_interactions.py` the
same way pass/rush are built here -- consistent with the AC's own
"documented no-build... represented only where historical coverage is
sufficient" allowance already used elsewhere this session (SUD-126 predates
no scoring-luck decomposition, which is SUD-129's own scope).

## A real bug found and fixed before evaluating

`context_effects.fit_context_coefficient`'s default search bounds
(`(-0.2, 0.2)`) are calibrated for that ticket's own features -- an integer
rest-day differential and a +-1 dome indicator. A pass/rush net-EPA/play
differential lives on the same scale `efficiency_strength.py`'s own slope
fit targets (bounds `(0.5, 20.0)`), roughly two orders of magnitude smaller
in raw feature units than a rest-day count. An unmodified call against real
2015-2022 training data saturated both coefficients at exactly `0.2000`,
the upper bound -- not a converged optimum, an artifact of reusing the
wrong scale's search interval. Fixed by passing an explicit, wider
`bounds=(-20.0, 20.0)` (matching `efficiency_strength`'s own slope scale)
when fitting `pass_coefficient`/`rush_coefficient` in
`matchup_interactions_evaluation.py`.

## Real result

`evaluate-matchup-interactions`, three rolling folds (each fit on all
2015-through-prior-season data, tested on the named season), against real
locally ingested 2015-2025 `TeamGameEfficiency` and `Game` data:

| Test season | Train seasons | Pass coef. (fit) | Rush coef. (fit) | Baseline Brier | Combined Brier | Baseline log loss | Combined log loss |
|---|---|---:|---:|---:|---:|---:|---:|
| 2023 | 2015-2022 | 0.632 | 0.772 | **0.2313** | 0.2314 | **0.6569** | 0.6586 |
| 2024 | 2015-2023 | 0.571 | 0.801 | 0.2179 | **0.2143** | 0.6282 | **0.6207** |
| 2025 | 2015-2024 | 0.727 | 0.827 | **0.2206** | 0.2267 | **0.6300** | 0.6434 |

Pass-only and rush-only individually show the same mixed pattern (rush-only
edges out baseline Brier in 2023 and 2024, worse in 2025; pass-only is
worse than baseline in every season). No configuration beats the baseline
in more than one of the three seasons, and none beats it consistently on
both Brier and log loss together. Zero games in any season required the
aggregate-rating fallback (real 2015-2025 pass/rush coverage is complete
for every team-game in this window), so the mixed result is not an
artifact of missing-data fallback noise.

## What this ticket does not do

- Does not change the shipped default model.
- Does not incorporate market/betting-line data anywhere in the model.
- Does not build pressure/sack, explosive-play, early-down, or red-zone
  matchup dimensions (see "Scope deliberately left un-built" above).
- Does not claim pass/rush splits are the ceiling for the matchup-
  interaction feature family -- SUD-129 (Decompose Scoring Luck) and
  SUD-128 (Model Coaching Interactions) are separate tickets extending
  this data layer further, not this one.

## Reproduction

```powershell
python -m sgr.cli evaluate-matchup-interactions --train-season 2015 --train-season 2016 --train-season 2017 --train-season 2018 --train-season 2019 --train-season 2020 --train-season 2021 --train-season 2022 --test-season 2023
python -m sgr.cli evaluate-matchup-interactions --train-season 2015 ... --train-season 2023 --test-season 2024
python -m sgr.cli evaluate-matchup-interactions --train-season 2015 ... --train-season 2024 --test-season 2025
```
