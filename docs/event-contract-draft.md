# Draft event contract informed by the incentives bot

Status: design sketch, not a frozen public SDK. The discovery fixture is intentionally smaller than this target contract.

## Envelope

Every event needs a schema version, stable event identity, event type, logical workspace namespace, source identity, producer session, source sequence where available, timestamps, provenance and a typed payload. Trading context includes venue, account/subaccount, instrument, optional strategy, optional local order ID, exchange order ID and client order ID.

Account and strategy identity must be explicit bindings when established. In historical imports, unavailable values remain null and coverage states why. Production deduplication must scope exchange IDs to the relevant account and venue; fixture aliases are not production identities.

Separate `occurred_at` (source timestamp), `received_at` (first collector observation) and `ingested_at` (durable ingestion). Preserve source timestamp resolution and clock domain. An offline import time cannot replace a missing historical receive time. Use monotonic elapsed measurements for request latency within one process.

Record source payload location/hash and adapter version so evidence can be traced and translations reproduced. Apply retention and redaction rules to stored source payloads; do not copy arbitrary bot internals.

## Event families

| Family | Semantics |
| --- | --- |
| Order intent | Optional local intention; not a submission. Correlation/group ID can link multiple legs without putting basket policy in the core. |
| Order request | Submission, cancel or amend requested; request ID, local/client order IDs, action and monotonic timing context. |
| Order response | ACK, reject, timeout or transport failure. Timeout means outcome unknown, not rejected. Preserve returned identities and amend old/new order linkage. |
| Order observation | Source-reported state at a point in time. Must not be promoted into an ACK or cancellation transition merely because state changed. |
| Fill | Stable execution identity, exchange order identity, normalized exposure direction, decimal quantity/price/fees, units, liquidity classification and source timestamp. |
| Position/balance observation | Explicit entity scope, units, valuation conventions, snapshot identity, as-of information and completeness. Distinguish bot-calculated positions from independently fetched exchange positions. |
| Market/reference observation | Instrument, valid bid/ask/reference values, timestamp/sequence, quality and gap status. An incentive scoring reference is a distinct reference type. |
| Source health | Connected/stale/recovering, last successful update, known gaps, dropped event count, queue overflow and snapshot recovery status. |

`BOT_STATE_OBSERVATION` in the discovery fixture packages the legacy report's combined local order/position sample without claiming stronger source semantics. A production adapter can emit typed order and position observations tied to the same snapshot ID.

## Required distinctions from this first example

1. **Origin and independence:** a saved exchange fill object remains exchange-origin evidence even though the bot stored it. A bot inventory derived from those same fills cannot establish independent agreement with the exchange's position service.
2. **Order chains:** amendments may return a different exchange order ID. Keep a durable logical-order identity plus old/new exchange relationships. Entry and exit are distinct orders; their grouping does not make them one lifecycle.
3. **Partial fills:** keep original requested quantity, cumulative filled quantity and remaining quantity as separate fields when supported. Legacy `order_size` is only a reported observation, with adapter-defined interpretation and confidence.
4. **Economic direction:** the selected exit fill carries legacy `action=sell`, `side=no`, and `book_side=ask`. Do not infer exposure from the legacy side field alone. This adapter uses explicit book direction and YES-basis price for this verified example; retain source values for auditing and reject unsupported ambiguity.
5. **Exact decimals:** 49.32 contracts and a $0.194800 fee occur in real saved records. Decimal/fixed-point values are required at ingestion and in calculations.
6. **Unknown versus healthy:** the absence of recorded cancel timing, a local order ID, or a position feed is not proof that cancellation succeeded, no orders exist, or reconciliation passed.

## Minimum future Python telemetry

Instrument request boundaries for submit/cancel/amend and their responses/errors; local order/position snapshots with complete declared scope; producer start/restart; heartbeat and dropped-event counters. Strategy ID and correlation/group ID are sufficient attribution inputs; fair values and model metadata remain optional.

Emit through a bounded nonblocking queue into an independent collector. A network failure, full queue or disabled collector must leave the trading call operational, while exposing the telemetry gap. Do not add Othryss as an order router.

The collector separately captures exchange orders/fills/positions and relevant quotes. Reconciliation compares observations with aligned scope, freshness and an explicit propagation policy. This independent feed is required before making a trustworthy local-versus-exchange mismatch claim.
