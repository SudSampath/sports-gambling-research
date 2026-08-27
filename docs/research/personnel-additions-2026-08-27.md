# Personnel-additions source spike — 2026-08-27

Linear: [SUD-116](https://linear.app/sudsampath/issue/SUD-116/assess-personnel-additions)

## Decision

**Viable, with one critical point-in-time trap identified.** nflverse
publishes real, free, already-permitted-to-use datasets for draft capital
(`draft_picks`), trades (`trades`), and free-agent contracts
(`contracts`, republished from Over The Cap). All three were downloaded
and inspected live on 2026-08-27. A concrete follow-up implementation
ticket is drafted below, gated on SUD-115's roster-continuity coordination
per this ticket's own instruction, and not implemented here (research
spike only).

## Sources evaluated

| Source | Seasons | Access method | Schema (verified live) | Point-in-time fidelity | Classification |
|---|---|---|---|---|---|
| nflverse `draft_picks` | Every NFL draft (whole-history single CSV) | Free, public GitHub release asset, same connector pattern as SUD-118/119/123 | `season, round, pick, team, gsis_id, pfr_player_id, ..., position, college, age, to, allpro, probowls, seasons_started, w_av, car_av, dr_av, games, pass_completions, ..., def_sacks` | **Draft slot itself (season/round/pick/team/position/college) is point-in-time safe** -- knowable the moment the pick is made, months before the season it affects. **The value/production columns (`allpro`, `probowls`, `seasons_started`, `w_av`/`car_av`/`dr_av`, `games`, and every counting stat) are cumulative career totals through the `to` (final season) column** -- confirmed live: e.g. Ezekiel Elliott's 2016 draft row (`season=2016`) carries `games=135`, `rush_yards=9130`, `to=2024`, his entire career. Using these columns as "how good was this pick" for a 2016-season forecast would leak nine future seasons of production into a point-in-time feature. | **Usable with restrictions** -- draft slot/position/college only; the value columns are explicitly excluded from any point-in-time feature |
| nflverse `trades` | Whole-history single CSV | Free, public GitHub release asset | `trade_id, season, trade_date, gave, received, pick_season, pick_round, pick_number, conditional, pfr_id, pfr_name` | `trade_date` is a real transaction date -- point-in-time safe by construction. Verified live: a 2021-10-25 trade correctly carries a **future** conditional pick (`pick_season=2024`), confirming the schema already distinguishes when a trade happened from when a traded asset resolves. | **Usable** |
| nflverse `contracts` (Over The Cap via nflverse, not a direct OTC scrape) | Whole-history single CSV (`historical_contracts.csv.gz`) | Free, public GitHub release asset | `player, position, team, is_active, year_signed, years, value, apy, guaranteed, apy_cap_pct, ..., draft_year, draft_round, draft_overall, draft_team, season_history` | `year_signed` is point-in-time safe (a real signing-event year). **`is_active` and `apy_cap_pct` read as current-status fields, not point-in-time-as-of-signing** -- would need verification against a specific historical snapshot before use, not assumed safe from this spike alone. | **Usable with restrictions** -- `year_signed`/`value`/`apy`/`guaranteed` as of signing; `is_active` not verified as historically point-in-time |
| Pro Football Reference / Over The Cap direct scraping | N/A | Not evaluated -- this project already has a standing rule (SUD-118) not to scrape these sites directly; nflverse's republished datasets above are the permitted path | N/A | N/A | **Out of scope** -- superseded by the nflverse datasets above |

## Distinguishing knowable-at-cutoff facts from later outcomes

- **Knowable before the season**: draft slot, position, college, trade
  date and pick(s) exchanged, contract signing date/years/value. All of
  these are fixed facts the moment the transaction happens, well before
  Week 1.
- **Not knowable, and must never enter a preseason feature**: rookie
  performance (`w_av`/`car_av`/games/every stat column on `draft_picks`),
  final depth-chart role, future snap share, whether a free agent "worked
  out," and (per `contracts`) whether a contract signed years ago is still
  `is_active` today.

## Proposed mechanism (not implemented here)

A **position-weighted incoming/outgoing value feature**, deliberately
separate from SUD-118's roster-continuity signal:

- For each team-season, at the preseason cutoff: sum of incoming value
  (drafted rookies by round/pick slot -- a *draft-slot* prior, e.g. an
  average historical AV by pick-slot bucket fit from **prior draft
  classes' now-complete career totals**, never the current class's own
  future totals -- plus free-agent signings by `apy_cap_pct` at signing)
  minus outgoing value (departed players' *prior-season* production, the
  same currency roster-continuity already uses).
- Adjusts blended points-for/points-against by position group (offensive
  skill positions affect points-for; defensive front-seven/secondary
  affect points-against), mirroring `player_impact.py`'s existing
  position-aware structure rather than inventing a new one.
- Every coefficient (draft-slot-to-value curve, free-agent
  value-to-production conversion) must be fit from this project's own
  historical games plus point-in-time-safe nflverse data -- never copied
  from an external source, per this ticket's constraint.

## Entity resolution and edge cases

- `draft_picks`/`contracts`/`trades` all carry a `pfr_id`/`pfr_player_id`
  identifier -- the same PFR-namespace ID space `roster_continuity.py`
  already uses for snap counts, so entity resolution reuses that existing
  convention rather than introducing a new player-ID namespace.
- Unsigned draft picks, compensatory picks, released/retired players, and
  players who never make a roster all have explicit rows/absence in these
  datasets (a compensatory pick is still a normal `draft_picks` row; a
  released player simply stops appearing in current-season `rosters`) --
  no special-case parsing invented beyond what SUD-118's existing
  `rosters`/`weekly_rosters` handling already does.

## Comparison requirement (for the eventual implementation ticket)

Per this ticket's AC, any implementation must compare against **both** the
shipped baseline **and** a roster-continuity-only candidate (SUD-118), so
double-counting between "low continuity" and "low incoming value" is
measurable rather than assumed independent.

## What this ticket does not do

- Does not ingest any data, add a schema, or implement a connector --
  research and source classification only.
- Does not adopt any external AV/valuation number as a coefficient; every
  value curve is explicitly flagged as "to fit from this project's own
  data," not to copy from Pro Football Reference's own published methodology.
- Does not change the shipped default model.

## Draft follow-up ticket (create when prioritized against SUD-113/114/115's other continuity signals -- SUD-117's decision gate)

**Title:** Model position-weighted personnel additions

**As a** developer with an AI coding agent, **I want** a point-in-time
incoming/outgoing personnel-value signal from nflverse's `draft_picks`,
`trades`, and `contracts` releases **so that** preseason forecasts can
represent material talent changes independent of roster continuity.

- Given a team-season's preseason cutoff
  When incoming value is computed
  Then only `draft_picks` slot/position/college and `contracts`
  `year_signed`/`value`/`apy`/`guaranteed` are used -- never any
  cumulative career/production column from a draft class whose season has
  not yet completed
- Given a departing player
  When outgoing value is computed
  Then it uses that player's own prior-season production (the same
  currency roster-continuity already uses), not an external valuation
- Given the roster-continuity signal (SUD-118) already exists for the
  same team-season
  When the personnel-addition candidate is evaluated
  Then baseline, roster-continuity-only, personnel-addition-only, and
  combined variants are compared on real rolling held-out seasons so
  double-counting is measurable, not assumed
- Given the variant does not beat the roster-continuity-only candidate
  When the ticket concludes
  Then it remains rejected or opt-in, and the shipped default is unchanged

## Reproduction (evidence commands run for this spike)

```powershell
curl.exe -sL "https://github.com/nflverse/nflverse-data/releases/download/draft_picks/draft_picks.csv" -o draft_picks.csv
curl.exe -sL "https://github.com/nflverse/nflverse-data/releases/download/trades/trades.csv" -o trades.csv
curl.exe -sL "https://github.com/nflverse/nflverse-data/releases/download/contracts/historical_contracts.csv.gz" -o contracts.csv.gz
```

These are research signals about data-source availability and design, not
financial, betting, or licensing advice, and imply no purchase or
scraping decision.
