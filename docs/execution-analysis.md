# Execution latency and suspected stale fills

The imported-history explorer shows measured request latency and cancellation/fill timing from the existing LIP telemetry. It requires no new credentials, worker, bot modification, or trading permissions. Analysis reads a consistent history snapshot; it never submits, changes, or cancels orders.

Choose an account, optionally filter by exact market and operation, and inspect the latest 25 requests. Older requests are paginated. Export includes policy version `execution-1`, raw request records, linked fills, classifications, sample sizes, query limits, and collector freshness. Inspect order evidence opens the existing history search.

## Timing definitions

- **Request elapsed:** the SDK's monotonic duration around the bot's existing request method, including retries and waits.
- **HTTP attempts:** individual transport round trips. The total includes failed attempts. The difference from request elapsed also includes overhead; it is not a pure retry-backoff measurement.
- **Retries:** complete HTTP attempt count minus one.
- **Outcome:** HTTP success, HTTP error, or unknown. A timeout may have measured elapsed time with unknown exchange outcome. These outcomes have separate percentile groups.
- **Percentiles:** nearest-rank p50 and p95, grouped by operation/outcome for the displayed page. P95 needs at least 20 complete samples in that group. These are descriptive page samples, not a rolling service-level measurement or exchange processing latency.

Incomplete lifecycle records, unpaired attempts, missing session sequences, inconsistent identity, and invalid durations make timing unavailable. Unrelated records interleaved within a request do not constitute a gap. Wall-clock jumps disable fill comparisons while leaving valid monotonic latency usable. Data arriving while a request is in flight may temporarily appear incomplete until the next refresh.

## Cancellation/fill classifications

Fills must match the account scope, market, subaccount, and target order ID on the cancel request. Unknown subaccounts are not linked. Amend replacement IDs are never treated as cancel targets. Historical REST order snapshots cannot establish when the bot received a cancellation acknowledgement.

| Classification | Meaning |
| --- | --- |
| Earlier execution imported late | Execution timestamp is more than one second before the cancel request; ingestion happened later. This is not evidence of a stale execution. |
| Execution timestamp before cancel request | Same earlier execution comparison, already imported before the request. |
| Within clock guard | Execution timestamp is within one second of request start; timing uncertain. |
| Cancel in flight or clock uncertainty | Execution timestamp is later than request + one second, but no later than response + one second. Normal cancellation races remain possible. |
| Suspected fill after confirmed cancel response | Execution timestamp is more than one second after a successful response explicitly reporting that exact target order `canceled`. Still suspected, because exchange and local clocks are not synchronized. |
| Fill after unconfirmed cancel | Later execution timestamp, but cancellation was not confirmed. Includes HTTP success without terminal status, errors, and timeouts. Does not establish a stale fill. |
| Timing unavailable | Incomplete request evidence, invalid timestamps, or local wall/monotonic discrepancy greater than 250 ms. |

The one-second guard is a heuristic, not a measured bound on exchange clock offset. Execution uses the fill's exchange timestamp; history `received_at` means import observation only. Confirmation requires both HTTP success and a response reporting `status=canceled` for the target ID. HTTP 200 alone and HTTP 404 never imply terminal cancellation. Current live cancel responses often omit terminal status, so they remain unconfirmed.

No classification creates an incident automatically. Reference-price markouts remain a separate measure of subsequent price movement; these timestamp classifications do not measure adverse selection, incentives, or P&L.

## Coverage and growth

API: `GET /api/history/execution?scope=...&instrument=...&operation=cancel&limit=25&offset=0`. Limit is 1–50 and maximum offset is 100000. Each request includes up to 200 records; larger traces are explicitly unavailable. Session completeness checks span at most 5000 sequence numbers. Each cancellation links up to the latest 100 matching fills with an explicit truncation flag. Repeated cancels may link the same fill; page summaries deduplicate fill event IDs within each classification. A fill can have different classifications relative to different cancellation requests.

No matching retained fills does not establish that no fills occurred. Account collection freshness remains visible above the panel and is included in exports. Earlier fills without retained bot telemetry cannot acquire reconstructed timing. New requests move the offset-based pages during refresh; exports are consistent snapshots, not stable streaming cursors.

This v0 evaluates retained evidence on demand with indexed scope/request/order lookups and bounded response sizes. Request discovery still groups retained telemetry, so database growth will eventually require materialized request summaries and cursor pagination. The pure evaluator and versioned policy can be reused for that change without changing producers or evidence semantics.

Verification: `python -m unittest discover -s tests -v` and `npm.cmd run test:execution-browser`. Browser acceptance creates an isolated synthetic database under `artifacts/browser`, never synthetic production evidence.
