# SUD-60/SUD-91: Injury/availability feed decision matrix

> This document evaluates candidate injury-data providers. It does not commit to a vendor,
> purchase a feed, or add a credentialed integration for SportsDataIO/Sportradar — that
> remains a separate future decision gated on this evidence plus a licensing/cost
> conversation with each vendor's sales team. ESPN is now ingested (SUD-91): read-only,
> free, no vendor relationship required.

## Update (SUD-91, 2026-08-24): ESPN's injury endpoint is confirmed, with an important caveat

Spiked ESPN's `summary?event=<id>` endpoint (same undocumented site API SUD-23 already uses
for scoreboard data). It has both `boxscore` (real, historically accurate per-player stat
lines — verified against a real completed 2025 game) and `injuries` directly, free, no key.

**The critical finding: `injuries` reflects ESPN's *current* team injury report at request
time, regardless of which event/date is queried.** Verified by requesting a 2023 week-1
game's summary and receiving injury entries dated the current day. This means ESPN's
injuries field **cannot reconstruct historical point-in-time injury state for past games** —
querying an old event does not return what was known before that game, only what's current
now. It *is* immediately usable for a live pregame snapshot: querying an **upcoming**
(unplayed) game's summary returns real, populated, current injury entries for both teams
(verified against a real 2026 Week 1 matchup).

This resolves the "ESPN injury-endpoint existence: unconfirmed" row below, and answers the
"night before / weekend games" use case directly and for free — but does not, by itself,
give a path to historically-accurate injury data for backtesting past seasons. That gap can
only be closed by (a) our own snapshot history accumulating going forward, matching the
project's existing point-in-time snapshot philosophy, or (b) a vendor with genuine
historical-replay rights, still unconfirmed for either SportsDataIO or Sportradar below.

## Candidates

| Provider | Access model | Confirmed as of this ticket |
|---|---|---|
| ESPN unofficial site API | No key; same undocumented interface SUD-23 already uses for scoreboard data | **Confirmed and ingested (SUD-91).** `boxscore` is real and historically accurate. `injuries` is real but current-state-only — see the update above. |
| SportsDataIO | Requires a paid API key (`Ocp-Apim-Subscription-Key` header or query param). Their public free trial explicitly excludes NFL (it covers UEFA Champions League only), so NFL injury data requires a paid subscription or a sales conversation. [[Process Guide - Injuries]](https://support.sportsdata.io/hc/en-us/articles/9911200480663-Process-Guide-Injuries), [[NFL API Developer Portal]](https://sportsdata.io/developers/api-documentation/nfl) | Confirmed field semantics and cadence (below); pricing/contract terms not obtained (would require sales engagement, out of scope here). |
| Sportradar | Requires an API key (standard for all Sportradar feeds). [[NFL Update Frequencies]](https://developer.sportradar.com/football/docs/nfl-ig-update-frequencies), [[NFL Weekly Injuries reference]](https://developer.sportradar.com/football/reference/nfl-weekly-injuries) | Confirmed cache/update cadence (below); pricing/contract terms not obtained. |

## Matrix

| Dimension | ESPN (unofficial) | SportsDataIO | Sportradar |
|---|---|---|---|
| **Source authority** | Unofficial, best-effort, no SLA (same caveat SUD-23 already documents for scoreboard data) | Commercial aggregator; licensed data product with a support/process guide | Commercial aggregator; official developer portal with per-endpoint update-frequency documentation |
| **Update latency** | Live/current at request time (confirmed); no documented refresh cadence of its own since it's undocumented | Documented: poll every 5–10 minutes during the day; specifically re-poll after practice reports publish and ~90 minutes before kickoff, when gameday inactive lists are released | Documented: Weekly Injuries endpoint cache TTL is **4 hours** (`Cache-Control: max-age=14400`); recommended pull frequency "every hour or less during the current week." Materially slower than SportsDataIO's near-real-time cadence — by their own docs, **not by itself sufficient for live in-game incident trading** (matches this ticket's own background note) |
| **Historical as-of availability** | **Confirmed absent.** Querying an old event returns today's report, not what was known then (see Update above) — this is the one dimension ESPN concretely loses on, not merely an unknown | Not confirmed in this pass (would need a dedicated historical-replay spike, same category of question SUD-35 answered for ESPN game data) | Not confirmed in this pass |
| **Player/depth-chart identity** | Confirmed: athlete id, display name, and position present on injury entries; position and jersey present on boxscore entries (position sometimes absent there) | Documented distinction between `Status` (roster status: Active/Inactive/Injured Reserve/etc.) and `InjuryStatus` (game status: Probable/Questionable/Doubtful/Out) — gameday inactives are specifically identified as `InjuryStatus=Out` **and** `Status=Active` | Not confirmed in this pass; would need the same schema-level check |
| **Corrections/retractions** | Not exposed as a distinct field — SUD-91 ingests every fetch as its own append-only observation rather than inferring a correction | Not confirmed in this pass | Not confirmed in this pass |
| **Licensing/terms** | Same terms caveat as SUD-23's PRD entry: unofficial interface, review current ESPN terms before automated use, do not represent as independently verified | Free trial excludes NFL; commercial license required — exact terms not obtained | API key required — exact terms not obtained |
| **Cost** | Free (unofficial) | Not obtained — requires sales engagement | Not obtained — requires sales engagement |
| **Rate limits** | Unknown; SUD-91 uses the same cache-first, immutable-snapshot pattern as scoreboard ingestion to stay conservative | Documented per-endpoint polling *recommendations*, not hard rate-limit numbers, in the excerpt reviewed | Cache TTL effectively defines the practical floor (4h) for the injuries endpoint specifically |

## What this evidence supports

- **ESPN is now the default live pregame source, for free, with no vendor relationship.** SUD-91 ingests it directly into the same `AvailabilityReport` contract this document defines, feeding SUD-61's confirmation/resolution logic today.
- **ESPN cannot backtest historical injury correctness for past seasons.** Any walk-forward evaluation of an injury-aware model against 2023-2025 games must either exclude injury features for that period or accept that our *own* accumulated ESPN snapshots (starting now, going forward) are the only historically-honest injury record this project will have without a paid vendor.
- **SportsDataIO's documented cadence and field semantics remain the strongest fit for a future paid pregame source** if ESPN's coverage or reliability proves insufficient: it explicitly separates roster status from game status, and explicitly describes the gameday-inactive release window this project's `T-60m` decision-time snapshot already targets.
- **Sportradar's 4-hour cache makes its Weekly Injuries endpoint unsuitable as the sole source for the live in-game-incident case** — this is Sportradar's own documented cadence, not a guess, and it directly matches the background note already in this ticket.
- No pricing, contract terms, or historical-replay rights were obtainable for either paid vendor without directly engaging their sales teams, which remains out of scope. **Recommendation: rely on ESPN for live pregame injury snapshots now; begin accumulating ESPN's own snapshot history immediately since it cannot be backfilled later; revisit SportsDataIO only if ESPN's coverage proves insufficient once real usage data exists.**

## Provider-neutral contract

The canonical `AvailabilityReport` schema and confirmation policy in `src/sgr/research/availability.py` are built so that whichever provider is eventually selected (or several, cross-referenced) normalizes into the same shape — no provider-specific field name or vendor assumption leaks past the adapter boundary, matching the pattern already established for ESPN (`sgr.connectors.espn`) and Kalshi (`sgr.connectors.kalshi`).
