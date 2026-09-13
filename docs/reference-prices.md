# Reference-price capture

An independent, read-only worker captures best bid/ask evidence for the active incentives bots. It discovers markets from scoped, imported telemetry heartbeats, checks their binary-market identity and active status, and subscribes to authenticated Kalshi orderbook updates. It does not load or alter a trading bot. The explorer reads committed local evidence without exchange credentials.

## Run and stop

```powershell
python -m pip install -r requirements-reference.txt
python -m othryss.reference_cli --account my-account
```

Use the same read-only key in `local.env` as the account's historical importer. Startup verifies the stored credential binding and read-only API scope. `--environment demo` selects the separate demo host and account namespace. Authenticated redirects are refused. The only WebSocket commands are market-data subscription and snapshot refresh; REST requests verify permissions and market metadata.

The default paths are `artifacts/reference/quotes.sqlite`, historical input `artifacts/history/othryss.sqlite`, and stop file `artifacts/reference/worker.stop`. Override with `--db`, `--history-db` and `--stop-file`. Reference storage must be separate from history. An OS-held lock prevents duplicate reference writers and releases after a crash.

Stop with Ctrl+C, or create the stop file:

```powershell
New-Item -ItemType File -Force artifacts/reference/worker.stop
```

Wait for the worker to exit before removing that exact stop file and restarting. Never start a second writer against the same database. Locally deployed worker PID and logs are saved under `artifacts/reference/`; the history collector and trading bots run independently. This is a local process, not a reboot-persistent hosted service.

## Evidence and quality

The worker reconstructs full depth in memory from a fresh snapshot and contiguous deltas. It stores only normalized best bid/ask prices, top sizes, midpoint and quality after each accepted snapshot or delta. YES ask equals one dollar minus the best NO bid. Prices and fractional quantities remain decimal strings, with USD/contract and YES outcome basis declared.

Each quote has a version, account scope, connection ID, subscription ID and sequence. Sequence is checked across the whole subscription, including updates for different markets and sequenced control acknowledgements. Duplicate, skipped, malformed or unscoped updates invalidate the connection; reconnect begins from new snapshots. Reconstructed books and raw depth are not persisted, so this store supports replay of captured top-of-book observations, not independent revalidation of all historical depth.

`source_at` retains the exchange timestamp when supplied. `received_at` is local processing receipt time; `received_monotonic_ns` records the local monotonic clock. A snapshot without an exchange timestamp has `source_at: null` and `clock_basis: local_receipt_only`. Source time is never invented from receipt time. Monotonic times are not comparable across hosts or reboots.

A reference requires both sides and a positive spread. Empty, one-sided, locked and crossed books have no midpoint. When the exchange timestamp differs from local receipt by more than two seconds behind or one second ahead, the quote is marked `timing_uncertain` and has no midpoint. This checks observed timestamp disagreement; it does not establish clock synchronization or precise network latency. Wide spreads and the bots' own resting orders may affect midpoints; no fair-value or profitability claim is implied.

The worker pings every five seconds and requests full snapshots every 15 seconds. Missing initial snapshots after 15 seconds, missing refreshed snapshots after 30 seconds, disconnects and invalid sequences trigger recovery. A gap starts conservatively at the last locally verified connection/book point when available and ends only with a fresh snapshot. Open gaps survive process restarts. A crash is detectable from stale worker health before its gap is persisted on restart.

The read API additionally rejects current references if receipt age exceeds 30 seconds, worker heartbeat age exceeds 15 seconds, the worker is not streaming, the market is unwatched, or a gap remains open. These are display/current-state checks; historical quote quality alone is insufficient to approve an entire future markout interval.

Market discovery and lifecycle checks run roughly every 30 seconds. Bot heartbeats must be within 180 seconds and not stopped. Departed markets are retained for another 60 seconds to support future 30-second horizons. Closed/nonbinary markets and failed lifecycle checks stop capture; stale selection beyond 60 seconds invalidates the stream. At most ten markets are watched, including departure tails; exceeding this limit fails closed with visible worker error state.

## Storage, inspection and limits

Reference evidence uses a separate WAL SQLite database, so its writes do not contend for the historical collector's writer lock. Retention runs approximately every 30 seconds: three days or the newest one million quote records per scope, whichever is smaller. Bursts can temporarily exceed the row target between maintenance passes. Closed old gaps and unwatched markets without retained evidence are pruned. SQLite reuses freed pages; pruning does not immediately shrink the file. This is a bounded local pilot, not a measured production throughput guarantee.

The **Reference prices** panel refreshes with the selected history account every 15 seconds. Select a market to inspect its latest 200 normalized observations and latest 50 gap records. **Export displayed references** exports that bounded view, explicitly preserving quote timing, quality and connection identity. It is not a full-history export. `GET /api/history/references?scope=...&instrument=...&limit=200` is read-only, validates the history account, and permits at most 500 quotes. The explorer accepts `--reference-db` for isolated saved stores.

The explorer's [Fill markouts](markouts.md) evaluator computes 1/5/30-second receipt-time estimates with additional scope, direction, timing and interval checks. Fills outside retained capture remain unavailable. Snapshot receipt time cannot support a claim of exact exchange-time pricing by itself. Execution-quality alerts are not implemented.

## Validation

```powershell
python -m unittest discover -s tests -v
npm.cmd run test:reference-browser
```

Synthetic tests cover complementary prices, fractional depth, empty/invalid books, subscription-wide sequences, delayed clocks, duplicate evidence, scope separation, retention, stale health, snapshot-gated gap recovery, worker reconnection, read-only HTTP bounds and browser display/export. Live checks confirmed all three current bot markets, periodic refresh snapshots and incoming deltas. No live order submissions or cancellations are needed.

Protocol references checked September 10, 2026: [Kalshi orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates), [WebSocket connection](https://docs.kalshi.com/websockets/websocket-connection), [orderbook price interpretation](https://docs.kalshi.com/getting_started/orderbook_responses), and the installed websockets 16.1 client implementation for redirect and connection behavior.
