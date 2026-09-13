# Source health, recovery and expected retirement

Source monitoring distinguishes a stopped producer, a current monitoring failure, and recovered telemetry with historical errors. Incident evidence and cumulative counters are retained.

## Expected LIP retirement

The collector optionally reads the configured LIP supervisor log. Set the absolute path in `ops.local.json` as `lip_supervisor_log`, or pass `--lip-supervisor-log` to the collector. This is explicitly trusted local integration evidence for the collector's account. It does not change the trading supervisor or control a process.

A retirement requires an explicit completed eviction line, a preceding matching `child_launch`, one imported LIP `PRODUCER_START` for that market/primary subaccount within ten seconds of the log filename's launch time, a heartbeat older than 180 seconds, and an exited/absent Windows PID. An active/reused PID, access denial, absent log, ambiguous launch or fresh heartbeat cannot establish retirement. Log reads use a bounded 4 MiB tail and discard a partial first line; missing older launch context yields unknown. A new heartbeat invalidates stored retirement evidence. Once the session exit is verified, that historical fact survives log rotation and later PID reuse. Older records that lost the verified-exit flag may recover it only from a matching retained health check; an arbitrary active PID cannot establish a new retirement. The recorded retirement observation time is the collector's observation time, not an invented shutdown timestamp.

Expected retirement or an explicit producer shutdown supplies clear evidence only for `source_stale`. Two separate health checks close an existing source incident with a shutdown explanation. Position/order/fill discrepancies are not cleared. Retirement does not certify cancellation or a flat exchange position; those require exchange evidence. A process that disappears without an explicit eviction/shutdown remains stale. Historical errors remain visible after retirement.

## Recovering from transient errors

SQLite schema 7 adds `source_health` to retain recovery baselines and retirement evidence. The observer samples imported heartbeats; read-only API queries never advance recovery. A source with historical event-writer or dropped-record counters can recover after at least 120 seconds with unchanged error counts, fresh heartbeats, advancing sequence and a new bot-state observation after the baseline. Event-writer/drop counter changes, a stale heartbeat, a heartbeat time reversal, capacity pressure or an observer interruption reset the baseline. Re-reading a fixed heartbeat is insufficient.

An isolated heartbeat-file error does not make fresh, complete telemetry unhealthy; if publication actually stops, normal heartbeat staleness still applies. The writer-recovery quiet period remains 120 seconds. With fresh exchange coverage and contiguous bot data, its incident-opening grace is at least 300 seconds so brief recovery does not generate an open/resolved pair at the boundary. Ongoing writer errors still open an incident after that grace. Missing records, capacity pressure, stale sources and failed exchange captures retain their ordinary configured grace. Read-only exchange capture continues during a writer quiet period, while trading comparisons remain suspended until recovery.

All imported session sequence numbers must remain contiguous, and all records reported in the heartbeat must have reached the collector. True gaps, invalid spool evidence, stale bot state or a failed exchange audit still prevent a healthy comparison. This recovery policy does not erase historical missing records or change request-level latency/markout policies. An existing source incident clears only after fresh exchange coverage and two clear checks, unless an expected shutdown ends monitoring.

## SDK 0.3.1

Heartbeat-file errors increment `heartbeat_failures`, separately from `write_failures` and `dropped`. `last_error_stage`, `last_error_type` and `last_error_at` identify the failure without storing exception messages, paths or credentials. Heartbeat replacement retries Windows sharing errors up to three attempts, with 20 ms waits on the background writer only. Event writing, serialization, rotation and flush errors remain possible data-loss indicators. Queue/capacity behavior is unchanged. The importer accepts both older contracts and 0.3.1.

Replacing the installed SDK file affects newly launched probes. Already running probes keep their loaded code; collector-side quiet-period recovery also supports their existing 0.3.0 counters. No restart is needed for the collector-side retirement and recovery policy.

## Notifications

Discord and general webhooks share the same source summary: an allowlisted cause, validated last heartbeat and bounded integer session error totals. Expected retirement is described as monitoring ending, rather than exchange/trading recovery. Raw check evidence, supervisor paths, exception messages and review notes remain local. Existing delivery deduplication, retry policy and route activation cursors are unchanged; historical opened alerts are not replayed for this upgrade.
