"""Restartable polling over the importer; collection state is separate from the UI."""

import json
from datetime import datetime, timezone

from .history import cutoff_values, run_import
from .kalshi_client import ImportRequestError
from .storage import encode, utc_now


def epoch(value):
    return int(datetime.fromisoformat(value).timestamp())


def state(store, scope):
    row = store.db.execute("SELECT * FROM sync_state WHERE scope_id=?", (scope,)).fetchone()
    return dict(row) if row else None


def update(store, scope, **values):
    permitted = {"active_run", "fill_watermark", "last_success_at", "last_full_at", "cutoff_json", "status",
                 "last_attempt_at", "heartbeat_at", "next_attempt_at", "failure_count", "last_error"}
    if not values or set(values) - permitted:
        raise ValueError("Unsupported collection state update")
    with store.db:
        store.db.execute("UPDATE sync_state SET " + ",".join(f"{key}=?" for key in values) + " WHERE scope_id=?", (*values.values(), scope))


def configure(store, scope, *, interval=60, overlap=300, full_rescan=86400):
    if not 10 <= interval <= 86400 or not 60 <= overlap <= 604800 or not 300 <= full_rescan <= 604800:
        raise ValueError("Interval must be 10–86400s, overlap 60–604800s, and full-rescan 300–604800s")
    baseline = None
    for row in store.db.execute("SELECT * FROM imports WHERE scope_id=? AND status='traversed' ORDER BY started_at DESC,run_id DESC", (scope,)):
        config = json.loads(row["config_json"])
        if not config.get("ticker") and config.get("mode") != "incremental" and config.get("environment") in {"production", "demo"}:
            baseline = row
            break
    with store.db:
        store.db.execute("""INSERT INTO sync_state(scope_id,fill_watermark,last_full_at,cutoff_json,interval_seconds,overlap_seconds,full_rescan_seconds)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET interval_seconds=excluded.interval_seconds,
            overlap_seconds=excluded.overlap_seconds,full_rescan_seconds=excluded.full_rescan_seconds""",
                         (scope, epoch(baseline["started_at"]) if baseline else None,
                          epoch(baseline["started_at"]) if baseline else None,
                          baseline["cutoff_after_json"] if baseline else None, interval, overlap, full_rescan))
    return state(store, scope)


def fail(store, scope, message):
    current = state(store, scope)
    update(store, scope, status="error", last_error=message, failure_count=current["failure_count"] + 1, heartbeat_at=utc_now())


def retry_delay(current):
    if current["status"] == "error":
        return min(900, current["interval_seconds"] * 2 ** min(current["failure_count"] - 1, 8))
    return current["interval_seconds"]


def finish(store, scope, report):
    config = report["config"]
    if report["status"] == "traversed":
        values = dict(active_run=None, fill_watermark=config["window_end"], last_success_at=report["finished_at"],
                      cutoff_json=encode(report["cutoff_after"]), status="idle", failure_count=0, last_error=None, heartbeat_at=utc_now())
        if config["mode"] == "full":
            values["last_full_at"] = config["window_end"]
        update(store, scope, **values)
    elif report["status"] == "needs_rescan":
        update(store, scope, active_run=None, cutoff_json=None, status="paused", last_error="Archive cutoff moved; a full rescan is required", heartbeat_at=utc_now())
    else:
        update(store, scope, status="paused", last_error="Page budget reached; next cycle resumes the committed cursor", heartbeat_at=utc_now())
    return report


def cycle(store, client, scope, account, *, page_size=500, max_pages=1000, now=None, progress=None):
    """Caller holds the database import lock. Only complete cycles advance coverage."""
    if not state(store, scope):
        raise ValueError("Configure the collector before starting a cycle")
    now = now or datetime.now(timezone.utc)
    end = int(now.timestamp())
    update(store, scope, status="running", last_attempt_at=now.isoformat(), heartbeat_at=utc_now(), next_attempt_at=None)
    try:
        access = client.verify_read_only()  # Recheck each cycle, including resumes.
        client.credential_subaccount = access["subaccount"]
        prior = store.db.execute("SELECT config_json FROM imports WHERE scope_id=? ORDER BY started_at DESC,run_id DESC LIMIT 1", (scope,)).fetchone()
        if prior and json.loads(prior[0]).get("credential_subaccount") != client.credential_subaccount:
            raise ValueError("Collection credential subaccount differs from stored history")
        current = state(store, scope)
        active = current["active_run"]
        if active:
            report = store.report(active)
            config = report["config"]
            if not config.get("collector") or config.get("ticker") or report["scope_id"] != scope:
                raise ValueError("Active collection run has an incompatible account scope")
            if config.get("credential_subaccount") != client.credential_subaccount or config["environment"] != client.environment:
                raise ValueError("Collection credential scope changed; restore the original key scope")
            # Recover a crash after the last import commit but before watermark commit.
            if report["status"] in {"traversed", "needs_rescan"}:
                return finish(store, scope, report)
        cutoff = cutoff_values(client)
        if active and cutoff != store.report(active)["cutoff_before"]:
            store.status(active, "needs_rescan", "Archive cutoff moved during interruption", cutoff)
            update(store, scope, active_run=None, cutoff_json=None)
            active = None
            current = state(store, scope)
        if current["fill_watermark"] is not None and end < current["fill_watermark"]:
            raise ValueError("System clock moved behind the last completed collection window")
        full = (current["fill_watermark"] is None or current["last_full_at"] is None
                or end - current["last_full_at"] >= current["full_rescan_seconds"]
                or not current["cutoff_json"] or json.loads(current["cutoff_json"]) != cutoff
                or current["fill_watermark"] - current["overlap_seconds"] < epoch(cutoff["trades_created_ts"]))
        plan = {"collector": True, "mode": "full" if full else "incremental", "window_end": end,
                "fill_min_ts": max(0, (current["fill_watermark"] or 0) - current["overlap_seconds"]),
                "window": "All current order snapshots; overlapping bounded current fills; periodic full archive repair"}

        def on_progress(value):
            update(store, scope, heartbeat_at=utc_now())
            if progress:
                progress(value)

        report = run_import(store, client, scope, account, page_size=page_size, max_pages=max_pages,
                            resume=active, collection=plan, progress=on_progress)
        return finish(store, scope, report)
    except KeyboardInterrupt:
        update(store, scope, status="stopped", heartbeat_at=utc_now(), next_attempt_at=None)
        raise
    except Exception as exc:
        # Opaque cursors may expire. Keep the watermark and restart from its overlap.
        active = state(store, scope)["active_run"]
        if isinstance(exc, ImportRequestError) and exc.http_status == 400 and active:
            cursors = store.db.execute("SELECT 1 FROM streams WHERE run_id=? AND cursor<>'' LIMIT 1", (active,)).fetchone()
            if cursors:
                update(store, scope, active_run=None, cutoff_json=None)
        # Never persist exception text from arbitrary callbacks, OS paths or payloads.
        message = str(exc) if isinstance(exc, ImportRequestError) else f"Collection failed ({type(exc).__name__}); committed pages retained. Check collector configuration or source records."
        fail(store, scope, message)
        raise


def freshness(db, scope, *, now=None):
    if db.execute("PRAGMA user_version").fetchone()[0] < 2:
        return {"status": "not_configured", "freshness": "unknown"}
    row = db.execute("SELECT * FROM sync_state WHERE scope_id=?", (scope,)).fetchone()
    if row is None:
        return {"status": "not_configured", "freshness": "unknown"}
    value = dict(row)
    now = now or datetime.now(timezone.utc)
    age = max(0, int(now.timestamp()) - value["fill_watermark"]) if value["last_success_at"] else None
    heartbeat_age = max(0, int(now.timestamp()) - epoch(value["heartbeat_at"])) if value["heartbeat_at"] else None
    return {key: value[key] for key in ("status", "last_success_at", "last_attempt_at", "next_attempt_at", "last_error", "failure_count", "interval_seconds", "active_run")} | {
        "freshness": "unknown" if age is None else "stale" if age > max(300, 3 * value["interval_seconds"]) else "recent",
        "coverage_age_seconds": age, "coverage_through": datetime.fromtimestamp(value["fill_watermark"], timezone.utc).isoformat() if value["last_success_at"] else None,
        "worker_heartbeat": "missing" if heartbeat_age is None or heartbeat_age > 120 else "recent",
    }
