# Expanded historical evaluation — 2026-08-27

Linear: [SUD-122](https://linear.app/sudsampath/issue/SUD-122/expand-historical-evaluation)

## What this ticket built

1. **Extended local ESPN game coverage from 4 seasons (2023-2026) to 27
   (2000-2026)** by backfilling regular-season games via the existing
   `ingest-historical-seasons` command. Along the way this surfaced and
   fixed real data-quality issues in the historical ESPN archive and in
   this project's own schedule assumptions (see "Bugs found and fixed"
   below) -- none of them hypothetical, all confirmed against live ESPN
   responses.
2. **A rolling-origin, multi-season evaluation harness**
   (`src/sgr/research/rolling_evaluation.py`) that trains each fold only on
   seasons strictly before its test season, selects the Pythagorean
   exponent inside that fold's own training seasons
   (`select_exponent_on_training_fold`, unchanged from SUD-38), and reports
   game Brier score/log loss/calibration/accuracy plus expected-margin
   MAE/RMSE by test season and in aggregate.
3. **Season-clustered bootstrap confidence intervals**
   (`season_clustered_bootstrap_ci`): resamples whole seasons, not
   individual games, so within-season dependence (a team's whole-season
   form, a rules change, a shortened/rescheduled year) isn't treated as if
   every game were an independent draw the way `evaluation.py`'s existing
   per-game bootstrap does.
4. **A separate stable-parameter robustness check** (`robustness_evaluation`)
   that fits once on the earliest available era and tests 2011-2016,
   deliberately not pooled with the primary rolling analysis.
5. A real, measured performance fix (see below) that was necessary to make
   any of this usable at the expanded data scale.

## Real data floor: 2000, not 1999

nflverse's play-by-play goes back to 1999, but **this project's own
ESPN-sourced canonical data cannot**: `EspnConnector._validate_season_request`
and every canonical schema entity with a `season_year` field
(`schemas.py`, `Field(ge=2000)`) both floor at 2000 -- verified live by
attempting to ingest 1999, which raises `ValueError`. Loosening a
foundational schema constraint used across every entity in the project was
judged out of scope for extending the *evaluation* harness, so
`EARLIEST_AVAILABLE_SEASON = 2000` in `rolling_evaluation.py`, and every
"1999-2010" reference in the ticket/PRD language is "2000-2010" for
anything built on this project's own `Game` data.

## Bugs found and fixed while backfilling 2000-2022

All four were discovered by attempting the real backfill, not invented in
advance, and are covered by regression tests:

| Season | Symptom | Real cause | Fix |
|---|---|---|---|
| 2000-2001 | Coverage gate failed: "31/32 teams" | The Houston Texans joined as the league's 32nd franchise in **2002**; 2000-2001 genuinely had 31 teams (`historical.py::expected_team_count`) | Season-aware expected team count |
| 2000-2020 | Coverage gate failed: expected 272 games | The NFL ran a 16-game/17-week season through 2020; the 17-game/18-week era started in **2021** (`historical.py::expected_regular_season_games`/`expected_regular_season_weeks`) | Season-aware expected games/weeks |
| 2001 week 14 | Hard crash: `EspnSchemaError: ESPN event is missing id` | ESPN's own historical scoreboard archive contains a completely empty `{}` placeholder event alongside 14 real games | `EspnConnector._normalize_snapshot` now skips an empty event rather than aborting the whole week |
| 2001, 2014, 2017, 2022 | Coverage gate failed on otherwise-complete seasons ("1 incomplete game") | A rescheduled-or-cancelled game keeps a permanent `STATUS_POSTPONED`/`STATUS_CANCELED` placeholder under its **original** event ID in ESPN's archive (2014 wk12 BUF@NYJ moved for the "Snowvember" blizzard; 2017 wk1 MIA@TB moved for Hurricane Irma; 2022 wk17 CIN@BUF, the Damar Hamlin game, never replayed). The real replayed game (when one exists) is captured normally under its own event ID | `historical.py` excludes `PERMANENTLY_UNCOMPLETED_STATUSES` from both the captured and expected counts, reporting them explicitly (`rescheduled_or_canceled_event_ids`) rather than silently dropping or blocking on them |
| 2001 | Coverage gate failed: 247/248 games with no other symptom | ESPN's site API scoreboard simply never returns a Dallas-Seattle game anywhere in its 2001 archive -- every other of the 31 teams shows exactly 16 games; only DAL and SEA show 15 each. Confirmed absent from the raw per-week payloads under any week/status, not a filtering bug here | Documented one-off exception (`SEASON_GAME_COUNT_EXCEPTIONS[2001] = 247`); not recoverable from this data source |

## Real performance fix required to make this tractable

`generate_forecast` (the shipped model's own forecast function) had no
cache at all for the `games` table, and `compute_injury_adjustment` had no
cache for the ~64k-row `player_game_statlines` table. Both re-scanned their
full table from scratch on **every single call**. With the original 4
seasons (~1,000 games) this was slow but livable; after this ticket
backfilled to ~7,000 games, the identical O(n) reload happening inside an
O(n)-game rolling evaluation loop made a single fold's exponent selection
take on the order of tens of minutes.

Two real, tested fixes:

- `generate_forecast` now reuses a process-lifetime, database-path-keyed
  cache of `all_games` (mirroring the existing injury-inputs cache
  pattern), refreshing itself if a requested game_id is a genuine cache
  miss (so a game written *after* the cache was first populated is still
  found -- an existing test exercised exactly this pattern).
- `run_walk_forward_evaluation`/`select_exponent_on_training_fold` gained
  an `apply_injury_adjustment` passthrough (default `True`, unchanged
  behavior everywhere else). `rolling_evaluation.py` passes `False`: this
  is **provably a no-op for every completed game** -- `injury_ingest.py`
  never backfills availability reports against completed games, and a live
  audit of the real store confirmed zero overlap between the 2,703 real
  availability reports and any completed game -- so disabling it changes
  no computed probability, only the wasted scan time.

Measured impact: 20 real forecasts with the injury path enabled took 3.7s
(~186ms/forecast); the same 20 with it disabled took 0.9s (~46ms/forecast)
-- a ~4x reduction, and the dominant remaining cost at this data scale.

**Known remaining limitation, disclosed rather than fixed here:** each
fold re-scores its entire training window from scratch with no cross-fold
forecast memoization, so folds sharing most of the same early history
(every fold in the primary 2017-2025 analysis; every fold in the
robustness check, which shares an *identical* fixed training set) redo
that shared work independently. This is a real, bounded-scope
optimization left for a follow-up rather than expanded here.

## Results

From a real run of `python -m sgr.cli expand-evaluation --robustness` against
the full local 2000-2026 dataset (candidate exponents 2.15/2.37/2.60; the
default 3-candidate grid described above).

### Primary rolling-origin analysis (expanding window, test seasons 2017-2025)

| Test season | Train seasons | Exponent chosen | N | Excluded | Brier | Margin MAE |
|---|---|---:|---:|---:|---:|---:|
| 2017 | 2000-2016 (17) | 2.150 | 256 | 0 | 0.2157 | 10.79 |
| 2018 | 2000-2017 (18) | 2.150 | 254 | 2 | 0.2162 | 10.49 |
| 2019 | 2000-2018 (19) | 2.150 | 255 | 1 | 0.2241 | 10.90 |
| 2020 | 2000-2019 (20) | 2.150 | 255 | 1 | 0.2217 | 10.44 |
| 2021 | 2000-2020 (21) | 2.150 | 271 | 1 | 0.2319 | 11.47 |
| 2022 | 2000-2021 (22) | 2.150 | 269 | 2 | 0.2298 | 9.50 |
| 2023 | 2000-2022 (23) | 2.150 | 272 | 0 | 0.2303 | 10.54 |
| 2024 | 2000-2023 (24) | 2.150 | 272 | 0 | 0.2184 | 10.29 |
| 2025 (validation) | 2000-2024 (25) | 2.150 | 271 | 1 | 0.2205 | 10.64 |

**Aggregate**: N=2375, excluded=8 (all ties, none of them
`InsufficientHistoryError` -- every fold has ample training history by
2017), Brier=0.2233. **Season-clustered Brier 95% CI: [0.2196, 0.2272]**
(resampling whole seasons, not individual games).

**Notable, real finding:** the lowest candidate in the default grid, 2.15,
won *every single one* of the nine 2017-2025 folds against 2.37 (the
currently shipped exponent, from Football Outsiders' published value) and
2.60. This is a real, repeatable-across-many-seasons signal, not a
single-season artifact -- exactly the kind of pattern this ticket exists to
surface. It does **not** change the shipped default here: SUD-38's own
2023-2025-only refit had already selected values in this neighborhood, and
promoting a new exponent is explicitly SUD-132's predeclared gate, not this
ticket's. The finding is: rerun this search with a wider/finer grid
(candidates below 2.15 included) as part of that gate, since every boundary
value in this grid was won by the boundary itself, which suggests the true
optimum over the expanded history may be lower still.

### Robustness check (trained once on 2000-2010, tested on 2011-2016)

Aggregate: N=1531, Brier=0.2171 -- in the same range as the primary
analysis's per-season Brier values, showing no sign that the model behaves
qualitatively differently in the older, 16-game-schedule era.

## What this ticket does not do

- Does not change the shipped default model or its exponent.
- Does not claim 2025 is an untouched holdout -- it is labeled `validation`
  throughout (`VALIDATION_SEASON = 2025`), consistent with roster
  continuity and other recent candidate decisions having already consulted
  it.
- Does not permit 2026 to enter any fold, as a test season or as training
  data (`PROSPECTIVE_LOCKBOX_SEASON = 2026`; enforced structurally, not
  just documented -- `rolling_origin_evaluation`/`robustness_evaluation`
  raise `ProspectiveLockboxViolationError` rather than silently proceeding).
- Does not add a rolling five-to-eight-season *feature* window to the
  Pythagorean model itself -- `compute_team_strength` only ever blends the
  current season with its immediately-prior season by construction, so
  "expanding" vs. "rolling" here governs which seasons' games are pooled to
  select an exponent, not which games enter any single forecast. This is
  disclosed explicitly rather than implied to be a deeper model change.

## Reproduction

```powershell
python -m sgr.cli ingest-historical-seasons --season 2000 --season 2001 ... --season 2022
python -m sgr.cli expand-evaluation --robustness
python -m sgr.cli expand-evaluation --window rolling --rolling-window-seasons 6
```
