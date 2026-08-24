# SUD-60: Injury/availability feed decision matrix

> This document evaluates candidate injury-data providers. It does not commit to a vendor,
> purchase a feed, or add a credentialed integration — per SUD-60's scope, that remains a
> separate future decision gated on this evidence plus a licensing/cost conversation with
> each vendor's sales team.

## Candidates

| Provider | Access model | Confirmed as of this ticket |
|---|---|---|
| ESPN unofficial site API | No key; same undocumented interface SUD-23 already uses for scoreboard data | We have *not* verified an injury/availability endpoint exists on ESPN's site API — SUD-23 only exercised the scoreboard endpoint. Confirming this requires its own spike against a live response, not assumed here. |
| SportsDataIO | Requires a paid API key (`Ocp-Apim-Subscription-Key` header or query param). Their public free trial explicitly excludes NFL (it covers UEFA Champions League only), so NFL injury data requires a paid subscription or a sales conversation. [[Process Guide - Injuries]](https://support.sportsdata.io/hc/en-us/articles/9911200480663-Process-Guide-Injuries), [[NFL API Developer Portal]](https://sportsdata.io/developers/api-documentation/nfl) | Confirmed field semantics and cadence (below); pricing/contract terms not obtained (would require sales engagement, out of scope here). |
| Sportradar | Requires an API key (standard for all Sportradar feeds). [[NFL Update Frequencies]](https://developer.sportradar.com/football/docs/nfl-ig-update-frequencies), [[NFL Weekly Injuries reference]](https://developer.sportradar.com/football/reference/nfl-weekly-injuries) | Confirmed cache/update cadence (below); pricing/contract terms not obtained. |

## Matrix

| Dimension | ESPN (unofficial) | SportsDataIO | Sportradar |
|---|---|---|---|
| **Source authority** | Unofficial, best-effort, no SLA (same caveat SUD-23 already documents for scoreboard data) | Commercial aggregator; licensed data product with a support/process guide | Commercial aggregator; official developer portal with per-endpoint update-frequency documentation |
| **Update latency** | Unknown — endpoint existence unconfirmed | Documented: poll every 5–10 minutes during the day; specifically re-poll after practice reports publish and ~90 minutes before kickoff, when gameday inactive lists are released | Documented: Weekly Injuries endpoint cache TTL is **4 hours** (`Cache-Control: max-age=14400`); recommended pull frequency "every hour or less during the current week." Materially slower than SportsDataIO's near-real-time cadence — by their own docs, **not by itself sufficient for live in-game incident trading** (matches this ticket's own background note) |
| **Historical as-of availability** | Unknown | Not confirmed in this pass (would need a dedicated historical-replay spike, same category of question SUD-35 answered for ESPN game data) | Not confirmed in this pass |
| **Player/depth-chart identity** | Unknown — would need to join against ESPN's roster/athlete endpoints, separate from the scoreboard endpoint SUD-23 uses | Documented distinction between `Status` (roster status: Active/Inactive/Injured Reserve/etc.) and `InjuryStatus` (game status: Probable/Questionable/Doubtful/Out) — gameday inactives are specifically identified as `InjuryStatus=Out` **and** `Status=Active` | Not confirmed in this pass; would need the same schema-level check |
| **Corrections/retractions** | Unknown | Not confirmed in this pass | Not confirmed in this pass |
| **Licensing/terms** | Same terms caveat as SUD-23's PRD entry: unofficial interface, review current ESPN terms before automated use, do not represent as independently verified | Free trial excludes NFL; commercial license required — exact terms not obtained | API key required — exact terms not obtained |
| **Cost** | Free (unofficial) | Not obtained — requires sales engagement | Not obtained — requires sales engagement |
| **Rate limits** | Unknown | Documented per-endpoint polling *recommendations*, not hard rate-limit numbers, in the excerpt reviewed | Cache TTL effectively defines the practical floor (4h) for the injuries endpoint specifically |

## What this evidence supports

- **SportsDataIO's documented cadence and field semantics are the strongest fit for the pregame use case** (practice reports, gameday inactives) based on public documentation alone: it explicitly separates roster status from game status, and explicitly describes the gameday-inactive release window this project's `T-60m` decision-time snapshot already targets.
- **Sportradar's 4-hour cache makes its Weekly Injuries endpoint unsuitable as the sole source for the live in-game-incident case** — this is Sportradar's own documented cadence, not a guess, and it directly matches the background note already in this ticket.
- **ESPN's fit is genuinely unknown**, not merely deprioritized: this ticket did not confirm whether an injury/availability endpoint exists on the same unofficial site API SUD-23 uses. That is the cheapest thing to check next (no new vendor relationship required) before any paid commitment.
- No pricing, contract terms, or historical-replay rights were obtainable without directly engaging each vendor's sales team, which is explicitly out of this ticket's scope. **Recommendation: spike the ESPN injury-endpoint question first (free, no new vendor relationship); treat SportsDataIO as the leading paid candidate for the pregame path pending a licensing conversation; do not rely on Sportradar's weekly-injuries endpoint alone for the live in-game path.**

## Provider-neutral contract

The canonical `AvailabilityReport` schema and confirmation policy in `src/sgr/research/availability.py` are built so that whichever provider is eventually selected (or several, cross-referenced) normalizes into the same shape — no provider-specific field name or vendor assumption leaks past the adapter boundary, matching the pattern already established for ESPN (`sgr.connectors.espn`) and Kalshi (`sgr.connectors.kalshi`).
