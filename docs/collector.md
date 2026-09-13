# Incremental collection

The collector is a separate process with the same verified read-only Kalshi key and account scope as the historical importer. The explorer only reads committed local data; it does not start collection or load credentials.

```powershell
python -m othryss.collector_cli --account my-account
# One bounded cycle; useful for validation or an external scheduler:
python -m othryss.collector_cli --account my-account --once
```

Defaults: 60 seconds between completed cycles, 300 seconds of fill overlap, 500 records per page, 1,000 pages per cycle, and a full history repair every 86,400 seconds. Timing flags are `--interval`, `--overlap` and `--full-rescan`. `--db`, `--workspace`, `--environment`, `--env-file`, `--key-id-env` and `--key-file` select the existing configuration boundaries. This collector is account-wide; it does not accept a ticker filter.

Stop with Ctrl+C. The current request may finish first; committed pages remain recoverable. Running the same command resumes the saved window and cursor automatically. One OS-held database import lock prevents simultaneous collectors or a manual import racing the collector. It releases on process exit, including a crash. The explorer remains available while collection runs.

For a background process, optionally use a local stop file:

```powershell
python -m othryss.collector_cli --account my-account --stop-file artifacts/history/collector.stop
# From another terminal to stop it gracefully:
New-Item -Path artifacts/history/collector.stop -ItemType File -Force
# Before restarting:
Remove-Item -LiteralPath artifacts/history/collector.stop
```

The stop file is checked between pages and during waits (at most 30 seconds between checks while idle). The standalone collector command installs no startup task. For managed startup and recovery, use the supervisor and optional Windows task described in [operations](operations.md).

## What is collected

Each normal cycle traverses all current order pages, preserving new order states, and retrieves fills from the last completed cycle's start minus the overlap through the new cycle's fixed start time. Fill timestamps are bounded in integer Unix seconds; overlap revisits boundary seconds. Empty successful windows still advance the checkpoint. Timestamps of the latest observed fill do not determine freshness.

Order scans deliberately have no creation-time filter: an older order can change after it was created. Kalshi documents current order snapshots and keeps resting orders in the current tier. The collector does not assume an order-update cursor. [Get Orders](https://docs.kalshi.com/api-reference/orders/get-orders)

The current fills endpoint supports `min_ts` and `max_ts`. Those parameters are allowed only on that endpoint in this client. [Get Fills](https://docs.kalshi.com/api-reference/portfolio/get-fills)

Full traversals cover current orders/fills followed by archived orders/fills. They run on bootstrap when there is no completed account-wide baseline, daily, when a cutoff changes, or when catch-up would cross the fill archive boundary. A cutoff change during a traversal prevents checkpoint advancement and schedules another full pass. Kalshi archives older orders and fills behind separate endpoints. [Historical data](https://docs.kalshi.com/getting_started/historical_data)

The overlap covers ordinary delayed visibility; the periodic full pass repairs records that appear later than the overlap. Polling cannot capture every transient order state, supply bot submit/ACK timing or guarantee an atomic account snapshot. An optional [one-market position reconciliation pilot](reconciliation.md) now captures positions and settlement evidence after successful account cycles. Incentives, strategy attribution and settlement P&L remain outside collection.

## Recovery and health

SQLite schema 2 adds one collection-state row per account. Existing schema 1 data migrates transactionally when a writer opens the database. The reader supports both versions. The first authenticated migration was preceded by a local SQLite backup at `artifacts/history/before-collector-v2.sqlite`.

The active import link is committed with import creation. Each page atomically commits normalized events, retained source evidence and its next cursor. Only a fully traversed, cutoff-stable cycle advances the fill window. A crash after import completion but before the collection-state update recovers by finishing that update. Page budgets pause and resume without moving the window forward. Resumed cycles retain their original time bounds and page size.

Bounded HTTP retries handle network failures, rate limits and server errors. Failed cycles back off from the configured interval up to 15 minutes; successful cycles clear the failure count. Read scope is reverified each cycle, and subaccount changes are rejected. An HTTP 400 after a stored cursor clears the active run and forces a full retry without advancing coverage. Other malformed data or normalization errors remain visible and retain their checkpoint for investigation.

The UI polls a lightweight health endpoint every 15 seconds without resetting order selection. It reports last success, last attempt, next attempt, errors, completed window start and worker heartbeat. Data becomes stale when the completed window start is older than the greater of five minutes or three collection intervals. A heartbeat older than two minutes is marked missing; an orderly shutdown is explicitly stopped. Freshness is separate from whether new trades occurred. The order list updates when the user selects **Refresh local data**.

## Storage and scaling

Unchanged polling payloads are counted as duplicates without retaining another full copy or evidence link. Pages retain counters and checkpoints; newly inserted event evidence stores each original row index with its allowlisted source row. Historical manual imports retain their previous evidence-link behavior. Normalized identities and exact decimals are unchanged.

This bounds per-response browser work and avoids accumulating thousands of identical order payloads per minute. Order scan cost still grows with the current-tier order count, and page/run metadata accumulates. A larger deployment will need retention policies, measured request budgets, an update stream for orders, and a migrated storage implementation. Polling does not yet establish hosted capacity or real-time latency guarantees.

## Validation

53 Python tests passed, including overlapping late fills, changed old orders, process restart, graceful stop-file shutdown, page budgets, failure recovery, expired cursors, changing archive cutoffs, crash recovery between commits, duplicate payload retention, schema migration, subaccount isolation, read-only enforcement and freshness aging. Both browser suites passed, including stale/error status updates that preserve the selected evidence.

The first authenticated cycle was deliberately paused after one page and resumed in a separate process. Run `84123b405325464b8b6f6ee3aef84f5a` completed as an incremental traversal, inserting 23 order observations, skipping 4,642 duplicates and finding no new fills in its window. The checkpoint advanced only after all 11 pages completed. These observations validate the real API path and restart behavior; synthetic tests exercise new and delayed fills.
