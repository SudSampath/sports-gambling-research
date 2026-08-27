# Assess coaching turnover — 2026-08-27

Linear: [SUD-114](https://linear.app/sudsampath/issue/SUD-114/assess-coaching-turnover)

**This is a research spike only.** No ingestion, schema, model variant, or
shipped-default change is implemented in this ticket -- every mechanism
below is a proposal for a future ticket to build and walk-forward evaluate
against real data, not something adopted here.

## 1. Data source research (real, verified this session)

### Pro-Football-Reference / Sports-Reference network -- blocked

Every direct fetch this session against `pro-football-reference.com` and
`sports-reference.com` (the coaches index page, the glossary page, the
data-use page, `robots.txt` itself) returned **HTTP 403 Forbidden** to this
project's fetch tooling -- consistent with active bot detection, not a
one-off failure. A web search for their published policy corroborates
this: Sports Reference LLC's Terms explicitly prohibit "automated means to
access or use the Site, including scripts, bots, scrapers, data miners, or
similar software" without express written permission; their documented
rate limit is 10 requests/minute for FBref/Stathead and 20/minute for
their other sites, with violators placed in a request "jail" for up to a
day; and most of their underlying data is licensed from third parties
under terms that explicitly forbid redistributing it as a bulk download.
PFR's own coach pages are known (from general football-analytics
familiarity, not something this session could directly render past the
403) to carry full head-coach tenure history with win-loss/playoff
records and an "interim" annotation where applicable -- the richest
schema of any candidate source -- but it is **not usable here** without a
paid Stathead subscription or a written data-license agreement, neither
of which this project has. Recorded as a blocker for any future ticket
that would depend on PFR specifically, not something to route around by
scraping anyway.

### ESPN's undocumented coaches API -- accessible, but schema-poor

`site.api.espn.com`'s own documented team/scoreboard endpoints (already
used by `EspnConnector`) do not expose coaching staff. A separate,
undocumented host does: `sports.core.api.espn.com`. Two endpoint shapes
were fetched and inspected directly this session:

- `GET /v2/sports/football/leagues/nfl/seasons/{year}/coaches?limit=50` --
  a season-wide, paginated index of `$ref` links (32 entries for 2024, one
  per team _as of whenever ESPN's snapshot was taken_).
- `GET /v2/sports/football/leagues/nfl/seasons/{year}/teams/{teamId}/coaches`
  -- a team-scoped list, also `$ref` links.

Dereferencing individual coach records (e.g.
`.../coaches/2471205?lang=en&region=us`) returns only `id`, `uid`,
`firstName`, `lastName`, `experience` (a plain integer), `birthPlace`, and
`college`/`person` sub-references -- **no role/title field, no team
reference, no effective start/end date, and no win-loss record** in the
base payload. This was confirmed concretely, not assumed: team 2's
(Buffalo Bills) `/coaches` endpoint for the 2024 season resolved to **Joe
Brady**, that team's offensive coordinator that season, not head coach
Sean McDermott -- with nothing in the payload to indicate that distinction.
A separate season-wide lookup (`/coaches/4872749`) resolved to **Mike
LaFleur**, an offensive coordinator elsewhere that season. In other words:
this endpoint mixes head coaches and coordinators under one shape with no
role field to tell them apart, and ESPN's own site-search results describe
it plainly as an "unofficial/undocumented" API not intended for
programmatic reliance. Historical depth is unknown and unverified --
these fetches only checked the 2024 season; whether `{year}` resolves
correctly for `EspnConnector`'s existing 2000+ historical floor was not
tested and would need its own verification before any ingestion is built.

### nflverse -- no coaching dataset exists

This project's primary, already-trusted free data source
(`NflverseConnector`, used for schedules, play-by-play, and rosters) was
checked directly against the public `nflverse-data` GitHub releases index.
**No release tag for coaches, coaching staff, or team personnel exists** --
confirmed by listing every release tag present (`trades`, `teams`,
`schedules`, team/player summary stats, `ftn_charting`, `espn_data`,
`weekly_rosters`, `players`, etc.). Any coaching feature therefore cannot
reuse this project's existing, already-vetted ingestion path the way
SUD-121 (win totals), SUD-123 (play-level features), and SUD-127 (game
context) all did -- a materially higher cost than those tickets, not a
detail to gloss over.

### Other candidates surfaced, not verified further

A third-party research note (`gtonic/nfl_mcp`'s own coaching-data survey,
read for cross-reference, not copied as authoritative) independently
arrived at the same "ESPN Core API is the most practical free option, but
it's undocumented" conclusion, and separately flagged Wikipedia's
per-season coordinator list pages and several commercial APIs
(SportsDataIO, Sportradar) as alternatives. Wikipedia (CC-BY-SA, no
scraping restriction analogous to Sports Reference's) is a plausible free
fallback for historical HC/OC/DC identity and rough tenure years, but its
coverage, consistency, and interim/title-change handling were not
verified this session and would need their own spike before use.

### Access-method conclusion

No source checked this session offers, for free, a schema with role
taxonomy (HC vs. OC vs. DC), effective start/end dates, and historical
depth all at once. The two free options each cover part of the
requirement: ESPN gives current-snapshot access with no role field;
nflverse gives none at all. A future ingestion ticket would need either
(a) a paid PFR/Stathead subscription or written license, (b) a
repeated-polling strategy against ESPN's coaches endpoint to reconstruct a
timeline from many current snapshots taken over time (which this project
does not have retroactively -- it would only start accumulating history
from whenever such polling began), or (c) a from-scratch, manually
curated timeline (e.g. seeded from Wikipedia, cross-checked against ESPN)
that carries its own accuracy risk this project cannot independently
verify at scale. None of these is a "free lunch" the way nflverse's
schedules/pbp data was for prior tickets.

## 2. Interim appointments, in-season firings, shared duties, title changes -- scoped, not solved

Given the access-method conclusion above, this spike scopes (does not
solve) these cases for whichever future ticket implements ingestion:

- **In-season firings / interim HC**: ESPN's coaches endpoint is a live
  snapshot, not a historical record -- it would show whoever holds the
  role *right now*, silently overwriting a mid-season change rather than
  recording it as an event, unless polled frequently and diffed. A first
  version should scope to **season-opening coach of record only**
  (whoever started Week 1), flag in-season changes as a known, explicitly
  out-of-scope gap, and treat the handful of real in-season firings per
  year as missing data rather than guessing an effective date.
- **Shared play-calling duties**: several teams historically split
  play-calling between an OC and a HC, or between co-coordinators. A first
  version should record a single "primary" playcaller per side of the
  ball per team-season where identifiable, and treat a genuinely
  ambiguous split as missing (not arbitrarily assigned to one name) --
  the same "missing stays missing" discipline this project's `.env`/CLAUDE
  working agreement already applies elsewhere.
- **Title changes** (e.g., an OC promoted to HC on the same staff, a
  passing-game coordinator retitled OC): a future schema should key
  continuity on the **person**, not the title string, so a promotion is
  correctly modeled as HC continuity + OC change, not as two unrelated
  people.
- **Point-in-time eligibility**: whatever timeline is eventually ingested,
  only staff information knowable as of a game's own `feature_cutoff_at`
  is eligible -- the same discipline every other point-in-time feature in
  this project already enforces (`generate_forecast`, `compute_team_efficiency`,
  etc.). A coaching change announced after a game's cutoff must not
  retroactively appear in that game's features.

## 3. Proposed coaching-continuity mechanism (design only, not implemented)

Following the established shape (`turnover_adjustment.py`,
`sos_adjustment.py`, `context_effects.py`, `matchup_interactions.py`,
`scoring_luck.py`): three separate binary/count features, not one blended
"coaching disruption" score, so each can be fit and ablated independently
per the AC:

| Candidate feature | Proposed side affected | Rationale |
|---|---|---|
| `head_coach_is_new` (this season vs. last, same team) | Both points-for and points-against (split, same halves-each pattern `turnover_adjustment.py` uses) | A new HC can change both offensive and defensive scheme/philosophy, or neither directly, depending on background |
| `offensive_coordinator_is_new` | Points-for only | Directly analogous to `TeamGameEfficiency`'s own offense-only fields -- an OC change should not be assumed to move points allowed |
| `defensive_coordinator_is_new` | Points-against only | Symmetric to the OC feature |

Each would enter as an additive logit coefficient
(`sigmoid(logit(baseline) + hc*hc_new + oc*oc_new + dc*dc_new)`), fit via
`context_effects.fit_context_coefficient` on training-fold seasons only,
exactly the pattern SUD-126/127/129 already use -- reusing
`fit_context_coefficient` again rather than a fourth reimplementation of
the same golden-section search. A **tenure-length** feature (seasons in
role, capped and shrunk the way `SHRINKAGE_PSEUDOGAMES`-style constants
already shrink small samples elsewhere) is a natural second-phase
extension once the binary "is new" version is evaluated, not a
first-version requirement.

Coefficients would be calibrated the same way every other model-family
ticket this session calibrated its own: real project games plus the
ingested coaching timeline, rolling season-held-out evaluation (2023-2025
test seasons, same as SUD-124/126/129), reporting Brier/log loss/margin
MAE by season against the shipped baseline -- not adopted unless it wins
without regression, same gate every other candidate in this project has
had to clear.

## 4. QB/roster confound and multicollinearity

This is the single biggest risk a future implementation ticket would need
to design around, not an afterthought:

- **Literature caution, not a production constant** (per the AC): general
  football-analytics research finds new head coaches improve their team's
  record roughly 63% of the time by an average of ~1.3-2.0 wins in year
  one, and one academic study (Bryson et al., *Scottish Journal of
  Political Economy*, 2024) attributes roughly 20-30% of team-outcome
  variation to coaches. But a widely cited FiveThirtyEight analysis
  reaches the opposite conclusion once it controls for the team's own
  prior-season Elo rating: most of the "new coach bump" is regression to
  the mean (bad teams fire coaches and would have improved somewhat
  anyway), not a coaching effect net of team quality. **This project
  should treat any first-pass coefficient as an upper bound contaminated
  by this confound, not a validated causal effect size** -- exactly the
  same posture this session already took toward turnover margin in
  SUD-129 (a feature that fit sanely in two folds and catastrophically
  overfit in a third, due to a structurally similar mechanical
  entanglement with a team's own win/loss record).
- **QB confound specifically**: a new HC/OC very often arrives alongside a
  new starting quarterback (a coaching change and a QB change are two of
  the most correlated roster events in the league). A future evaluation
  should report a stratified or interaction breakdown -- coaching change
  with QB continuity vs. coaching change with simultaneous QB change --
  rather than a single pooled coefficient that cannot distinguish the two.
  This project does not currently have a canonical "starting QB
  continuity" signal to condition on; building one is plausibly its own
  future ticket, not a detail to improvise inside this one.
- **Sample size**: roughly 32 teams x however many seasons are eventually
  ingested, but head-coaching changes happen for only a fraction of teams
  each year (historically on the order of 15-20% of the league per
  season) -- a few hundred HC-change team-seasons at most across this
  project's full 2000-2025 window, and OC/DC changes are both more
  frequent (higher base rate of turnover) and far less reliably
  documented by any free source checked above (title changes, shared
  duties, and interim appointments all reduce how many coordinator-season
  observations are actually clean). A future ticket's evaluation should
  report this coverage/missingness explicitly, the same way SUD-122's
  coverage reports already do for game data, rather than silently
  dropping ambiguous team-seasons.

## 5. Entity/schema fields a future ingestion ticket would need (sketch only, not created)

Not added to `schemas.py` in this ticket. A future `CoachingAssignment`-
shaped canonical record would plausibly need: `team_id`, `season_year`,
`role` (`head_coach` / `offensive_coordinator` / `defensive_coordinator`),
`person_name` (and ideally a stable person identifier, not just a name
string, so a title change is detected as continuity), `effective_start_at`
/ `effective_end_at` (nullable if unknown -- see the interim/in-season
scoping above), `is_interim: bool`, and the usual `CanonicalRecord`
provenance fields (source snapshot, retrieved-at). Given the access-method
findings above, the realistic first version would likely only populate
`effective_start_at` reliably (season-opening coach of record) and leave
`effective_end_at` mostly null until a repeated-polling or PFR-licensed
source exists.

## What this ticket does not do

- Does not ingest any coaching data, add any schema, or implement any
  model variant.
- Does not scrape Pro-Football-Reference or Sports-Reference in violation
  of their documented terms -- every fetch against those domains that
  returned 403 was left as a 403, not retried through a workaround.
- Does not claim the literature-cited win-lift figures as this project's
  own calibrated effect size -- they are reported here only as an
  external prior for a future ticket's evaluation to be checked against,
  per the AC's own instruction.
- Does not resolve SUD-128 (Model Coaching Interactions)'s other
  blocker -- SUD-125 remains blocked on the SUD-118 PR #30 human merge
  decision, already recorded separately in Linear.
