# Bot-state reconciliation and incident workflow

This implementation extends the all-probes LIP telemetry integration with operational state capture, independent exchange audits and an evidence-backed review workflow. It runs locally and makes no exchange writes. External notification delivery is configured separately. Trading restarts use the existing controlled stack procedure.

## What is observed

SDK contract 0.2.0 adds `BOT_STATE` and `BOT_STATE_UNAVAILABLE`; contract 0.1.0 request telemetry remains readable. The hook wraps the probe's existing `step(live)` method and preserves its result or exception. It samples operational fields after a live step at most once every ten seconds, plus failed-step observations. Dry-run steps are excluded. Snapshot construction or publishing errors cannot fail the trading step.

Snapshots declare the starting account-market position, run inventory and their sum in signed YES contracts. The bot's inventory is relative to its initial baseline, so comparing inventory alone against the account position would be wrong. That baseline can include previous activity. Manual trading and other strategies can explain later differences; a difference is a review candidate, not an attribution of fault.

The active order set includes the entry order and the exit manager's order. Entry remaining quantity is observed; exit remaining quantity is unavailable because the exit manager does not retain it. The snapshot also carries bot-owned order IDs and locally seen fill IDs. Only these fields are included; model internals, strategy features, arbitrary logs, credentials and full fill objects are excluded.

Ownership and fill-ID sets are limited to 64 entries each. Explicit flags mark truncation; truncated sets suspend affected fill checks. The bounded queue and 32 MiB unreclaimed-data cap remain. SDK 0.3.0 rotates sealed segments and the Othryss supervisor reclaims only collector-acknowledged evidence, allowing long-running producers to resume after capacity pressure. See [operational reliability](operations.md).

## Comparison rules and coverage

The collector discovers instrument/subaccount/session bindings from validated state observations. It audits full current and historical order/fill history for each active market, then captures current orders, positions, market status and settlements through the dedicated read-only client. Pagination and account/subaccount identities must be complete. At most ten distinct market/subaccount groups are captured per cycle. Multiple healthy producers in the same market/subaccount suspend comparison because account-position attribution is ambiguous.

The comparison needs bot observations at least two seconds before and after the exchange request window, no more than 180 seconds away. Both must be successful-step observations with the same declared operational state. Changing state, incomplete exchange data, unknown subaccount identity, stale input, closure or settlement prevents a conclusive comparison. These timing margins are a conservative local-host policy, not an exchange atomicity guarantee.

Rules compare position, presence of locally active orders, exchange resting orders absent from the bot, entry remaining quantities, exchange fills missing locally, and local fill IDs absent from the exchange audit. Fill matching requires exact IDs on bot-owned orders. Unknown exchange orders are labelled as unattributed activity that needs review. Missing position rows remain unknown; zero is never inferred. A comparison with only some available fields is labelled **partial**. Decimal comparison allows 0.000001 contracts to accommodate the legacy bot's float representation.

Producer staleness, missing bot-state samples, sequence gaps, dropped records, spool verification errors and missing/stale exchange captures feed the source-health rule. The age limit is 180 seconds. Gracefully stopped producers and verified expected LIP retirements are shown as stopped. Source-health incidents can close after two shutdown checks; unresolved trading discrepancies remain uncertain. Transient telemetry errors can recover after a quiet period with fresh, advancing, contiguous evidence; historical counters remain visible. See [Source health recovery](source-health-recovery.md). The collector must be running to generate new incidents; the explorer independently shows stale source/check timestamps if collection stops. Out-of-process alerting is not provided by this local workflow.

## Incident lifecycle

A new difference creates a **pending** candidate. It opens only after at least two independent capture checks and the configured persistence grace, default 120 seconds. `--bot-grace 30..3600` sets the grace for newly discovered sessions; each monitor preserves the value used. Re-evaluating the same capture cannot advance counters. Unknown comparisons interrupt pending persistence and cannot resolve an open incident.

**Acknowledge** records a review action and optional note. It does not change the assessment. The collector automatically resolves an open or acknowledged incident after two independent comparable clear checks. A reviewer may resolve after the first fresh clear check; the server rejects manual resolution while the assessment is a difference, unknown or stale. A later recurrence creates a new incident and retains the previous incident's evidence and action history. Candidates that clear before opening remain in resolved history with a “cleared during grace” action.

The explorer shows monitor coverage, pending/open/acknowledged/resolved incidents, current assessment, first and latest preserved evidence, notes and action history. It exports incident or comparison evidence. Lists show at most 100 incidents. The latest comparison can be inspected even when no incident exists. Acknowledgement and resolution update only local review metadata.

## Storage and local HTTP boundary

SQLite schema 5 adds independent exchange captures, immutable comparison records, per-session monitors, incidents and review actions. A check and its incident transitions commit together. First/latest evidence survives restart, acknowledgement and resolution. Normalized exchange history remains separate from bot observations.

History GET requests still open read-only snapshots. The sole write endpoint, `/api/history/incident-action`, accepts only bounded JSON acknowledgement/resolution actions. It requires the local explorer Host and matching Origin plus a custom review header. The review writer opens an existing database and uses a short transaction; it does not run migrations, load credentials or acquire the collector's ingestion lock. This is a local-machine boundary, not hosted authentication or tenant authorization.

Before migration, `artifacts/history/before-bot-state-v5.sqlite` was created with SQLite's backup API. The installed bot upgrade and its original source/SDK/config/log backups are in `artifacts/integrations/lip-state/`. The supervisor and watchdog are restarted with the recorded original launch arguments; market selection remains the supervisor's normal behavior.

## Validation

The Python suite exercises scoped comparisons, partial-fill timing races, baseline arithmetic, exact decimal remaining quantities, missing and unknown orders/fills, incomplete sets and pagination, source health, incident grace, duplicate checks, acknowledgement, stale/manual resolution guards, restart recovery, recurrence, and cross-origin HTTP rejection. The SDK tests preserve original response/exception behavior and exclude dry runs and arbitrary bot data.

`npm.cmd run test:incidents-browser` uses an isolated synthetic database, never production history. It covers a seeded discrepancy, acknowledgement and notes, immutable evidence export, a recovery-gated resolution, filtering, account switching and mobile layout. Live validation records are saved separately and do not seed real-account incidents.
