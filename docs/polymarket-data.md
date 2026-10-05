# Public Polymarket data contract

Verified 2026-10-04 against official documentation and bounded public requests.
The connector has a fixed GET allowlist and no account, signing, wallet, or
real-order implementation. Raw observations remain under ignored local data.

- [Keyset discovery](https://docs.polymarket.com/api-reference/markets/list-markets-keyset-pagination):
  GET /markets/keyset, limit 1..100, opaque next_cursor passed as after_cursor.
  Offset is forbidden. The NFL primary tag is discovered through public /sports;
  it also includes unrelated contracts and is not an eligibility signal.
- [Rate limits](https://docs.polymarket.com/api-reference/rate-limits):
  Gamma /markets 300 requests/10 seconds; CLOB /book 1,500/10 seconds.
  This app defaults to at most five requests/second, four concurrent requests,
  three attempts, and a campaign-wide budget including retries. Provider queues
  still require timeouts/backoff. A budget is a ceiling, never a target.
- [Books](https://docs.polymarket.com/api-reference/market-data/get-order-book),
  [tick size](https://docs.polymarket.com/api-reference/market-data/get-tick-size),
  [fee rate](https://docs.polymarket.com/api-reference/market-data/get-fee-rate):
  preserve market condition and asset identities, provider timestamp,
  retrieval/availability time, depth, minimum order size, tick and fee evidence.
  Unknown fees, mismatched identities, duplicate/crossed levels and schema drift
  fail closed. No midpoint execution.
- [Current fee curve](https://docs.polymarket.com/trading/fees):
  fee = shares × (base_fee / 10,000) × price × (1-price).
  Fees and conservative rounding are applied by the paper simulator; no rebates.
  Query the token endpoint rather than infer fees from market category.
- [Public WebSocket](https://docs.polymarket.com/market-data/realtime-data):
  subscribe to selected asset IDs on the market channel. Maintain local depth,
  bounded queues and publish coherent snapshots at decision intervals. A
  disconnect, stale data, reverse timestamp or tick change invalidates the book.
  Reconnect needs a new full snapshot. The feed does not invoke Restate per tick.
  The protocol cannot prove detection of every silently missed update; data is
  best effort, and freshness gates do not constitute an exchange guarantee.

Exact interpretation is a versioned research input: condition, complete rules
hash, cited verbatim rule span, exact outcomes, game/team IDs, kickoff, deadline,
overtime, tie and cancellation semantics. Similar titles are insufficient.
Unreviewed interpretations, negative-risk conversion, spreads, totals, player
props and other unsupported contracts remain unknown. Existing calibration
excludes ties; regular-season winner pricing is therefore rejected until an
appropriate validated tie model exists. Postseason game winners are supported.

[Geographic restrictions](https://docs.polymarket.com/api-reference/geoblock)
would gate any separately proposed live execution. This research tool does not
check accounts, circumvent location controls, or enable live execution.
