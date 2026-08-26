# Research prompt for Codex: QB/coaching/roster continuity signals

## Context (read first)

This repo's win-probability model (`src/sgr/research/pythagorean.py`) is a
Pythagorean expectation built only from points scored/allowed, blended with
the team's prior season when the current season has few games played. It
has no awareness of *who* is on the team -- no QB, coach, or roster signal
of any kind.

A real backtest exposed the cost of that gap. Using only 2024 data (exactly
the same "pure prior-season carryover" mechanism the model uses for every
team's 2026 preseason number right now), the model's 2025 preseason win
projections missed real outcomes by an average of **2.81 wins per team
(MAE), 3.46 RMSE**, with several dramatic misses:

| Team | Projected (2024 carryover only) | Actual 2025 | Miss |
|---|---|---|---|
| New England | 6.10 wins (26th of 32) | 14-3 | **+7.90** |
| Jacksonville | 6.35 wins | 13-4 | **+6.65** |
| Arizona | 9.71 wins (11th) | 3-14 | **-6.71** |
| Washington | 10.01 wins (10th) | 5-12 | **-5.01** |

Separately, SUD-108/110/112 (see `docs/PRD.md` history and closed Linear
tickets SUD-108/109/110/111/112) already tested and *rejected* three other
candidate fixes on real held-out data: strength-of-schedule adjustment,
turnover-margin normalization, and shrinking the prior season toward
league-average. All three were properly calibrated from this project's own
data and walk-forward evaluated -- SOS and turnover-shrinkage measurably
hurt accuracy; prior-season shrinkage helped early-season calibration only
in a statistically insignificant-to-marginal way and is not adopted. None
of them address the actual mechanism behind the misses above, because none
of them add new *information* -- they all just reshape the same
points-for/points-against signal.

## The actual research question

Real handicappers and predictive-rating systems treat QB continuity,
coaching change, and roster/draft turnover as separate, explicit inputs --
not something inferred from last season's score margin. This project has
never modeled any of them.

**Research and propose (do not implement yet):**

1. **What real, ideally free or low-cost data sources exist** for:
   - Starting-QB continuity/change year over year (who started Week 1 last
     season vs. this season, and mid-season starter changes)
   - Head coach / offensive coordinator / defensive coordinator turnover
   - Roster continuity or turnover (e.g. Over The Cap's roster turnover
     methodology, snap-count continuity, cap dollars retained vs. departed)
   - Draft capital invested (especially early-round picks at premium
     positions) and notable free-agency additions

   For each source: is it scrapable/API-accessible, what's the actual
   schema/shape of the data, how far back does history go, and would it
   require the same point-in-time discipline this project already enforces
   (SUD-25's `feature_cutoff_at` boundary -- no using information that
   wasn't actually knowable at forecast time)?

2. **How would each signal actually enter the model?** This project's
   existing pattern (see `src/sgr/research/turnover_adjustment.py` and
   `src/sgr/research/sos_adjustment.py` for the established style) is: a
   real-data-calibrated adjustment to blended points-for/against, evaluated
   as a new `evaluation.py` baseline and walk-forward compared against the
   shipped default -- never assumed to help, always measured. Propose a
   concrete mechanism per signal (e.g., "a new starting QB with <N prior
   starts should discount X% of the prior season's points-for" -- with X
   to be calibrated from real data, not assumed) that would fit this same
   discipline.

3. **Which signal is likely to matter most, and why**, given the specific
   misses above are dominated by exactly this kind of question (was New
   England's 2025 turnaround a coaching change, a QB development story, or
   both? Same question for Jacksonville's jump and Arizona/Washington's
   collapses). A literature/precedent-based estimate of effect size is
   fine here -- this project will calibrate the real number itself once
   something is implemented.

## What NOT to do

- Do not write implementation code yet -- this is a research and proposal
  task. Follow this repo's own workflow (`DEVELOPMENT.md`): a Linear ticket
  with Given/When/Then acceptance criteria comes first, written after this
  research lands.
- Do not assume an external effect size transfers unchecked -- every
  existing calibrated constant in this codebase (home-field points term,
  turnover discount, prior-season shrinkage rate) was fit from this
  project's own real 2023-2025 data, not copied from outside sources. Any
  proposal should say explicitly what real data in this repo would be used
  to calibrate it.
- Outputs remain research signals only, never advice/picks/tips --
  see `docs/PRD.md`'s Responsible Use section.

## Data pointers

**This repo (schema/architecture Codex should read first):**
- `README.md` -- "Model and methodology" section (the current model, and
  the "what was tried and did not help" history)
- `docs/PRD.md` -- full project rationale and responsible-use rules
- `DEVELOPMENT.md` -- workflow (ticket-first, BDD, PR review)
- `src/sgr/research/schemas.py` -- canonical data model (`Game`, `Team`,
  `PlayerGameStatline`, `AvailabilityReport`, etc.)
- `src/sgr/research/pythagorean.py` -- the core model this would extend
- `src/sgr/research/turnover_adjustment.py` and `sos_adjustment.py` -- the
  established pattern for a calibrated, evaluated candidate adjustment
- `src/sgr/connectors/espn.py` -- the only data connector currently wired
  up; ESPN's public site API is the existing precedent for what this
  project already knows how to ingest (game data, player boxscores,
  current injury reports)

**External sources to start from (real, publicly known to exist -- verify
current access/terms before proposing ingestion):**
- Pro Football Reference (coaching history, starting QB by week, franchise
  encyclopedia pages) -- pro-football-reference.com
- Over The Cap (roster turnover, cap allocation, free agency tracking) --
  overthecap.com
- ESPN's own team/roster/depth-chart endpoints (same provider this project
  already uses for games and injuries) -- may expose coaching staff and
  starter history alongside what's already ingested

**Local data note:** this project's actual research database
(`data/research/`, DuckDB + immutable JSON snapshots) is git-ignored and
local-only -- it is not reachable from a GitHub-hosted Codex session. If
Codex has read access to a checked-out copy of this repo with that data
already ingested, it can query it directly via `ResearchStore`
(`src/sgr/research/storage.py`); otherwise, treat the schema files above as
the source of truth for what fields exist, and note in the proposal what
new ingestion would be required.
