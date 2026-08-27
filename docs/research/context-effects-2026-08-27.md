# Game-context effects (rest/venue/surface) — 2026-08-27

Linear: [SUD-127](https://linear.app/sudsampath/issue/SUD-127/model-context-effects)

## Decision

**No-build.** Rest-days differential and dome/outdoor roof, tested as
additive logit adjustments to the shipped baseline and fit on real training
folds, add essentially no incremental value on real held-out 2023-2025
games. The shipped default is unchanged. This is consistent with the
published research the PRD itself cites (no significant current bye
advantage).

## What this ticket built

- **`GameContext`** (`game_context.py`): rest days, divisional-game flag,
  roof, and surface, sourced from nflverse's `games.csv` (the same
  whole-history release `ClosingLine`/SUD-119 already uses) and joined to
  the canonical `Game` via the same ESPN-event-ID identity join. Real
  coverage verified live for 2011-2025: rest days and roof are **100%**
  covered (3,919/3,919 real games); surface is 99%+; final observed
  temp/wind (retained for provenance only, never a feature -- see below)
  covers ~65-75% depending on season (missing for dome games, as expected).
- **`context_effects.py`**: `rest_days_differential` (home minus away rest
  days) and a dome/outdoor indicator, each entering the model as an
  *additive logit adjustment* on top of the shipped baseline's own
  forecast (`sigmoid(logit(baseline) + coefficient * feature)`) rather than
  a new team-strength model -- matching the ticket's framing as a
  "constrained interaction" ablation on the existing model. Both
  coefficients are fit inside the training fold only
  (`fit_context_coefficient`, the same golden-section search
  `pythagorean.fit_exponent` uses).

## What was deliberately not built, and why

Per the AC's own allowance ("a documented no-build or metadata-only result
is acceptable"), this ticket does **not** implement:

- **Travel distance / time-zone change / local kickoff time**: would
  require a stadium-coordinates reference table this project does not have
  and did not compile for this ticket. `GameContext.game_id` plus
  nflverse's own `stadium`/`stadium_id` columns (present in the same
  `games.csv`, not yet ingested here) are a viable path for a focused
  follow-up ticket.
- **Weather as a pregame feature**: this project has no archived
  pregame-forecast weather source. nflverse's `temp`/`wind` columns are
  the **final observed** conditions, not a forecast available at the
  decision timestamp -- using them as a pregame feature would leak
  information exactly the way a closing market line would. They are
  ingested and retained on `GameContext` for provenance/benchmark analysis
  only, mirroring `ClosingLine`'s benchmark-only discipline, and are never
  read by any forecast-generating code path.
- **Franchise relocation / temporary venue / neutral-international-venue
  handling**: `Game.neutral_site` already exists and is unaffected by this
  ticket; a dedicated relocation/temporary-venue history was not built
  since none of the real 2011-2025 evaluation data required it to produce
  a usable result.

## Real result

`evaluate-context-effects`, one rolling fold (train 2011-2022, test
2023-2025), rest and dome coefficients fit on the training fold only:

| Configuration | N | Excluded | Brier | Log loss |
|---|---:|---:|---:|---:|
| baseline | 815 | 1 | 0.2233 | 0.6384 |
| rest-only (coefficient 0.0100) | 815 | 1 | 0.2232 | 0.6380 |
| venue-only (coefficient -0.0431) | 815 | 1 | 0.2234 | 0.6385 |
| combined | 815 | 1 | 0.2233 | 0.6382 |

All four configurations are effectively indistinguishable (Brier differs
in the fourth decimal place). Context coverage was complete: 816/816
scored games had a `GameContext` record. The fitted coefficients
themselves are tiny (0.01 logit-per-rest-day; -0.043 for indoor venue),
consistent with "no meaningful effect" rather than "an effect this
evaluation failed to detect."

## Real performance issue found and fixed while building this

The evaluation initially took over 4 minutes and was killed as
apparently hung. Root cause: this ticket's branch had not yet merged
SUD-122's `generate_forecast` caching fix (see
`docs/research/expanded-evaluation-2026-08-27.md`) -- without it, scoring
~1,600 real games (12 training + 3 test seasons) reloaded and re-parsed
the entire games table on every single forecast call. Merging that fix in
brought the same real evaluation down to 31 seconds. Documented here as a
reminder for any future ticket branch that needs to score more than a
handful of real seasons: merge in SUD-122's branch (or rebase once it
merges to `main`) before assuming a multi-season real evaluation will run
in reasonable time.

## What this ticket does not do

- Does not change the shipped default model.
- Does not claim rest/venue effects are absent in all contexts -- only
  that a simple linear differential/indicator, fit on 2011-2022 and tested
  on 2023-2025, adds no measurable value. A more targeted interaction
  (e.g. short-week-specifically, or extreme-cold-specifically) is a
  narrower, separately falsifiable hypothesis this ticket does not rule out.
- Does not incorporate market/betting-line data.

## Reproduction

```powershell
python -m sgr.cli ingest-game-context --season 2011 ... --season 2025
python -m sgr.cli evaluate-context-effects --train-season 2011 ... --train-season 2022 --test-season 2023 --test-season 2024 --test-season 2025
```
