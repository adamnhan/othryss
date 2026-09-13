"""Durable incident notification queue shared by every delivery channel."""
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .alert_channels import send, validate_destination
from .storage import digest, encode

RULES = {"source_stale", "position_mismatch", "local_order_missing", "unknown_exchange_order", "remaining_mismatch", "missing_local_fill", "unmatched_local_fill", "inventory_limit"}
SECRET_FIELDS = {"discord": {"url_env"}, "webhook": {"url_env", "signing_secret_env"}}
DEFAULTS = {"enabled": False, "events": ["opened", "resolved"], "rules": [], "reminder_seconds": 0,
            "max_attempts": 5, "ttl_seconds": 3600, "min_interval_seconds": 10}


def configuration(path):
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {"version": 1, "routes": []}
    if not isinstance(raw,dict) or set(raw) != {"version", "routes"} or raw["version"] != 1 or not isinstance(raw["routes"],list) or len(raw["routes"]) > 8:
        raise ValueError("Expected alert config version 1 with at most 8 routes")
    routes, seen = [], set()
    for item in raw["routes"]:
        if not isinstance(item,dict) or set(item)-({"id", "kind", "scope_id", "destination"}|DEFAULTS.keys()):
            raise ValueError("Unknown alert route setting")
        r = DEFAULTS | item
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", str(r.get("id",""))) or r["id"] in seen or r.get("kind") not in SECRET_FIELDS:
            raise ValueError("Invalid or duplicate alert route")
        if not re.fullmatch(r"[a-f0-9]{64}", str(r.get("scope_id",""))) or type(r["enabled"]) is not bool:
            raise ValueError("Expected explicit account scope and enabled flag")
        for key, allowed in (("events", {"opened", "resolved"}), ("rules", RULES)):
            if not isinstance(r[key],list) or any(not isinstance(v,str) or v not in allowed for v in r[key]): raise ValueError("Invalid alert filter")
        for key, low, high in (("reminder_seconds",0,86400),("max_attempts",1,10),("ttl_seconds",60,86400),("min_interval_seconds",1,3600)):
            if type(r[key]) is not int or not low <= r[key] <= high: raise ValueError("Invalid alert bound")
        if 0 < r["reminder_seconds"] < 300: raise ValueError("Reminder interval must be zero or at least 300 seconds")
        dest = r.get("destination")
        if not isinstance(dest,dict) or set(dest) != SECRET_FIELDS[r["kind"]] or any(not isinstance(v,str) or not re.fullmatch(r"OTHRYSS_ALERT_[A-Z0-9_]+",v) for v in dest.values()):
            raise ValueError("Destinations must reference OTHRYSS_ALERT_* environment variables")
        routes.append(r); seen.add(r["id"])
    return routes


def secrets_for(route, env_file):
    wanted = set(route["destination"].values())
    values = {}
    path = Path(env_file)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, sep, value = line.strip().partition("="); name = name.strip()
            if name not in wanted: continue
            if not sep or name in values: raise ValueError("Invalid or duplicate alert environment setting")
            value = value.strip()
            if value[:1] in {"'", '"'}:
                if len(value)<2 or value[-1]!=value[0]: raise ValueError("Invalid alert environment quoting")
                value = value[1:-1]
            values[name] = value
    result = {field: os.environ.get(name) or values.get(name, "") for field,name in route["destination"].items()}
    if not all(result.values()): raise ValueError("Missing alert destination environment values")
    validate_destination(route["kind"], result)
    return result


def connect(path):
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path, timeout=10); db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL"); db.execute("PRAGMA synchronous=FULL")
    if db.execute("PRAGMA user_version").fetchone()[0] not in (0,1):
        db.close(); raise ValueError("Unsupported alerts schema")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS routes (
            route_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL, kind TEXT NOT NULL,
            fingerprint TEXT NOT NULL, cursor INTEGER NOT NULL, active INTEGER NOT NULL,
            error TEXT, last_sent REAL, activated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries (
            delivery_id TEXT PRIMARY KEY, route_id TEXT NOT NULL, event_key TEXT NOT NULL,
            scope_id TEXT NOT NULL, incident_id TEXT NOT NULL, event TEXT NOT NULL,
            payload_json TEXT NOT NULL, created_at REAL NOT NULL, due_at REAL NOT NULL,
            expires_at REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT, http_status INTEGER, provider_id TEXT, UNIQUE(route_id,event_key));
        CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(route_id,status,due_at);
        CREATE INDEX IF NOT EXISTS deliveries_incident ON deliveries(route_id,incident_id,event);
        CREATE TABLE IF NOT EXISTS attempts (
            delivery_id TEXT NOT NULL, attempt INTEGER NOT NULL, started_at REAL NOT NULL,
            finished_at REAL, status TEXT NOT NULL, error TEXT, http_status INTEGER,
            PRIMARY KEY(delivery_id,attempt));
        CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY CHECK(id=1), status TEXT, heartbeat_at REAL);
        PRAGMA user_version=1;
    """)
    return db


def envelope(incident, event, when):
    # Review notes, raw evidence and credentials never leave the machine.
    incident=dict(incident)
    if isinstance(when,(int,float)): when=datetime.fromtimestamp(when,timezone.utc).isoformat()
    return {"schema_version": "othryss-alert-1", "event": event, "occurred_at": when,
            "scope_id": incident["scope_id"], "incident": {k: incident[k] for k in
            ("incident_id", "instrument_id", "rule", "entity", "assessment")} | {
                "checked_at": incident["last_seen"],
                "summary": "This is a delivery test, not a trading incident." if event == "test" else
                           incident.get("source_summary") or "Comparable checks confirmed recovery." if event == "resolved" else
                           incident.get("source_summary") or
                           incident.get("inventory_summary") or "Sustained difference needs review; inspect the recorded evidence."}}


def inventory_context(history, incident):
    if incident["rule"]=="source_stale":return source_context(history, incident)
    if incident["rule"]!="inventory_limit":return incident
    value=dict(incident)
    check=history.execute("SELECT result_json FROM bot_checks WHERE check_id=? AND scope_id=?",(incident["last_check"],incident["scope_id"])).fetchone()
    if check:
        for finding in json.loads(check[0]).get("findings",[]):
            if finding["entity"]==incident["entity"] and finding["rule"]=="inventory_limit":
                d=finding["details"]
                value["inventory_summary"]=f"{incident['entity']}: {d['observed_absolute_contracts']} absolute contracts exceeds limit {d['limit_contracts']} (primary subaccount)."
    return value


def source_context(history, incident):
    """Only approved diagnostic fields leave the machine; never raw errors/notes."""
    value = dict(incident)
    row = history.execute("SELECT result_json FROM bot_checks WHERE scope_id=? AND check_id=?", (incident["scope_id"], incident["last_check"])).fetchone()
    if not row:
        return value
    result = json.loads(row[0]); health = result.get("health") or {}
    reason = result.get("reason", "")
    labels = {
        "Producer heartbeat is missing or stale": "Bot heartbeat is missing or stale.",
        "Producer reports lost telemetry or a write failure": "Bot reported telemetry loss or a writer error.",
        "Producer telemetry errors; waiting for 120 seconds without new errors and advancing evidence": "Telemetry errors reported; waiting for a quiet recovery period and advancing records.",
        "Imported producer sequence has gaps": "Captured bot records have sequence gaps; coverage remains incomplete.",
        "Producer records have not all reached the collector": "The collector has not received all records reported by the bot.",
        "Bot-state observations are missing or stale": "Bot-state observations are missing or stale.",
        "Waiting for a new bot-state observation after telemetry errors": "Waiting for a new bot-state observation after telemetry errors.",
        "Spool verification failed for this account": "Local telemetry verification failed; evidence needs review.",
        "Exchange state capture is missing or stale": "Exchange state capture is missing or stale.",
        "Exchange state capture incomplete or producer scope ambiguous; comparisons suspended": "Exchange comparison is incomplete or producer identity is ambiguous.",
        "Fresh telemetry recovered after a quiet period; historical error counters retained": "Fresh telemetry and exchange checks recovered. Historical error counters are retained.",
        "Recent producer and bot state": "Fresh telemetry and exchange checks recovered.",
        "Fresh heartbeat and contiguous bot records; historical heartbeat-file errors retained": "Fresh telemetry and exchange checks recovered. Historical heartbeat-file errors are retained.",
        "Expected probe retirement: supervisor evicted this market": "Expected shutdown: supervisor retired this market and the probe process exited. Monitoring ended; this does not certify order cleanup.",
        "Producer stopped": "Producer reported an orderly shutdown. Monitoring ended; this does not certify order cleanup."
    }
    text = labels.get(reason, "Source monitoring is unavailable; inspect local evidence for the reason.")
    from .replay import timestamp
    try:
        text += " Last heartbeat: " + timestamp(health["heartbeat_at"]).isoformat() + "."
    except (KeyError, TypeError, ValueError):
        pass
    counters = [(label, health.get(key)) for key, label in (("dropped", "reported dropped"), ("write_failures", "writer errors"), ("heartbeat_failures", "heartbeat errors"))]
    valid = [f"{label}: {n}" for label, n in counters if type(n) is int and 0 <= n <= 10**12]
    if valid:
        text += " Session totals — " + ", ".join(valid) + "."
    value["source_summary"] = text
    return value


def enqueue(db, route, key, incident, event, now, when=None):
    payload = envelope(incident, event, when or now)
    db.execute("""INSERT OR IGNORE INTO deliveries
        (delivery_id,route_id,event_key,scope_id,incident_id,event,payload_json,created_at,due_at,expires_at,status)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (uuid.uuid4().hex,route["id"],key,route["scope_id"],incident["incident_id"],event,
        encode(payload),now,now,now+route["ttl_seconds"],"pending"))


def sync_routes(db, history, routes, resolved, now):
    """Activation starts at the current action cursor, without historical replay."""
    maximum = history.execute("SELECT COALESCE(MAX(rowid),0) FROM incident_actions").fetchone()[0]
    ids = set()
    with db:
        for r in routes:
            ids.add(r["id"])
            secrets = resolved.get(r["id"])
            active = bool(r["enabled"] and secrets)
            fingerprint = digest([r, secrets])
            old = db.execute("SELECT * FROM routes WHERE route_id=?",(r["id"],)).fetchone()
            reset = old is None or old["fingerprint"] != fingerprint or old["cursor"] > maximum
            if reset:
                db.execute("UPDATE deliveries SET status='canceled',error='route_changed' WHERE route_id=? AND status IN ('pending','retry')",(r["id"],))
                db.execute("INSERT OR REPLACE INTO routes VALUES (?,?,?,?,?,?,?,?,?)",(r["id"],r["scope_id"],r["kind"],fingerprint,maximum,int(active),
                            None if active else "disabled" if not r["enabled"] else "configuration_missing_or_invalid",None,now))
        for old in db.execute("SELECT route_id FROM routes").fetchall():
            if old[0] not in ids:
                db.execute("UPDATE routes SET active=0,fingerprint='inactive',error='removed' WHERE route_id=?",(old[0],))
                db.execute("UPDATE deliveries SET status='canceled',error='route_removed' WHERE route_id=? AND status IN ('pending','retry')",(old[0],))


def discover(db, history, route, now):
    state = db.execute("SELECT * FROM routes WHERE route_id=?",(route["id"],)).fetchone()
    if not state or not state["active"]: return
    if db.execute("SELECT COUNT(*) FROM deliveries WHERE route_id=? AND status IN ('pending','retry')",(route["id"],)).fetchone()[0] >= 4500:
        return  # Preserve cursor while the bounded pending queue drains/expires.
    # Scan a bounded global action batch so unrelated accounts cannot stall cursors.
    actions = history.execute("SELECT rowid AS sequence,* FROM incident_actions WHERE rowid>? ORDER BY rowid LIMIT 500",(state["cursor"],)).fetchall()
    with db:
        for action in actions:
            incident = history.execute("SELECT * FROM incidents WHERE incident_id=? AND scope_id=?",(action["incident_id"],route["scope_id"])).fetchone()
            if not incident or not incident["opened_at"] or action["action"] not in route["events"]: continue
            if route["rules"] and incident["rule"] not in route["rules"]: continue
            # Skip outdated historical transitions after a long worker outage.
            from .replay import timestamp
            if now-timestamp(action["occurred_at"]).timestamp() > route["ttl_seconds"]: continue
            enqueue(db,route,action["action_id"],inventory_context(history,incident),action["action"],now,action["occurred_at"])
        if actions: db.execute("UPDATE routes SET cursor=? WHERE route_id=?",(actions[-1]["sequence"],route["id"]))
        if route["reminder_seconds"]:
            for row in db.execute("""SELECT incident_id,MAX(created_at) last_notice FROM deliveries WHERE route_id=?
                    AND event IN ('opened','reminder') AND created_at>=? GROUP BY incident_id
                    HAVING MAX(created_at)<=? AND SUM(CASE WHEN event='opened' AND status IN ('accepted','unknown') THEN 1 ELSE 0 END)>0
                    AND SUM(CASE WHEN status IN ('pending','retry','sending') THEN 1 ELSE 0 END)=0
                    ORDER BY last_notice LIMIT 100""",(route["id"],state["activated_at"],now-route["reminder_seconds"])).fetchall():
                incident = history.execute("SELECT * FROM incidents WHERE incident_id=? AND scope_id=? AND status='open' AND assessment='difference'",(row["incident_id"],route["scope_id"])).fetchone()
                if not incident: continue
                from .replay import timestamp
                if not -5 <= now-timestamp(incident["last_seen"]).timestamp() <= 180: continue
                key = f"reminder:{incident['incident_id']}:{int(row['last_notice'])}"
                enqueue(db,route,key,inventory_context(history,incident),"reminder",now)


def recover_inflight(db):
    # Persisted 'sending' means the process could have died after remote acceptance.
    with db:
        db.execute("UPDATE attempts SET status='unknown',error='worker_interrupted' WHERE status='sending'")
        db.execute("UPDATE deliveries SET status='unknown',error='worker_interrupted' WHERE status='sending'")


def prune(db, history, now):
    """Retain active-incident notification context; prune old closed audit rows."""
    candidates = db.execute("SELECT delivery_id,incident_id,scope_id FROM deliveries WHERE created_at<? AND status NOT IN ('pending','retry','sending') ORDER BY created_at LIMIT 500",(now-30*86400,)).fetchall()
    with db:
        for row in candidates:
            if history.execute("SELECT 1 FROM incidents WHERE incident_id=? AND scope_id=? AND status!='resolved'",(row["incident_id"],row["scope_id"])).fetchone(): continue
            db.execute("DELETE FROM attempts WHERE delivery_id=?",(row["delivery_id"],))
            db.execute("DELETE FROM deliveries WHERE delivery_id=?",(row["delivery_id"],))


def deliver_one(db, history, route, secrets, now, sender=send):
    state = db.execute("SELECT * FROM routes WHERE route_id=?",(route["id"],)).fetchone()
    if not state or not state["active"]: return
    with db:
        db.execute("UPDATE deliveries SET status='expired',error='delivery_expired' WHERE route_id=? AND status IN ('pending','retry') AND expires_at<=?",(route["id"],now))
    if state["last_sent"] is not None and now < state["last_sent"]+route["min_interval_seconds"]: return
    row = db.execute("SELECT * FROM deliveries WHERE route_id=? AND status IN ('pending','retry') AND due_at<=? ORDER BY created_at,delivery_id LIMIT 1",(route["id"],now)).fetchone()
    if row is None: return
    if row["event"] != "test":
        incident = history.execute("SELECT * FROM incidents WHERE incident_id=? AND scope_id=?",(row["incident_id"],route["scope_id"])).fetchone()
        relevant = incident and (incident["status"] == "resolved" if row["event"] == "resolved" else incident["status"] == "open")
        if incident and incident["rule"] == "inventory_limit" and row["event"] != "resolved":
            relevant = relevant and incident["assessment"] == "difference"
        if row["event"] == "resolved":
            relevant = relevant and db.execute("SELECT 1 FROM deliveries WHERE route_id=? AND incident_id=? AND event='opened' AND status IN ('accepted','unknown') AND created_at>=?",(route["id"],row["incident_id"],state["activated_at"])).fetchone()
        if not relevant:
            with db: db.execute("UPDATE deliveries SET status='suppressed',error='incident_changed_or_opening_not_sent' WHERE delivery_id=?",(row["delivery_id"],))
            return
    attempt = row["attempts"]+1
    with db:
        db.execute("UPDATE deliveries SET status='sending',attempts=? WHERE delivery_id=?",(attempt,row["delivery_id"]))
        db.execute("INSERT INTO attempts VALUES (?,?,?,NULL,'sending',NULL,NULL)",(row["delivery_id"],attempt,now))
        db.execute("UPDATE routes SET last_sent=? WHERE route_id=?",(now,route["id"]))
    # No database transaction or history snapshot is held across network I/O by caller.
    try:
        result = sender(route,secrets,json.loads(row["payload_json"]),row["delivery_id"])
    except Exception:
        result = {"status":"unknown", "error":"adapter_outcome_unknown"}
    status = result["status"]
    delay = result.get("retry_after") or min(900,5*2**(attempt-1))
    if status == "retry" and attempt >= route["max_attempts"]: status = "failed"
    if status == "retry" and now+delay >= row["expires_at"]: status = "expired"
    with db:
        db.execute("UPDATE deliveries SET status=?,due_at=?,error=?,http_status=?,provider_id=? WHERE delivery_id=?",
            (status,now+delay,result.get("error"),result.get("http_status"),result.get("provider_id"),row["delivery_id"]))
        db.execute("UPDATE attempts SET finished_at=?,status=?,error=?,http_status=? WHERE delivery_id=? AND attempt=?",
            (time.time(),status,result.get("error"),result.get("http_status"),row["delivery_id"],attempt))
        if result.get("retry_after"):
            db.execute("UPDATE routes SET last_sent=? WHERE route_id=?",(now+delay-route["min_interval_seconds"],route["id"]))


def read(path, scope):
    if not Path(path).exists(): return {"routes": [], "deliveries": [], "worker": None}
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True,timeout=5); db.row_factory=sqlite3.Row
    try:
        db.execute("BEGIN")
        routes = [dict(r) for r in db.execute("SELECT route_id,kind,active,error,activated_at FROM routes WHERE scope_id=?",(scope,))]
        rows = [dict(r) for r in db.execute("""SELECT delivery_id,route_id,incident_id,event,created_at,status,attempts,error,http_status,provider_id
            FROM deliveries WHERE scope_id=? ORDER BY created_at DESC,delivery_id LIMIT 50""",(scope,))]
        for row in rows:
            row["history"] = [dict(r) for r in db.execute("SELECT attempt,started_at,finished_at,status,error,http_status FROM attempts WHERE delivery_id=? ORDER BY attempt",(row["delivery_id"],))]
        worker = db.execute("SELECT status,heartbeat_at FROM worker WHERE id=1").fetchone()
        return {"routes": routes, "deliveries": rows, "worker": dict(worker) if worker else None}
    finally: db.close()
