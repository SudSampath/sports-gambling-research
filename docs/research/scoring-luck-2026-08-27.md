# Decompose scoring luck — 2026-08-27

Linear: [SUD-129](https://linear.app/sudsampath/issue/SUD-129/decompose-scoring-luck)

## Decision

**Reject as default; keep opt-in for future ablation.** None of red-zone
touchdown-rate, special-teams EPA, or turnover margin -- individually or
combined -- consistently beats the shipped Pythagorean baseline across the
three real rolling test seasons (2023-2025). Turnover margin also exhibits
a genuine, reproducible instability (see below) that is itself an
argument against shipping it unregularized. The shipped default
(`pythagorean-v1`) is unchanged.

## Predeclared candidate set (committed before any fold was evaluated)

Per the AC's own caution against "enumerat[ing] features until one happens
to win 2025," the candidate list and procedure below were fixed in
`scoring_luck.py`'s module docstring before `evaluate-scoring-luck` was run
against any real season:

1. **Red-zone touchdown-rate differential** -- offense red-zone conversion
   rate (`redzone_touchdowns / redzone_plays`), shrunk toward the team's
   own prior-season rate (`REDZONE_SHRINKAGE_PSEUDOPLAYS = 15`), combined
   home-offense-vs-away-defense-allowed the same way
   `efficiency_strength.net_efficiency_differential` combines EPA.
2. **Special-teams EPA/play differential** -- reuses
   `efficiency_strength.compute_team_epa_by_field` directly with a
   different field pair (`special_teams_epa_per_play`/`special_teams_plays`)
   -- no new aggregation code, since nflverse's own special-teams EPA
   already folds return/coverage/punting value into one per-play number.
3. **Turnover margin per game**, shrunk toward its true long-run mean of
   zero (`TURNOVER_SHRINKAGE_PSEUDOGAMES = 8`) rather than toward a team's
   prior season, reusing the existing ESPN-boxscore-derived
   `build_turnovers_committed_index` (SUD-91/93) rather than new
   ingestion. Evaluated as its own independently-fit, independently-ablated
   coefficient -- a materially different procedure from SUD-110's rejected
   single hand-calibrated points-per-turnover-margin discount blended
   directly into scoring inputs.

All three are combined via the same additive-logit pattern SUD-126/SUD-127
use: `sigmoid(logit(baseline) + rz*rz_diff + st*st_diff + to*to_diff)`,
with each coefficient fit on training-fold games only
(`context_effects.fit_context_coefficient`, reused as-is).

### Explicitly not built this ticket

Fumble-recovery rate specifically (distinct from the turnover-margin
aggregate -- recovery of a *forced* fumble is close to a coin flip, but
this repository has not ingested play-level fumble-recovery-team
attribution), interceptions/fumbles split apart from each other,
return/defensive touchdowns, field position, fourth-down outcome rate, and
kicking accuracy separate from the special-teams EPA aggregate all would
need new play-level ingestion this repository has not yet run. One-score-
game record regression is also not built: it is a season-aggregate
strength adjustment (regressing a team's *record in one-score games*
toward .500), not a per-game point-in-time feature the way the other three
are -- using "will this game be decided by one score" itself as a pregame
feature would leak the very outcome being predicted, so it needs different
machinery than this ticket's validated additive-logit pattern, not more
time inside it.

## Real result

`evaluate-scoring-luck`, three rolling folds (each fit on all
2015-through-prior-season data, tested on the named season), against real
locally ingested 2015-2025 `TeamGameEfficiency`, `Game`, and
`PlayerGameStatline` data:

| Test season | RZ coef. | ST coef. | TO coef. | Baseline Brier | RZ-only | ST-only | TO-only | Combined |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2023 | 0.261 | 0.512 | **20.000** | **0.2313** | 0.2313 | 0.2312 | 0.3966 | 0.3967 |
| 2024 | 0.294 | 0.515 | -0.203 | 0.2179 | 0.2179 | **0.2175** | 0.2189 | 0.2187 |
| 2025 | 0.280 | 0.543 | -0.0002 | **0.2206** | 0.2209 | 0.2220 | **0.2206** | 0.2222 |

Red-zone and special-teams coefficients are small, stable in sign, and
similar in magnitude across all three folds -- but neither improves Brier
score or log loss over the baseline in any season by a meaningful margin
(red-zone is flat; special-teams edges out baseline only in 2024, by
0.0004). Neither is a real signal at this sample size.

## A real instability found, not fixed -- and why not

The turnover coefficient fit on the 2015-2022 training fold landed at
**exactly `20.0000`**, the outer edge of the shared
`SCORING_LUCK_COEFFICIENT_BOUNDS = (-20.0, 20.0)` search interval -- and
the resulting 2023 test-fold predictions are catastrophic: Brier `0.3966`
and log loss `2.9377`, both far worse than the always-0.5 baseline
(Brier 0.25) would produce. The 2024 and 2025 folds, by contrast, fit sane,
small turnover coefficients (`-0.203`, `-0.0002`) with unremarkable
held-out performance. This is not the same failure SUD-126 found (a
bounds-scale mismatch that silently capped an otherwise well-behaved fit) --
widening the bound further would not fix it, since the golden-section
search is finding a training-fold optimum that keeps improving toward the
boundary. This is quasi-separation: `golden-section`/MSE-minimization
without regularization can drive a single coefficient toward infinity when
a feature correlates strongly enough with training outcomes, and
current-season turnover margin-to-date is mechanically entangled with a
team's win/loss record so far (takeaways often *cause* the wins that also
feed the Pythagorean baseline's own points-for/against), making this
particular feature more prone to that failure mode than red-zone rate or
special-teams EPA, both of which fit stably across all three folds.

This is left undamped and reported rather than patched with an ad hoc
narrower bound for turnover alone: the instability itself is evidence
against shipping an unregularized, per-fold-fit turnover coefficient, on
top of the aggregate discount SUD-110 already rejected. A production
version of this feature would need L2 regularization or a much narrower,
independently-justified prior bound (not one picked after seeing this
result) -- explicitly left as future work, not attempted here.

## What this ticket does not do

- Does not change the shipped default model.
- Does not incorporate market/betting-line data anywhere in the model.
- Does not build fumble-recovery, interception/fumble-split,
  return/defensive-touchdown, field-position, fourth-down, kicking-
  specific, or one-score-game-record components (see above).
- Does not add regularization to the turnover-coefficient fit -- the
  instability is reported, not patched.

## Reproduction

```powershell
python -m sgr.cli evaluate-scoring-luck --train-season 2015 --train-season 2016 --train-season 2017 --train-season 2018 --train-season 2019 --train-season 2020 --train-season 2021 --train-season 2022 --test-season 2023
python -m sgr.cli evaluate-scoring-luck --train-season 2015 ... --train-season 2023 --test-season 2024
python -m sgr.cli evaluate-scoring-luck --train-season 2015 ... --train-season 2024 --test-season 2025
```
