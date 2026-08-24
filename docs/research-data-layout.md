# Local research data layout

Generated research artifacts live under `data/research/` by default and are excluded
from Git. The layout is local-only and contains no credentials or Kalshi account data.

```text
data/research/
  raw/<provider>/<retrieval-date>/<sha256>.json
  canonical/<entity-table>.parquet
  research.duckdb
```

Raw JSON files are content-addressed evidence envelopes and are created exclusively;
an existing checksum path is verified rather than overwritten. Canonical records carry
the raw path and checksum, provider IDs, event/retrieval times, and schema version.

DuckDB tables provide the query surface. Each table includes stable ID, entity type,
schema version, indexed event/retrieval timestamps, and the complete canonical JSON.
The Parquet file beside each table is a deterministic export for analysis. Use
`ResearchStore.lineage_for_recommendation` to replay a recommendation through its game,
forecast, market, quote, match decision, and optional settlement outcome.

Schema version changes must be registered explicitly through the canonical reader.
Unregistered versions raise `IncompatibleSchemaVersionError`; fields are never silently
reinterpreted.
