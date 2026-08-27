# Historical preseason win-total source spike — 2026-08-27

Linear: [SUD-121](https://linear.app/sudsampath/issue/SUD-121/source-win-total-history)

## Decision

**No historical backfill.** No source found during this spike offers a free,
licensed, systematically-accessible history of preseason NFL team season
win-total lines. Actual-win comparison in SUD-132's historical table stays
model-only for every season except where a market column is explicitly
documented as available. This is a valid, documented negative result, not a
gap to paper over.

**Forward collection is viable starting the 2026 season** through the
existing, already-integrated, no-paid-plan Kalshi connector (`KalshiConnector`,
SUD-24) -- see "Recommended forward path" below. A follow-up implementation
ticket should be drafted once Kalshi's 2026-27 win-total markets are open and
liquid (not yet true as of this spike; see the Kalshi section).

## What this ticket is not

- Not game point totals (over/under points scored in one game) -- SUD-119
  already ingests those from nflverse's `total_line` and they are a
  completely different market from a *team's season win total*.
- Not Super Bowl or conference-championship futures -- those settle on a
  single binary outcome, not a team's regular-season win count.
- Not a scraper, a vendor purchase, or a claim that any of the sources below
  were queried under a commercial data license.

## Candidates evaluated

| Source | Seasons | Method | Price/juice | Access | Cost | Licensing/redistribution | Point-in-time fidelity | Classification |
|---|---|---|---|---|---|---|---|---|
| **The Odds API** (`americanfootball_nfl`) | n/a | Verified live via `GET /v4/sports` on 2026-08-27: `"key": "americanfootball_nfl", "has_outrights": false`. Only `americanfootball_nfl_super_bowl_winner` carries `has_outrights: true` (championship winner, not team win totals). | n/a | n/a | n/a | This provider does not carry the market at all for NFL team win totals, at any plan tier. | n/a | **Unsuitable** -- market does not exist on this provider |
| **nflverse release assets** | n/a | Verified live via the nflverse-data GitHub Releases API on 2026-08-27: 25 release tags, none named/tagged win-totals, futures, odds, or market-adjacent beyond the `schedules` (game-level spread/total/moneyline, already ingested in SUD-119) and `pbp` releases. | n/a | n/a | n/a | nflverse does not publish this dataset at all. | n/a | **Unsuitable** -- dataset does not exist on this provider |
| **Kalshi `KXNFLWINS-<TEAM>` series** (e.g. `KXNFLWINS-DEN`) | Confirmed via live `GET /trade-api/v2/series` and `/events` calls on 2026-08-27: series exists (`product_metadata.scope: "Wins"`, `frequency: "annual"`), but only one season of events (`KXNFLWINS-DEN-25`, `KXNFLWINS-DEN-25B`, both "2025-26 season") exists per team checked; the nested-markets query for `KXNFLWINS-DEN-25` returned an **empty markets array** at query time. | Kalshi prices contracts as event probabilities (cents), not bookmaker odds/juice; a resolved win-total product would need its own strike-ladder (over/under N wins, or exact-win-count buckets -- `KXNFLEXACTWINS<TEAM>` series exist alongside `KXNFLWINS-<TEAM>`, suggesting Kalshi may offer both a ladder and an exact-count product). | Free, public, unauthenticated GET (no API key required for series/events/markets endpoints; this is the same read-only surface `KalshiConnector`, SUD-24, already uses) | None -- Kalshi's terms already govern the existing connector | ESPN listed as the series' `settlement_sources`, matching this project's own data source, which simplifies later result verification. Only **one season of events observed** (2025-26); no market snapshot history was checked in this ticket (that is SUD-36/SUD-120's territory, not this one) | **Usable with restrictions** -- free and no new legal/contract review needed, but zero historical depth confirmed so far, and the one event checked had no populated markets |
| VegasInsider "NFL Win Totals" pages | Publicly viewable current-season page; historical seasons not offered through any documented API | Manual web page, no API | Displayed juice, not timestamped | Browser page only; no terms reviewed for systematic access in this ticket | Free to view | Not reviewed -- no license permitting redistribution or systematic collection was found or sought in this spike | Unknown; page reflects "now," not a fixed publication timestamp | **Unsuitable** -- no API, no terms review, no historical archive |
| The Action Network / Sports Insights / Unabated (subscription odds-tracking products) | Current and some historical data behind paid subscriptions | Web/app, some vendor APIs exist but are commercial | Included in some tiers | Requires a paid plan and a redistribution/terms review not performed here | Paid (tier-dependent, not investigated further since a purchase decision is out of this ticket's scope) | Not reviewed | Not established | **Unsuitable for this ticket** -- would require a purchase and terms review this spike is explicitly not authorized to make |
| Sports Reference / Pro-Football-Reference | All seasons (actual final win/loss records only) | Already used by this project for actual outcomes | n/a -- no win-total line data published here at all | n/a | Free | n/a | n/a | **Not a line source** -- confirms actual wins only; already the source of truth for the "actual wins" column, not a market column |

## Why game totals and championship futures are not this market

- nflverse's `total_line` (SUD-119) is a **single game's** combined-score
  over/under, refreshed every week -- unrelated to a team's expected win
  count across an entire season.
- The Odds API's and Kalshi's championship/conference-winner markets settle
  on "did this team win the whole thing," a much rarer and differently
  distributed outcome than "how many regular-season games did this team
  win." Mistaking either for a season win total would silently corrupt any
  win-total MAE/RMSE comparison in SUD-132.

## Data contract this spike would require if a source becomes viable

Even though nothing is being implemented in this ticket, the shape a real
source must satisfy is fixed here so a later ticket does not redesign it:

- **Distinct observations for opening, a fixed decision-time snapshot, and
  the final preseason line** -- never substitute a later line for an earlier
  missing one (the same rule SUD-119/SUD-120 already enforce for game lines).
- **Settlement convention recorded explicitly**: regular-season length for
  the season in question (17 games since 2021, 16 before), how ties count
  (nflverse's own game data already represents ties as neither a win nor a
  loss -- SUD-119's `ClosingLine`/`Game` pair -- and any win-total product's
  settlement rule for ties must be recorded, not assumed, since some
  sportsbook win-total products count a tie as half a win).
- **Provider IDs, retrieval timestamp, and source checksum** on every
  observation, matching every other canonical entity in `schemas.py`.
- A canonical `SeasonWinTotalLine`-shaped record (team_id, season_year, line
  value, juice/price where available, snapshot kind
  [opening/decision/final], source, retrieved_at) -- deliberately not
  specified further here; the implementation ticket owns the exact schema.

## Recommended forward path

1. Do not purchase, scrape outside permitted terms, or backfill historical
   preseason win totals in this project at this time.
2. Re-check Kalshi's `KXNFLWINS-<TEAM>` and `KXNFLEXACTWINS<TEAM>` series
   once the 2026 season's markets open (expected shortly before the 2026
   season begins, since the 2025-26 season's events were found with no
   populated markets even after that season had already completed) --
   confirm strike ladder, liquidity, and whether Kalshi's settlement source
   (ESPN) resolves ties the same way this project's own `Game` records do.
3. If confirmed liquid and stable, open a follow-up implementation ticket
   (drafted below) scoped to the existing `KalshiConnector` -- forward
   collection only, starting with the 2026 season; no historical claim.
4. Until then, SUD-132's historical comparison table reports market
   win-total columns only where this ticket establishes trustworthy
   coverage -- which, as of this spike, is **no season** -- and every other
   season's market win-total columns are explicitly omitted rather than
   estimated or backfilled from a different market type.

## Draft follow-up ticket (create when Kalshi's 2026 win-total markets are confirmed liquid)

**Title:** Capture Kalshi season win-total markets (forward-only)

**As a** developer with an AI coding agent, **I want** the existing
read-only Kalshi connector to snapshot `KXNFLWINS-<TEAM>` markets at
documented offsets before and during the season **so that** 2026-forward
preseason win-total lines become an honest, timestamped market benchmark
without any historical-backfill claim.

- Given the 2026 season's `KXNFLWINS-<TEAM>` events are open and quoting
  When a snapshot is captured
  Then series, event, market, strike ladder, best executable prices, and
  retrieval timestamp are stored with the same provenance discipline as
  `ClosingLine` (SUD-119) and `KalshiSnapshotHistory` (SUD-36)
- Given a tie-inclusive or tie-exclusive settlement rule is published in the
  series' contract terms
  When the observation is normalized
  Then the settlement convention is recorded verbatim, not inferred
- Given no snapshot exists at a target offset
  When the collection window closes
  Then the missing observation is recorded explicitly, never interpolated
- Given this is a forward-only source
  When SUD-132's historical table is built
  Then only 2026-forward seasons may show a Kalshi-sourced win-total column,
  and every earlier season's column states "no market source" rather than
  a blank cell that could be misread as zero

## Reproduction (evidence commands run for this spike)

```powershell
# The Odds API: confirm americanfootball_nfl has no outrights market
curl.exe -s "https://the-odds-api.com/liveapi/guides/v4/#historical-odds" | Select-String "has_outrights"

# nflverse: confirm no win-total/futures release exists
curl.exe -s "https://api.github.com/repos/nflverse/nflverse-data/releases" |
  python -c "import json,sys; print([r['tag_name'] for r in json.load(sys.stdin)])"

# Kalshi: confirm the KXNFLWINS-<TEAM> series family and its event history
curl.exe -s "https://api.elections.kalshi.com/trade-api/v2/series?category=Sports" |
  python -c "import json,sys; [print(s['ticker'], s['title']) for s in json.load(sys.stdin)['series'] if 'WINS-' in s['ticker']]"
curl.exe -s "https://api.elections.kalshi.com/trade-api/v2/events?series_ticker=KXNFLWINS-DEN&limit=100"
curl.exe -s "https://api.elections.kalshi.com/trade-api/v2/events/KXNFLWINS-DEN-25?with_nested_markets=true"
```

These are research signals about data-source availability, not financial,
betting, or licensing advice, and none of this ticket's findings imply a
purchase decision or a claim of any project's future profitability.
