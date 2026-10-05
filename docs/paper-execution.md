# Fictional portfolio and research assumptions

Default policy fictional-fixed-v1: 1,000 fictional USD; 25 fixed stake; contract
50, event 100, each correlated team group 150, total open/reserved exposure 400,
realized loss stop 100. Policy content is fingerprinted and frozen per portfolio.
Changing it requires a new portfolio. No Kelly scaling or real account data.
Campaign interpretations, model inputs, versions and decision timestamps are
immutable. Replaying an expired decision retrieves its existing evidence/effects;
it cannot open a new position. A new analysis needs new observations.

SQLite BEGIN IMMEDIATE serializes the capital ledger independently of Restate.
Each decision reserves once; each simulated execution commits one fill operation.
On a lost acknowledgement, read/reconcile the existing fill before retrying.
Contract/event/group caps count reserved capital plus cost of unresolved inventory.
Cancelling partial orders releases only the remainder; disputed inventory continues
to consume exposure. Settlement credits payout once, with explicit rule/source
evidence. Realized P&L is reported separately from depth-based liquidation marks.
The loss stop applies to realized losses, not a prediction of worst possible loss;
the open exposure cap supplies the independent worst-case capital bound.

Execution crosses asks, with at most 25% of displayed depth, 500ms assumed latency,
10-second quote age, price/tick/minimum-size checks, and token-specific current
fees rounded conservatively to microdollars. There are no guaranteed maker fills,
midpoint fills, rebates, atomic multi-leg fills or queue-position assumptions.
Displayed depth is consumed cumulatively per asset and price for the lifetime of
the fictional portfolio across retrieval IDs. This prevents repeated snapshots
from manufacturing liquidity, but also understates real replenishment. A
minimum-size failure rolls back consumption. Data/reconciliation
failure pauses the portfolio. A fresh reanalysis is required after decision expiry.

The model reuses Pythagorean strength, calibration selection and confirmed
injury-impact modules through a price-free, frozen, point-in-time view. Only
source snapshots available by the cutoff enter the view; duplicate provider game
identities are deduplicated and conflicting scores rejected. Calibration fits
only earlier seasons, with its existing chronological internal validation and
uncalibrated fallback recorded. Forecast IDs incorporate frozen input digest,
calibration parameters and model/strategy versions. This prospective availability
check is stricter than older retrospective research paths in the repository.

The opportunity families are hypotheses:
NFL fair price after costs; de-vigged/equivalent benchmarks with identical economic
rules; exhaustive two-outcome cost/depth inconsistencies; confirmed injury
adjustments with timestamped sources. Missing comparable benchmark or injury
history is explicitly unknown. Multi-leg atomic execution is unsupported.

Prior research selected and rejected multiple strategies on 2023–2025 data.
Those seasons are not a fresh holdout for this strategy. Further investment
requires predeclared thresholds and untouched chronological evaluation, with
selection/multiple-testing disclosed. Historical price-only datasets do not
contain execution depth and cannot prove realistic fill P&L. Synthetic lifecycle
results validate accounting and orchestration, never profitability.
