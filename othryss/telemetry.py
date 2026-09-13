"""Import allowlisted bot telemetry; link it to exchange evidence without inventing fills."""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .replay import timestamp
from .storage import encode, utc_now, import_lock

KINDS = {"PRODUCER_START", "PRODUCER_STOP", "ORDER_INTENT", "ORDER_REQUEST", "HTTP_ATTEMPT", "HTTP_RESPONSE", "ORDER_RESPONSE", "BOT_STATE", "BOT_STATE_UNAVAILABLE"}
ENVELOPE = {"schema_version", "producer", "session_id", "account", "environment", "workspace", "instrument_id", "strategy_id", "run_id", "subaccount", "event_id", "sequence", "occurred_at", "monotonic_ns", "type", "payload"}
PAYLOAD = set("request_id operation attempt attempts order_id previous_order_id ticker client_order_id side count price post_only time_in_force expiration_time intent_scope http_status remaining_count_fp fill_count_fp status body_unavailable outcome error_type duration_ns".split())
HEALTH = set("schema_version producer session_id account environment workspace instrument_id strategy_id run_id subaccount heartbeat_at last_write_at last_sequence dropped write_failures queue_depth capped stopped".split())
HEALTH |= {"heartbeat_failures", "last_error_stage", "last_error_type", "last_error_at"}


def binding(record, account):
    for key in ("account", "environment", "workspace"):
        if record.get(key) != account[key]:
            raise ValueError("Telemetry account/environment/workspace does not match collector binding")
    if record.get("schema_version") not in {"0.1.0", "0.2.0", "0.3.0", "0.3.1"} or not re.fullmatch("[a-f0-9]{32}", str(record.get("session_id", ""))):
        raise ValueError("Unsupported telemetry version or session identity")
    if type(record.get("subaccount")) is not int or not 0 <= record["subaccount"] <= 63:
        raise ValueError("Invalid telemetry subaccount")
    for key in ("instrument_id", "strategy_id", "producer", "run_id"):
        if not isinstance(record.get(key), str) or not 1 <= len(record[key]) <= 256:
            raise ValueError("Telemetry source identity missing or oversized")


def validate(record, account):
    binding(record, account)
    is_state = record.get("type") == "BOT_STATE"
    if set(record) - ENVELOPE or not isinstance(record.get("payload"), dict) or (not is_state and set(record["payload"]) - PAYLOAD):
        raise ValueError("Telemetry contains fields outside the event contract")
    if record.get("type") not in KINDS or type(record.get("sequence")) is not int or record["sequence"] < 1:
        raise ValueError("Invalid telemetry event kind or sequence")
    if record.get("event_id") != f"{record['session_id']}:{record['sequence']}":
        raise ValueError("Telemetry event identity does not match producer sequence")
    timestamp(record["occurred_at"])
    if not str(record.get("monotonic_ns", "")).isdigit():
        raise ValueError("Missing monotonic timestamp")
    if is_state:
        from .bot_state import validate_state
        validate_state(record["payload"])
        return record
    for value in record["payload"].values():
        if value is not None and type(value) not in (str, int, bool):
            raise ValueError("Unsupported telemetry payload value")
        if isinstance(value, str) and len(value) > 256:
            raise ValueError("Telemetry payload value is too long")
    if record["type"].startswith(("ORDER_", "HTTP_")):
        p = record["payload"]
        if not re.fullmatch("[a-f0-9]{32}", str(p.get("request_id", ""))) or p.get("operation") not in {"submit", "amend", "cancel"}:
            raise ValueError("Order telemetry lacks request correlation")
    return record


def import_directory(store, scope, directory, *, max_events=1000):
    if not Path(directory).is_dir():
        return {"status":"waiting_for_producer","inserted":0,"errors":0}
    with import_lock(Path(directory)/"maintenance"):
        return _import_directory(store,scope,directory,max_events=max_events)


def _import_directory(store, scope, directory, *, max_events=1000):
    root = Path(directory).resolve()
    account = dict(store.db.execute("SELECT * FROM accounts WHERE scope_id=?", (scope,)).fetchone())
    if not root.is_dir():
        return {"status": "waiting_for_producer", "inserted": 0, "errors": 0}
    inserted = errors = processed = 0
    for path in sorted(root.glob("*.jsonl"), key=lambda p:(p.name[:32],len(p.name),p.name)):
        if processed >= max_events:
            break
        if not re.fullmatch(r"[a-f0-9]{32}(?:\.\d{6,})?", path.stem) or path.resolve().parent != root:
            continue
        name = str(path.resolve())
        with store.db:
            store.db.execute("INSERT OR IGNORE INTO telemetry_files(scope_id,path) VALUES (?,?)", (scope, name))
        checkpoint = store.db.execute("SELECT * FROM telemetry_files WHERE scope_id=? AND path=?", (scope, name)).fetchone()
        try:
            with path.open("rb") as handle:
                if path.stat().st_size < checkpoint["byte_offset"]:
                    raise ValueError("Telemetry file was truncated; checkpoint retained")
                if checkpoint["prefix_hash"]:
                    if hashlib.sha256(handle.read(checkpoint["prefix_length"])).hexdigest() != checkpoint["prefix_hash"]:
                        raise ValueError("Telemetry file changed beneath its checkpoint")
                handle.seek(checkpoint["byte_offset"])
                while processed < max_events:
                    line = handle.readline(16385)
                    if not line:
                        break
                    if len(line) > 16384:
                        raise ValueError("Telemetry record exceeds byte limit")
                    if not line.endswith(b"\n"):
                        break  # Live writer's partial last line is not a complete record.
                    record = validate(json.loads(line), account)
                    if record["session_id"] != path.name[:32]:
                        raise ValueError("Session identity does not match spool filename")
                    canonical = encode(record)
                    new_offset = handle.tell()
                    with store.db:
                        prior = store.db.execute("SELECT canonical_json FROM telemetry_records WHERE scope_id=? AND event_id=?", (scope, record["event_id"])).fetchone()
                        if prior and prior[0] != canonical:
                            raise ValueError("Conflicting immutable telemetry event")
                        if not prior:
                            store.db.execute("INSERT INTO telemetry_records VALUES (?,?,?,?,?,?,?,?,?,?,?)", (scope, record["event_id"], record["session_id"], record["sequence"], record["payload"].get("request_id"), record["instrument_id"], record["subaccount"], record["occurred_at"], utc_now(), record["type"], canonical))
                            inserted += 1
                        if not checkpoint["prefix_hash"]:
                            here = handle.tell(); handle.seek(0); prefix = handle.read(min(new_offset, 1024)); handle.seek(here)
                            store.db.execute("UPDATE telemetry_files SET prefix_hash=?,prefix_length=? WHERE scope_id=? AND path=?", (hashlib.sha256(prefix).hexdigest(), len(prefix), scope, name))
                        store.db.execute("UPDATE telemetry_files SET byte_offset=?,last_error=NULL WHERE scope_id=? AND path=?", (new_offset, scope, name))
                    processed += 1
            # Closed segments are immutable. Acknowledge only a fully committed import.
            closed = path.with_name(path.name + ".closed")
            if closed.is_file() and closed.stat().st_size <= 1024:
                marker = json.loads(closed.read_text(encoding="utf-8"))
                checkpoint = store.db.execute("SELECT * FROM telemetry_files WHERE scope_id=? AND path=?", (scope,name)).fetchone()
                if path.stat().st_size == checkpoint["byte_offset"] == marker.get("size"):
                    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                    if checksum != marker.get("sha256"): raise ValueError("Closed segment hash changed")
                    ack = path.with_name(path.name + ".acked")
                    temporary = ack.with_name(ack.name + ".tmp")
                    temporary.write_text(encode(marker),encoding="utf-8")
                    temporary.replace(ack)
        except Exception:
            errors += 1
            with store.db:
                store.db.execute("UPDATE telemetry_files SET last_error=? WHERE scope_id=? AND path=?", ("Telemetry file could not be verified; checkpoint retained", scope, name))
    for path in sorted(root.glob("*.health.json")):
        name = str(path.resolve())
        try:
            if path.resolve().parent != root or path.stat().st_size > 8192:
                continue
            health = json.loads(path.read_text(encoding="utf-8"))
            binding(health, account)
            if set(health) - HEALTH or path.name != health["session_id"] + ".health.json":
                raise ValueError("Unknown health fields or identity")
            timestamp(health["heartbeat_at"])
            if health.get("last_write_at") is not None:
                timestamp(health["last_write_at"])
            if any(type(health.get(key)) is not bool for key in ("capped", "stopped")):
                raise ValueError("Invalid health flags")
            for key in ("last_sequence", "dropped", "write_failures", "queue_depth"):
                if type(health.get(key)) is not int or health[key] < 0:
                    raise ValueError("Invalid health counter")
            if "heartbeat_failures" in health and (type(health["heartbeat_failures"]) is not int or health["heartbeat_failures"] < 0):
                raise ValueError("Invalid heartbeat error counter")
            if health.get("last_error_stage") not in {None, "heartbeat", "open", "serialize", "rotate", "spool_budget", "event_write", "flush"}:
                raise ValueError("Invalid telemetry failure stage")
            if health.get("last_error_type") is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", str(health["last_error_type"])):
                raise ValueError("Invalid telemetry failure type")
            if health.get("last_error_at") is not None:
                timestamp(health["last_error_at"])
            with store.db:
                store.db.execute("INSERT INTO telemetry_health VALUES (?,?,?,?) ON CONFLICT(scope_id,session_id) DO UPDATE SET received_at=excluded.received_at,health_json=excluded.health_json", (scope, health["session_id"], utc_now(), encode(health)))
                store.db.execute("DELETE FROM telemetry_files WHERE scope_id=? AND path=?", (scope, name))
        except Exception:
            errors += 1
            with store.db:
                store.db.execute("INSERT INTO telemetry_files(scope_id,path,last_error) VALUES (?,?,?) ON CONFLICT(scope_id,path) DO UPDATE SET last_error=excluded.last_error", (scope, name, "Telemetry heartbeat could not be verified"))
    return {"status": "errors" if errors else "imported", "inserted": inserted, "errors": errors}


def catalog(db, scope):
    if db.execute("PRAGMA user_version").fetchone()[0] < 4:
        return {"sessions": [], "requests": [], "file_errors": 0}
    sessions = []
    for row in db.execute("SELECT session_id,MAX(sequence) max_sequence,COUNT(*) records,MAX(occurred_at) last_event_at FROM telemetry_records WHERE scope_id=? GROUP BY session_id ORDER BY last_event_at DESC LIMIT 20", (scope,)):
        item = dict(row)
        item["sequence_gaps"] = row["max_sequence"] - row["records"]
        sessions.append(item)
    health_rows = db.execute("SELECT session_id,health_json FROM telemetry_health WHERE scope_id=? ORDER BY received_at DESC LIMIT 20", (scope,))
    for row in health_rows:
        health = json.loads(row["health_json"])
        item = next((s for s in sessions if s["session_id"] == row["session_id"]), None)
        if item is None:
            item = {"session_id": row["session_id"], "records": 0, "sequence_gaps": None}; sessions.append(item)
        item["health"] = health
        age = (datetime.now(timezone.utc) - timestamp(health["heartbeat_at"])).total_seconds()
        item["stale"] = age > 120 or age < -5
        from .bot_state import source_status
        item["source_status"], item["source_reason"], _ = source_status(db, scope, row["session_id"], datetime.now(timezone.utc))
    requests = [dict(r) for r in db.execute("""SELECT session_id,request_id,instrument_id,MIN(occurred_at) started_at,COUNT(*) records
        FROM telemetry_records WHERE scope_id=? AND request_id IS NOT NULL GROUP BY session_id,request_id,instrument_id ORDER BY started_at DESC LIMIT 25""", (scope,))]
    return {"sessions": sessions, "requests": requests, "file_errors": db.execute("SELECT COUNT(*) FROM telemetry_files WHERE scope_id=? AND last_error IS NOT NULL", (scope,)).fetchone()[0]}


def request_detail(db, scope, session, request):
    if db.execute("PRAGMA user_version").fetchone()[0] < 4:
        raise ValueError("No telemetry captured")
    rows = db.execute("SELECT canonical_json FROM telemetry_records WHERE scope_id=? AND session_id=? AND request_id=? ORDER BY sequence LIMIT 200", (scope, session, request))
    records = [json.loads(r[0]) for r in rows]
    if not records:
        raise ValueError("Unknown telemetry request for this account")
    ticker, sub = records[0]["instrument_id"], records[0]["subaccount"]
    if any(any(r[key] != records[0][key] for key in ("instrument_id", "subaccount", "strategy_id", "run_id")) for r in records):
        raise ValueError("Conflicting request identity")
    order_ids = {r["payload"][key] for r in records for key in ("order_id", "previous_order_id") if r["payload"].get(key)}
    client_ids = {r["payload"]["client_order_id"] for r in records if r["payload"].get("client_order_id")}
    # Timed-out submissions can be linked later by an exact exchange client ID.
    for row in db.execute("SELECT canonical_json FROM events WHERE scope_id=? AND instrument_id=? AND type='ORDER_OBSERVATION'", (scope, ticker)):
        event = json.loads(row[0]); payload = event["payload"]
        if payload.get("subaccount") == sub and payload.get("client_order_id") in client_ids:
            order_ids.add(payload["order_id"])
    evidence = []
    for order_id in sorted(order_ids):
        for row in db.execute("SELECT canonical_json FROM events WHERE scope_id=? AND instrument_id=? AND order_id=? ORDER BY COALESCE(occurred_at,received_at),event_id", (scope, ticker, order_id)):
            event = json.loads(row[0])
            if event["payload"].get("subaccount") == sub:
                evidence.append(event)
    return {"records": records, "order_ids": sorted(order_ids), "exchange_events": evidence[:200], "exchange_event_count": len(evidence),
            "telemetry_truncated": len(records) == 200, "strategy_id": records[0]["strategy_id"],
            "coverage": "Bot-declared intent and HTTP boundaries. Timeouts have unknown outcomes; cancel 404 is not confirmation. Wall clocks are not used to infer exchange latency. No bot response manufactures a fill."}
