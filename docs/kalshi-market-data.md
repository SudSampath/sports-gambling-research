# Kalshi read-only market data

SUD-24 implements a credential-free, GET-only connector for Phase 1 NFL
research. It targets `https://external-api.kalshi.com/trade-api/v2` and exposes
series, events, markets, single and batched orderbooks, the historical cutoff,
and live/historical market lookup. It contains no order, cancel, portfolio,
balance, transfer, or funding method.

## Contract baseline

The offline contract fixture was checked on 2026-08-03 against Kalshi's
[OpenAPI schema](https://docs.kalshi.com/openapi.yaml),
[API changelog](https://docs.kalshi.com/changelog),
[public market-data quickstart](https://docs.kalshi.com/getting_started/quick_start_market_data),
[fixed-point migration](https://docs.kalshi.com/getting_started/fixed_point_migration),
[orderbook guide](https://docs.kalshi.com/getting_started/orderbook_responses), and
[historical-data guide](https://docs.kalshi.com/getting_started/historical_data).

A credential-free production smoke check on 2026-08-03 confirmed that
`KXNFLGAME` returned `quadratic_with_maker_fees`, an open 2026 NFL market,
cursor pagination, and all required `*_dollars` and `*_fp` fields.

## Normalization and rejection

- Dollar prices and fixed-point counts are parsed as `Decimal`; removed legacy
  cent/count fields are never used as fallbacks.
- Each normalized market retains provider and event tickers, participant side,
  status, rules, occurrence and close times, four quoted prices, available
  sizes, volume, open interest, fee type, and retrieval time.
- Kalshi orderbooks contain YES and NO bids. The executable YES ask is
  `1 - best NO bid`, and the executable NO ask is `1 - best YES bid`; ask size
  comes from the corresponding opposite-side bid level.
- Empty, malformed, crossed, or stale books raise a typed rejection. The
  connector never substitutes midpoint or last-trade prices.
- HTTP 429 and transient server failures use a three-attempt exponential
  backoff bounded at two seconds. Response bodies are excluded from errors.

## Historical replay

Before reading an old settled market, the connector reads
`/historical/cutoff`. Markets settled before `market_settled_ts` route to
`/historical/markets/{ticker}`; newer markets remain on `/markets/{ticker}`.

When a snapshot store is configured, raw JSON is content-addressed by SHA-256.
Every observation also receives immutable metadata containing its public
endpoint, retrieval timestamp, normalization version, and raw-content hash.
Generated snapshot directories should remain local and untracked.
