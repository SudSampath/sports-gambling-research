# Roster-continuity research — 2026-08-26

Linear: [SUD-118](https://linear.app/sudsampath/issue/SUD-118/add-roster-continuity-variant)

## Decision

Ship snap-weighted roster continuity as an **opt-in research variant**, not as
the default single-game model. It improved the held-out 2025 season's win-total
MAE and RMSE and was directionally better over earlier rolling tests. Its
single-game probability improvement was effectively flat and neither paired
test crossed the predeclared 0.05 significance threshold.

This preserves `pythagorean-v1` and identifies the new path independently as
`pythagorean-roster-continuity-v1`.

## Data and point-in-time convention

- Prior-season player snap counts come from nflverse's public
  [`snap_counts`](https://github.com/nflverse/nflreadr/blob/main/R/load_snap_counts.R)
  release asset.
- Historical target rosters use the target season's regular-season Week 1 rows
  from nflverse's public
  [`weekly_rosters`](https://github.com/nflverse/nflreadr/blob/main/R/load_rosters_weekly.R)
  asset. Current estimates use the retrieved-at snapshot from
  [`rosters`](https://github.com/nflverse/nflreadr/blob/main/R/load_rosters.R).
- The connector retains the exact CSV, retrieval time, URL, and SHA-256. Each
  canonical continuity signal attaches both the snap and roster snapshots.
- Only `ACT`, `INA`, and `RES` roster statuses count as retained. `CUT`, `DEV`,
  `RET`, exempt, and other non-playing statuses do not.
- Historical signals must have a feature cutoff at or before the forecast
  cutoff. Current-roster signals use their actual retrieval time. A later
  snapshot is rejected rather than silently used.
- nflverse documents the broader data project and licensing in its
  [official data repository](https://github.com/nflverse/nflverse-data) and
  [nflreadpy repository](https://github.com/nflverse/nflreadpy).

## Model

For each team and unit:

```text
retention = retained prior-season snaps / all prior-season snaps
quality_offense = log(prior PF/game / prior league PF/team/game)
quality_defense = log(prior league PF/team/game / prior PA/game)

adjusted prior PF/game = prior PF/game
  * exp(-0.877724 * (1 - offense retention) * quality_offense)

adjusted prior PA/game = prior PA/game
  * exp(+1.183390 * (1 - defense retention) * quality_defense)
```

The adjusted prior rates enter the baseline's existing four-pseudogame
shrinkage. Full retention is therefore an exact no-op; the adjustment fades as
current-season games accrue.

The two no-intercept coefficients were fit on 352 team-season transitions from
2014 through 2024. For offense the target was
`log(next PF/game / prior PF/game)`; for defense it was
`log(prior PA/game / next PA/game)`. The 2025 holdout did not participate in
coefficient fitting.

## Held-out result: 2025 regular season

The comparison uses the same 271 non-tied games for both configurations (one
tie excluded) and one preseason projection for each of 32 final team win
totals. The injury adjustment is disabled on both sides so this is an isolated
test of roster continuity.

| Metric | Baseline | Roster continuity | Change |
|---|---:|---:|---:|
| Game Brier | 0.220617 | 0.220608 | -0.000009 |
| Game log loss | 0.629976 | 0.629797 | -0.000179 |
| Game accuracy | 62.36% | 62.73% | +0.37 pp |
| Win-total MAE | 2.809 | 2.650 | -0.159 wins |
| Win-total RMSE | 3.457 | 3.231 | -0.226 wins |

- Paired per-game Brier test: `z=0.004`, two-sided `p=0.9968`.
- Paired team win-total squared-error test: `z=1.567`, two-sided `p=0.1171`.
- Earlier rolling preseason checks averaged MAE 2.392 for the baseline and
  2.364 for roster continuity. This supported carrying the variant forward,
  but the small difference is not evidence for replacing the default.

## Alternatives checked

| Candidate | 2025 win-total result | Earlier rolling check | Decision |
|---|---|---|---|
| Opening QB depth chart | MAE 2.896, RMSE 3.597 | MAE 2.398 vs 2.392 baseline | Reject |
| Head-coach continuity | MAE 2.682, RMSE 3.246 | MAE 2.388 vs 2.392 baseline | Do not ship alone; improvement is too small/unreliable |
| Roster + coach | MAE 2.585, RMSE 3.111 | MAE worsened to 2.507 | Reject as holdout overfit |
| Snap-weighted roster | MAE 2.650, RMSE 3.231 | MAE 2.364 vs 2.392 baseline | Ship opt-in |

## Current 2026 estimate effect

Using the 2026 roster snapshot retrieved on 2026-08-26, the signal mostly
shrinks extreme 2025 scoring performances. The largest current revisions are:

| Team | Baseline wins | Continuity wins | Delta | Off./def. retained |
|---|---:|---:|---:|---:|
| Tennessee | 4.19 | 6.23 | +2.04 | 39.2% / 47.7% |
| Las Vegas | 3.56 | 5.51 | +1.95 | 36.9% / 63.9% |
| New York Jets | 4.20 | 5.80 | +1.59 | 35.6% / 65.4% |
| Jacksonville | 11.36 | 9.91 | -1.45 | 39.6% / 68.4% |
| New England | 11.92 | 10.50 | -1.42 | 43.5% / 69.5% |
| Buffalo | 10.61 | 9.40 | -1.21 | 46.2% / 50.6% |
| Seattle | 12.63 | 11.43 | -1.20 | 48.2% / 78.9% |

These are model estimates for research and calibration, not wagering advice.

## Reproduction

```powershell
python -m sgr.cli ingest-roster-continuity --season 2025 --historical-week1 --as-of 2025-08-27T00:00:00
python -m sgr.cli compare-roster-continuity --season 2025
python -m sgr.cli ingest-roster-continuity --season 2026
python -m sgr.cli project-roster-continuity --season 2026
```

The historical cutoff is explicit and must precede the first prediction
cutoff. The current ingestion command records the downloaded roster's actual
retrieval timestamp automatically.
