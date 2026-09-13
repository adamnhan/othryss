"""Evidence-backed incident lifecycle. Acknowledgement never changes the evidence."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .replay import timestamp
from .storage import encode


def record_check(store, monitor, capture, result, now):
    scope, session = monitor["scope_id"], monitor["session_id"]
    checked = now.isoformat(timespec="microseconds")
    check_id = uuid.uuid4().hex
    findings = {(f["rule"], f["entity"]): f for f in result["findings"]}
    evaluated = set(result["evaluated_rules"])
    unknown_entities = {tuple(k) for k in result.get("unknown_entities", [])}
    with store.db:
        inserted = store.db.execute("INSERT OR IGNORE INTO bot_checks VALUES (?,?,?,?,?,?,?)", (check_id, scope, session, capture, checked, result["status"], encode(result))).rowcount
        if not inserted:
            return
        existing = {(r["rule"], r["entity"]): dict(r) for r in store.db.execute("SELECT * FROM incidents WHERE scope_id=? AND session_id=? AND status!='resolved'", (scope, session))}
        for key in findings.keys() | existing.keys():
            rule, entity = key
            incident = existing.get(key)
            if rule not in evaluated or key in unknown_entities:
                # An uncertain trading check or failed source suspends assessment.
                uncertain = (not capture.startswith("health:")) or result["status"] in {"unavailable", "stopped"}
                if incident and (rule != "source_stale" or capture.startswith("health:")) and uncertain:
                    store.db.execute("UPDATE incidents SET assessment='unknown',hits=0,clears=0,last_check=?,last_seen=?,last_capture=? WHERE incident_id=?", (check_id, checked, capture, incident["incident_id"]))
                continue
            if key in findings:
                if incident is None:
                    store.db.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, scope, session, monitor["instrument_id"], rule, entity, "pending", "difference", checked, checked, None, None, None, check_id, check_id, 1, 0, capture))
                    continue
                hits = incident["hits"] + 1
                first = incident["first_seen"]
                if incident["status"] == "pending" and (incident["assessment"] != "difference" or (now-timestamp(incident["last_seen"])).total_seconds() > 180):
                    first, hits = checked, 1
                status = incident["status"]
                opened = incident["opened_at"]
                if status == "pending" and hits >= 2 and (now-timestamp(first)).total_seconds() >= monitor["grace_seconds"]:
                    status, opened = "open", checked
                first_check = check_id if first != incident["first_seen"] else incident["first_check"]
                store.db.execute("UPDATE incidents SET status=?,assessment='difference',first_seen=?,last_seen=?,opened_at=?,first_check=?,last_check=?,hits=?,clears=0,last_capture=? WHERE incident_id=?", (status, first, checked, opened, first_check, check_id, hits, capture, incident["incident_id"]))
                if status == "open" and incident["status"] == "pending":
                    store.db.execute("INSERT INTO incident_actions VALUES (?,?,?,?,?)", (uuid.uuid4().hex, incident["incident_id"], checked, "opened", "Difference persisted across independent captures and the grace period"))
            elif incident:
                clears = incident["clears"] + 1
                resolved = incident["status"] == "pending" or clears >= 2
                store.db.execute("UPDATE incidents SET assessment='clear',last_seen=?,last_check=?,hits=0,clears=?,last_capture=?,status=?,resolved_at=? WHERE incident_id=?", (checked, check_id, clears, capture, "resolved" if resolved else incident["status"], checked if resolved else None, incident["incident_id"]))
                if resolved:
                    note = "Monitoring ended after verified producer shutdown; trading discrepancies remain separate" if rule == "source_stale" and result["status"] == "stopped" else "Cleared during grace" if incident["status"] == "pending" else "Two independent comparable captures confirmed recovery"
                    store.db.execute("INSERT INTO incident_actions VALUES (?,?,?,?,?)", (uuid.uuid4().hex, incident["incident_id"], checked, "resolved", note))


def catalog(db, scope):
    if db.execute("PRAGMA user_version").fetchone()[0] < 5:
        return {"monitors": [], "incidents": [], "counts": {}}
    monitors = []
    for row in db.execute("SELECT * FROM bot_monitors WHERE scope_id=? ORDER BY instrument_id,session_id", (scope,)):
        item = dict(row)
        health = db.execute("SELECT health_json FROM telemetry_health WHERE scope_id=? AND session_id=?", (scope, row["session_id"])).fetchone()
        item["stopped"] = bool(health and json.loads(health[0])["stopped"])
        check = db.execute("SELECT check_id,checked_at,status,result_json FROM bot_checks WHERE scope_id=? AND session_id=? AND snapshot_id NOT LIKE 'health:%' ORDER BY checked_at DESC LIMIT 1", (scope, row["session_id"])).fetchone()
        item["latest_check"] = {"check_id": check[0], "checked_at": check[1], "status": check[2], "reason": json.loads(check[3])["reason"], "position_coverage": json.loads(check[3]).get("position_coverage")} if check else None
        from .bot_state import source_status
        item["source_status"], item["source_reason"], _ = source_status(db, scope, row["session_id"], datetime.now(timezone.utc))
        item["stopped"] = item["source_status"] == "stopped"
        item["check_stale"] = not check or (datetime.now(timezone.utc)-timestamp(check[1])).total_seconds() > 180
        monitors.append(item)
    incidents = [dict(r) for r in db.execute("SELECT * FROM incidents WHERE scope_id=? ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'acknowledged' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END,last_seen DESC LIMIT 100", (scope,))]
    counts = dict(db.execute("SELECT status,COUNT(*) FROM incidents WHERE scope_id=? GROUP BY status", (scope,)))
    return {"monitors": monitors[-100:], "incidents": incidents, "counts": counts, "limit": 100,
            "coverage": "Sustained differences are review candidates. Acknowledgement records review; resolution requires comparable recovery evidence. Stale or incomplete data cannot resolve an incident."}


def check_detail(db, scope, check_id):
    row = db.execute("SELECT * FROM bot_checks WHERE scope_id=? AND check_id=?", (scope, check_id)).fetchone()
    if not row:
        raise ValueError("Unknown bot check for this account")
    result = dict(row); result["result"] = json.loads(result.pop("result_json"))
    snapshot = db.execute("SELECT * FROM bot_exchange WHERE scope_id=? AND snapshot_id=?", (scope, row["snapshot_id"])).fetchone()
    if snapshot:
        result["exchange"] = dict(snapshot)
        result["exchange"]["evidence"] = json.loads(result["exchange"].pop("evidence_json"))
    return result


def detail(db, scope, incident_id):
    row = db.execute("SELECT * FROM incidents WHERE scope_id=? AND incident_id=?", (scope, incident_id)).fetchone()
    if row is None:
        raise ValueError("Unknown incident for this account")
    incident = dict(row)
    return {"incident": incident, "first_evidence": check_detail(db, scope, row["first_check"]),
            "latest_evidence": check_detail(db, scope, row["last_check"]),
            "actions": [dict(r) for r in db.execute("SELECT occurred_at,action,note FROM incident_actions WHERE incident_id=? ORDER BY occurred_at,action_id", (incident_id,))]}


def act(path, scope, incident_id, action, note=""):
    """Narrow local review write: no migrations, exchange client, or collector lock."""
    if action not in {"acknowledge", "resolve"} or not isinstance(note, str) or len(note) > 1000:
        raise ValueError("Unsupported incident action or note")
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM incidents WHERE scope_id=? AND incident_id=?", (scope, incident_id)).fetchone()
        if row is None:
            raise ValueError("Unknown incident for this account")
        if action == "acknowledge" and row["status"] not in {"open", "acknowledged"}:
            raise ValueError("Only open incidents can be acknowledged")
        if action == "resolve" and (row["status"] not in {"open", "acknowledged"} or row["assessment"] != "clear"):
            raise ValueError("Resolution requires a comparable clear check; an acknowledgement cannot clear a difference")
        if action == "resolve" and not -5 <= (datetime.now(timezone.utc)-timestamp(row["last_seen"])).total_seconds() <= 180:
            raise ValueError("The clear check is stale; wait for fresh recovery evidence")
        when = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        if action == "acknowledge" and row["status"] == "acknowledged":
            db.rollback()
            return {"status": "acknowledged"}
        status = "acknowledged" if action == "acknowledge" else "resolved"
        column = "acknowledged_at" if action == "acknowledge" else "resolved_at"
        db.execute(f"UPDATE incidents SET status=?,{column}=? WHERE incident_id=?", (status, when, incident_id))
        db.execute("INSERT INTO incident_actions VALUES (?,?,?,?,?)", (uuid.uuid4().hex, incident_id, when, action, note.strip()))
        db.commit()
        return {"status": status}
    finally:
        db.close()
