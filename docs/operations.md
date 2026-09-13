# Operational reliability

Othryss supervises its historical collector, reference worker, loopback explorer and alert-delivery worker. The alert worker sends only to explicitly configured destinations; routes start disabled. It never manages trading processes. See [Discord and webhook setup](alert-delivery.md).

## Commands

```powershell
python -m othryss.ops start
python -m othryss.ops status
python -m othryss.ops stop
python -m othryss.ops backup
python -m othryss.ops restore --backup artifacts/backups/BUNDLE --to artifacts/restores/NEW_DIRECTORY
```

Start is idempotent. Status reports supervisor freshness, child PIDs/restarts, application heartbeats, backups and spool cleanup. Stop is asynchronous: wait for `status: stopped` before manually importing or updating services. Workers receive stop sentinels and up to 60 seconds for in-flight reads; only remaining supervisor-owned read-only children can then be terminated. The persistent `artifacts/ops/STOP` sentinel prevents automatic resurrection after an intentional stop, including at login. Start clears it.

`artifacts/ops/status.json` is authoritative. Old `artifacts/history/state-services.json` and `artifacts/reference/worker.pid` belong to the retired manual deployment. Do not terminate processes using those old PIDs. Legacy worker stop files remain; managed workers use stop files under `artifacts/ops`. Stop the supervisor before standalone collector/importer commands.

## Startup and crash recovery

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install_ops_task.ps1
python -m othryss.ops start
```

The execution setting applies only to this invocation. The installed **Othryss Local Services** task runs hidden at current-user login with a limited interactive token and no stored password. It allows battery operation and retries failed supervisors after one minute, up to 999 times. A repeating one-minute trigger also covers unexpected exits reported by Windows as successful; overlapping instances are ignored, and the persistent STOP sentinel keeps intentional stops stopped. Startup occurs after login, not before login. The installer refuses to replace a task with a different action; its `-Remove` switch removes this task after Othryss is stopped.

With the task installed, start uses Task Scheduler so it owns recovery. Without it, start launches a detached supervisor with child recovery only. `run` is the foreground entry point. Child exits are checked every two seconds and retried with exponential backoff up to 60 seconds. Five minutes of survival resets consecutive failure accounting. A Windows job object kills only owned read-only children if the supervisor disappears. OS-held locks prevent duplicate writers and release after crashes. Application freshness is distinct from process liveness; stale exchange evidence alone does not force-kill a running worker.

Child output is drained independently into `artifacts/ops/collector.log`, `reference.log` and `explorer.log`, each with a 5 MiB target and three rotated copies. Failed log writes do not block child output pipes. Inspect Task Scheduler for scheduled startup errors; direct launches also have `supervisor.log`.

For a new installation, use [repeatable onboarding](onboarding.md) to write explicit account/environment settings. Alternatively, customize `ops.local.example.json` before copying it to `ops.local.json`; the example uses a demo account and no reconciliation ticker. Built-in defaults use my-account/demo, port 8766, and no reconciliation ticker; initialize explicit account settings before starting services. Automatic backups run every six hours and retain seven completed bundles. Credentials stay in `local.env` and the separate key file. Restart after settings change.

## Telemetry rotation

SDK 0.3.0 preserves session, sequence and request identities across approximately 1 MiB segments. The first file remains `<session>.jsonl`; later segments use `<session>.<number>.jsonl`. A flush/fsync precedes the closed marker containing byte size and SHA-256. Older telemetry remains compatible.

The collector acknowledges only fully committed, hash-verified closed segments. About every ten seconds, the supervisor checks the current database checkpoint, marker, size and hash before deletion. Import, reclamation and backup share a spool maintenance lock. Active, unacknowledged, changed and unsealed legacy files are retained.

Unreclaimed data stays capped at 32 MiB per session. If ingestion falls behind, the producer drops telemetry and reports its cumulative counter and capped flag while trading continues. After reclamation, that same producer resumes capture. The nonblocking 1024-record queue, 16 KiB record limit and bounded shutdown remain. Rotation cannot make crashes, queue overflow or prolonged outages lossless. Normalized database history is separate and can grow; this bounds producer spools, not all evidence.

Installing the SDK in the live bot repo uses its controlled stop, clear resting-order audit and original launch settings. Othryss stop never performs a trading-stack shutdown.

## Backups and restore

Bundles contain individually consistent SQLite history/reference snapshots, the alert queue when present, complete spool lines, health files and allowlisted nonsecret ops settings. Credentials, keys, alert destination configuration, bot source and logs are excluded. Restored alert queues are disarmed to prevent notification replay. Hashes and COMPLETE are written last. Low disk reserve prevents new backups. Automatic failures appear in status; incomplete bundles cannot be restored and require inspection before cleanup.

Failed automatic backups retry five minutes after the attempt finishes, returning to the usual six-hour interval after success. Status preserves the last successful backup while a new attempt runs or fails. Errors include the SQLite error code when available; deadline failures identify the database and whether copying or integrity checking timed out. Arbitrary exception text is excluded. Retention errors are reported separately and do not mark an already completed backup as failed.

Spool copying precedes the history snapshot under the maintenance lock; partial trailing lines are omitted. The two databases are individually consistent, not an atomic shared snapshot. The manifest records the collection interval and that limitation.

Restore validates hashes, SQLite integrity and manifest paths before copying to a new directory. It never overwrites or activates live evidence. Telemetry checkpoints are cleared so restored complete lines can replay from zero at their new path; immutable IDs prevent duplicate imports.

```powershell
python -m othryss.server --port 8770 --db artifacts/restores/NEW_DIRECTORY/history.sqlite --reference-db artifacts/restores/NEW_DIRECTORY/references.sqlite
```

That command inspects the isolated restore without exchange access. Promoting it to live collection requires a separate stopped-service maintenance operation that preserves current evidence and deliberately configures restored paths. Backups currently stay on this machine; no off-machine copy is configured. Retention deletes only verified completed direct children of `artifacts/backups`.

## Validation

Run `python -m unittest discover -s tests -v`. Operational tests cover child crash recovery, duplicate locks, Windows job cleanup, intentional stop, cap recovery, acknowledgement-gated reclamation, backup integrity, path validation, secret exclusion and restore replay. Live acceptance records are under `artifacts/ops`; SDK deployment evidence is under `artifacts/integrations/lip-rotation`.

## Backups during active collection

At first startup, automatic backup waits for the history, reference and alert database schemas to be initialized. Status reports `waiting_for_databases` during that interval; the first backup starts as soon as they are ready instead of recording a missing-database failure and waiting six hours.

SQLite copying pins a committed read snapshot before incremental backup, preventing concurrent collector writes from repeatedly restarting the copy. WAL writers remain active. Copying and integrity checking retain a shared 120-second per-database budget. An integrity check interrupted by this deadline is reported as `BackupTimeout`, rather than a generic `OperationalError`. Only a verified bundle with a manifest and COMPLETE marker is restorable; incomplete earlier attempts are not successful backups. Regression coverage exercises concurrent writes, interrupted integrity checks, failed-attempt recovery, and retention failures.
