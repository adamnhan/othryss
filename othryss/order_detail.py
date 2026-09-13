"""Read-only order investigation, with conservative links and bounded evidence."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import execution, markouts
from .replay import timestamp

LIMITS = {"exchange_events": 500, "requests": 25, "client_ids": 50,
          "markout_fills": 25, "reference_quotes": 100, "reference_gaps": 100,
          "incidents": 20, "incident_actions": 20, "check_bytes": 131072,
          "timeline": 1000, "reference_window_seconds": 300}


def request_links(db, scope, instrument, order, subaccount):
    result = {"rows": [], "truncated": False, "excluded_requests": 0,
              "ambiguous_client_ids": [], "client_ids_truncated": False,
              "policy": execution.POLICY, "summary": execution.summary([])}
    if subaccount is None or db.execute("PRAGMA user_version").fetchone()[0] < 4:
        return result
    raw = db.execute("""SELECT DISTINCT json_extract(canonical_json,'$.payload.client_order_id') client
        FROM events WHERE scope_id=? AND instrument_id=? AND order_id=?
        AND json_extract(canonical_json,'$.payload.subaccount')=? AND client IS NOT NULL
        ORDER BY client LIMIT ?""", (scope, instrument, order, subaccount, LIMITS["client_ids"] + 1)).fetchall()
    result["client_ids_truncated"] = len(raw) > LIMITS["client_ids"]
    clients = []
    for row in raw[:LIMITS["client_ids"]]:
        client = row[0]
        matches = db.execute("""SELECT DISTINCT order_id FROM events
            WHERE scope_id=? AND instrument_id=? AND json_extract(canonical_json,'$.payload.subaccount')=?
            AND json_extract(canonical_json,'$.payload.client_order_id')=? LIMIT 2""",
            (scope, instrument, subaccount, client)).fetchall()
        if client and len(matches) == 1 and matches[0][0] == order:
            clients.append(client)
        else:
            result["ambiguous_client_ids"].append(client)
    match = "json_extract(canonical_json,'$.payload.order_id')=? OR json_extract(canonical_json,'$.payload.previous_order_id')=?"
    args = [scope, instrument, subaccount, order, order]
    if clients:
        match += " OR json_extract(canonical_json,'$.payload.client_order_id') IN (" + ",".join("?" for _ in clients) + ")"
        args.extend(clients)
    candidates = db.execute(f"""SELECT session_id,request_id,MAX(occurred_at) last_at FROM telemetry_records
        WHERE scope_id=? AND instrument_id=? AND subaccount=? AND request_id IS NOT NULL AND ({match})
        GROUP BY session_id,request_id ORDER BY last_at DESC,session_id,request_id LIMIT ?""",
        [*args, LIMITS["requests"] + 1]).fetchall()
    result["truncated"] = len(candidates) > LIMITS["requests"]
    for candidate in candidates[:LIMITS["requests"]]:
        row = execution.request_analysis(db, scope, candidate["session_id"], candidate["request_id"])
        records = row["records"]
        identity = ("session_id", "instrument_id", "subaccount", "strategy_id", "run_id", "account", "environment", "workspace")
        # Validate independently of timing: evaluate may stop early on an incomplete trace.
        if not records or any(r.get("instrument_id") != instrument or r.get("subaccount") != subaccount or
                any(r.get(k) != records[0].get(k) for k in identity) for r in records):
            result["excluded_requests"] += 1
            continue
        ids = {r["payload"].get(k) for r in records for k in ("order_id", "previous_order_id")} - {None, ""}
        client_link = any(r["payload"].get("client_order_id") in clients for r in records)
        if order not in ids and (not client_link or ids):
            # A client ID must not override a conflicting explicit exchange order ID.
            result["excluded_requests"] += 1
            continue
        row["link_basis"] = "exchange_order_id" if order in ids else "unique_client_order_id"
        row["related_order_ids"] = sorted(ids - {order})
        row["fills"] = [f for f in row["fills"] if f["event"]["payload"]["order_id"] == order]
        result["rows"].append(row)
    result["summary"] = execution.summary(result["rows"])
    return result


def check_evidence(db, scope, check_id):
    # Bound raw JSON in SQL before materializing potentially large account snapshots.
    row = db.execute("""SELECT check_id,snapshot_id,checked_at,status,
        length(CAST(result_json AS BLOB)) bytes,
        CASE WHEN length(CAST(result_json AS BLOB))<=? THEN result_json END result_json
        FROM bot_checks WHERE scope_id=? AND check_id=?""", (LIMITS["check_bytes"], scope, check_id)).fetchone()
    if not row:
        return {"check_id": check_id, "unavailable": True}
    item = dict(row)
    raw = item.pop("result_json")
    item["result"] = json.loads(raw) if raw else None
    item["truncated"] = raw is None
    snapshot = db.execute("""SELECT snapshot_id,instrument_id,subaccount,started_at,received_at,
        length(CAST(evidence_json AS BLOB)) bytes,
        CASE WHEN length(CAST(evidence_json AS BLOB))<=? THEN evidence_json END evidence_json
        FROM bot_exchange WHERE scope_id=? AND snapshot_id=?""", (LIMITS["check_bytes"], scope, row["snapshot_id"])).fetchone()
    if snapshot:
        item["exchange"] = dict(snapshot)
        raw = item["exchange"].pop("evidence_json")
        item["exchange"].update(evidence=json.loads(raw) if raw else None, truncated=raw is None)
    return item


def incident_links(db, scope, instrument, order, subaccount, start, end):
    result = {"rows": [], "truncated": False,
              "coverage": "Direct links require a matching order or fill ID and subaccount. Market context overlaps the retained order evidence window (with 120 seconds of grace); it does not establish a cause."}
    if subaccount is None or db.execute("PRAGMA user_version").fetchone()[0] < 5:
        return result
    # Subaccount comes from the monitor, or the primary-only inventory rule.
    direct = """(i.entity=? AND i.rule IN ('local_order_missing','unknown_exchange_order','remaining_mismatch') OR
        i.rule IN ('missing_local_fill','unmatched_local_fill') AND EXISTS (SELECT 1 FROM events e WHERE e.scope_id=i.scope_id
        AND e.instrument_id=? AND e.order_id=? AND e.type='ORDER_FILL'
        AND json_extract(e.canonical_json,'$.payload.subaccount')=?
        AND json_extract(e.canonical_json,'$.payload.fill_id')=i.entity))"""
    # Keep rule names explicit so a ticker/position quantity cannot masquerade as an order ID.
    direct_args = [order, instrument, order, subaccount]
    rows = db.execute(f"""SELECT i.*, CASE WHEN {direct} THEN 'direct' ELSE 'market_context' END relation
        FROM incidents i LEFT JOIN bot_monitors m ON m.scope_id=i.scope_id AND m.session_id=i.session_id
        WHERE i.scope_id=? AND ((i.instrument_id=? AND m.subaccount=?) OR
            (?=0 AND i.rule='inventory_limit' AND i.instrument_id='PRIMARY-ACCOUNT' AND i.entity=?))
        AND ({direct} OR (i.rule IN ('position_mismatch','source_stale','inventory_limit') AND i.first_seen<=? AND i.last_seen>=?))
        ORDER BY CASE WHEN {direct} THEN 0 ELSE 1 END, i.last_seen DESC,i.incident_id LIMIT ?""",
        [*direct_args, scope, instrument, subaccount, subaccount, instrument, *direct_args,
         markouts.iso(end + timedelta(seconds=120)), markouts.iso(start - timedelta(seconds=120)),
         *direct_args, LIMITS["incidents"] + 1]).fetchall()
    result["truncated"] = len(rows) > LIMITS["incidents"]
    for row in rows[:LIMITS["incidents"]]:
        item = dict(row)
        actions = db.execute("""SELECT occurred_at,action,note FROM incident_actions WHERE incident_id=?
            ORDER BY occurred_at DESC,action_id LIMIT ?""", (row["incident_id"], LIMITS["incident_actions"] + 1)).fetchall()
        item["actions"] = [dict(a) for a in reversed(actions[:LIMITS["incident_actions"]])]
        item["actions_truncated"] = len(actions) > LIMITS["incident_actions"]
        item["first_evidence"] = check_evidence(db, scope, row["first_check"])
        item["latest_evidence"] = check_evidence(db, scope, row["last_check"])
        result["rows"].append(item)
    return result


def reference_evidence(reader, path, scope, instrument, order, start, end, now):
    refs = None
    low, high = max(start - timedelta(seconds=20), end - timedelta(seconds=LIMITS["reference_window_seconds"])), end + timedelta(seconds=30)
    context = {"rows": [], "gaps": [], "status": "unavailable", "truncated": False,
               "from": markouts.iso(low), "through": markouts.iso(high),
               "window_limited": low > start - timedelta(seconds=20),
               "coverage": "Market observations at local receipt time, not quotes proven visible to this bot. Latest retained observations in this bounded order window. Markouts independently query each displayed fill's interval."}
    try:
        if path is not None and Path(path).is_file():
            refs = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
            refs.row_factory = sqlite3.Row
            refs.execute("PRAGMA query_only=ON"); refs.execute("BEGIN")
            if refs.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported reference schema")
            raw = refs.execute("""SELECT quote_id,record_json FROM quotes WHERE scope_id=? AND instrument_id=?
                AND received_at>=? AND received_at<=? ORDER BY received_at DESC,quote_id DESC LIMIT ?""",
                (scope, instrument, context["from"], context["through"], LIMITS["reference_quotes"] + 1)).fetchall()
            gaps = refs.execute("""SELECT * FROM gaps WHERE scope_id=? AND instrument_id=?
                AND started_at<=? AND (ended_at IS NULL OR ended_at>?) ORDER BY gap_id DESC LIMIT ?""",
                (scope, instrument, context["through"], context["from"], LIMITS["reference_gaps"] + 1)).fetchall()
            context.update(status="available" if raw else "no_overlap",
                           rows=[json.loads(r["record_json"]) | {"quote_id": r["quote_id"]} for r in reversed(raw[:LIMITS["reference_quotes"]])],
                           gaps=[dict(r) for r in gaps[:LIMITS["reference_gaps"]]],
                           truncated=len(raw) > LIMITS["reference_quotes"] or len(gaps) > LIMITS["reference_gaps"])
        computed = markouts.catalog(reader, None, scope, instrument, order, LIMITS["markout_fills"], now=now, reference_connection=refs)
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        context.update(status="unavailable", reason="reference_store_unavailable", rows=[], gaps=[])
        computed = markouts.catalog(reader, None, scope, instrument, order, LIMITS["markout_fills"], now=now)
    finally:
        if refs is not None:
            refs.close()
    computed["truncated"] = len(computed["rows"]) < computed["total"]
    return context, computed


def investigate(reader, reference_path, scope, instrument, order, *, now=None):
    if not instrument or not order or len(instrument) > 200 or len(order) > 200:
        raise ValueError("An instrument and order ID of at most 200 characters are required")
    now = now or datetime.now(timezone.utc)
    data = reader.order(scope, instrument, order, limit=100)
    events = data["events"]
    while len(events) < min(data["total"], LIMITS["exchange_events"]):
        events.extend(reader.order(scope, instrument, order, limit=100, offset=len(events))["events"])
    data.update(events=events, limit=len(events), offset=0)
    db = reader.db
    subs = [r[0] for r in db.execute("""SELECT DISTINCT json_extract(canonical_json,'$.payload.subaccount')
        FROM events WHERE scope_id=? AND instrument_id=? AND order_id=? LIMIT 3""", (scope, instrument, order))]
    subaccount = subs[0] if len(subs) == 1 and type(subs[0]) is int else None
    requests = request_links(db, scope, instrument, order, subaccount)
    times = [timestamp(e.get("occurred_at") or e["received_at"]) for e in events]
    times.extend(timestamp(r["occurred_at"]) for req in requests["rows"] for r in req["records"])
    start, end = min(times), max(times)
    references, computed = reference_evidence(reader, reference_path, scope, instrument, order, start, end, now)
    incidents = incident_links(db, scope, instrument, order, subaccount, start, end)
    timeline = []
    for e in events:
        timeline.append({"id": "exchange:" + e["event_id"], "source": "exchange", "at": e.get("occurred_at") or e["received_at"],
                         "clock": "exchange_source" if e.get("occurred_at") else "import_receipt_fallback", "kind": e["type"], "evidence": e})
    for req in requests["rows"]:
        for r in req["records"]:
            timeline.append({"id": "bot:" + r["event_id"], "source": "bot", "at": r["occurred_at"], "clock": "bot_local_wall",
                             "kind": r["type"], "request_id": req["request_id"], "evidence": r})
    for q in references["rows"]:
        timeline.append({"id": "quote:" + str(q["quote_id"]), "source": "reference", "at": q["received_at"],
                         "clock": "reference_local_receipt", "kind": "Market reference", "evidence": q})
    for i in incidents["rows"]:
        timeline.append({"id": "incident:" + i["incident_id"], "source": "incident", "at": i["first_seen"], "clock": "local_detection",
                         "kind": i["rule"], "relation": i["relation"], "evidence": {k: i[k] for k in ("incident_id", "rule", "entity", "status", "assessment", "first_seen", "last_seen")}})
    timeline.sort(key=lambda r: (timestamp(r["at"]), r["id"]))
    data.update(version="order-investigation-1", captured_at=markouts.iso(now), limits=dict(LIMITS),
                identity={"subaccount": subaccount, "status": "exact" if subaccount is not None else "unknown_or_ambiguous",
                          "coverage": "Cross-source order links require one known subaccount. Unknown or conflicting subaccounts disable bot and incident links; exchange totals still cover the selected account, market and order ID."},
                requests=requests, references=references, markouts=computed, incidents=incidents,
                timeline=timeline[-LIMITS["timeline"]:], timeline_total=len(timeline),
                exchange_truncated=len(events) < data["total"], timeline_truncated=len(timeline) > LIMITS["timeline"],
                export_scope="Exactly the displayed investigation snapshot, including bounded source records and coverage. History and references use separate read transactions, not an atomic cross-database snapshot.",
                timeline_coverage="Sorted timestamps aid navigation, not causal ordering. Exchange, bot and capture clocks are not synchronized. Bot session sequence and monotonic durations preserve local request order. Quotes and market incidents are context.")
    return data
