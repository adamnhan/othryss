"""Read-only request latency and cancellation/fill timing evidence.

No exchange clock synchronization is assumed. HTTP success is not a terminal
cancel acknowledgement unless the response itself reports the target canceled.
"""
import json
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from .replay import timestamp

POLICY = {
    "version": "execution-1",
    "clock_guard_seconds": 1,
    "max_local_clock_drift_ms": 250,
    "max_trace_records": 200,
    "max_session_span": 5000,
    "max_fills_per_cancel": 100,
    "p95_minimum_samples": 20,
    "latency": "Local monotonic request elapsed, including retries; transport timings are HTTP round trips, not exchange processing time.",
    "fills": "Exchange execution timestamps compared with local request timestamps using a heuristic one-second guard. Clocks are not synchronized; suspected stale fills are not proof of causality. Import receipt time is not execution time.",
    "coverage": "Displayed requests and retained, exactly linked fills only. No matching fills observed does not establish that none occurred. Summaries are page-scoped, with distinct fill IDs counted once per category.",
}


def milliseconds(ns):
    return format(Decimal(ns) / Decimal(1000000), "f")


def duration(value):
    if isinstance(value, bool) or not str(value).isdigit() or len(str(value)) > 20:
        raise ValueError("invalid duration")
    return int(value)


def evaluate(records, *, complete_span=True, truncated=False):
    """Evaluate one request; full-session span completeness is supplied by reader."""
    first = records[0] if records else {}
    result = {
        "session_id": first.get("session_id"), "request_id": first.get("payload", {}).get("request_id"),
        "instrument_id": first.get("instrument_id"), "subaccount": first.get("subaccount"),
        "strategy_id": first.get("strategy_id"), "run_id": first.get("run_id"),
        "operation": first.get("payload", {}).get("operation", "unknown"),
        "started_at": first.get("occurred_at"), "response_at": None,
        "order_id": None, "outcome": "unknown", "http_status": None,
        "timing_status": "unavailable", "reason": None, "elapsed_ms": None,
        "transport_ms": None, "retries": None, "attempts": [],
        "wall_clock_valid": False, "cancel_confirmation": "unconfirmed",
        "records": records, "trace_truncated": truncated, "fills": [], "fills_truncated": False,
    }
    try:
        if not records or truncated or not complete_span:
            raise ValueError("incomplete_trace")
        identity = ("session_id", "instrument_id", "subaccount", "strategy_id", "run_id", "account", "environment", "workspace")
        if any(any(r.get(k) != first.get(k) for k in identity) or
               r["payload"].get("request_id") != result["request_id"] or
               r["payload"].get("operation") != result["operation"] for r in records):
            raise ValueError("request_identity_mismatch")
        if result["operation"] not in {"submit", "amend", "cancel"}:
            raise ValueError("unsupported_operation")
        by_type = defaultdict(list)
        previous = None
        for r in records:
            mono = duration(r["monotonic_ns"])
            if previous and (r["sequence"] <= previous["sequence"] or mono < duration(previous["monotonic_ns"])):
                raise ValueError("sequence_or_monotonic_reversal")
            previous = r
            by_type[r["type"]].append(r)
        if any(len(by_type[k]) != 1 for k in ("ORDER_INTENT", "ORDER_REQUEST", "ORDER_RESPONSE")):
            raise ValueError("incomplete_request_lifecycle")
        intent, request, response = (by_type[k][0] for k in ("ORDER_INTENT", "ORDER_REQUEST", "ORDER_RESPONSE"))
        p = response["payload"]
        result.update(started_at=request["occurred_at"], response_at=response["occurred_at"],
                      order_id=request["payload"].get("order_id") if result["operation"] in {"cancel", "amend"} else p.get("order_id"))
        if not intent["sequence"] < request["sequence"] < response["sequence"] or records[-1] != response:
            raise ValueError("invalid_request_lifecycle")
        attempts = p.get("attempts")
        if type(attempts) is not int or not 1 <= attempts <= 98:
            raise ValueError("invalid_attempt_count")
        starts, ends = by_type["HTTP_ATTEMPT"], by_type["HTTP_RESPONSE"]
        if len(starts) != attempts or len(ends) != attempts:
            raise ValueError("incomplete_attempts")
        measured = []
        last_sequence = request["sequence"]
        for i, (start, end) in enumerate(zip(starts, ends), 1):
            if start["payload"].get("attempt") != i or end["payload"].get("attempt") != i or not last_sequence < start["sequence"] < end["sequence"] < response["sequence"]:
                raise ValueError("invalid_attempt_order")
            elapsed = duration(end["payload"]["duration_ns"])
            measured.append(elapsed)
            result["attempts"].append({"attempt": i, "elapsed_ms": milliseconds(elapsed),
                                       "http_status": end["payload"].get("http_status"), "outcome": end["payload"].get("outcome")})
            last_sequence = end["sequence"]
        elapsed = duration(p["duration_ns"])
        if sum(measured) > elapsed:
            raise ValueError("inconsistent_durations")
        code, outcome = p.get("http_status"), p.get("outcome")
        if outcome not in {"http_success", "http_error", "unknown"} or (outcome != "unknown" and
                (type(code) is not int or not 100 <= code <= 599 or (200 <= code < 300) != (outcome == "http_success"))):
            raise ValueError("inconsistent_outcome")
        # Check local wall/monotonic agreement throughout the request. Exchange
        # clock offset remains unknown even when this local check passes.
        wall_valid = all(abs((timestamp(b["occurred_at"]) - timestamp(a["occurred_at"])).total_seconds() -
                         (duration(b["monotonic_ns"]) - duration(a["monotonic_ns"])) / 1e9) <= .25
                         for a, b in zip(records, records[1:]))
        wall_valid = wall_valid and abs((timestamp(response["occurred_at"]) - timestamp(request["occurred_at"])).total_seconds() -
                         (duration(response["monotonic_ns"]) - duration(request["monotonic_ns"])) / 1e9) <= .25
        result.update(timing_status="measured", elapsed_ms=milliseconds(elapsed), transport_ms=milliseconds(sum(measured)),
                      retries=attempts - 1, outcome=outcome, http_status=code, wall_clock_valid=wall_valid)
        if result["operation"] == "cancel" and outcome == "http_success" and p.get("status") == "canceled" and p.get("order_id") == result["order_id"] and result["order_id"]:
            result["cancel_confirmation"] = "target_reported_canceled"
    except (KeyError, TypeError, ValueError) as exc:
        result["reason"] = str(exc) if isinstance(exc, ValueError) else "invalid_record"
        result["attempts"] = []
    return result


def classify_fill(request, fill):
    """Classifications describe timestamps, never proven exchange causality."""
    if request["timing_status"] != "measured" or not request["wall_clock_valid"]:
        return "timing_unavailable"
    try:
        at = timestamp(fill["occurred_at"])
        start, end = timestamp(request["started_at"]), timestamp(request["response_at"])
        guard = timedelta(seconds=POLICY["clock_guard_seconds"])
        if at < start - guard:
            return "late_observation_only" if timestamp(fill["received_at"]) > start else "before_request"
        if at <= start + guard:
            return "timing_uncertain"
        if at <= end + guard:
            return "cancel_in_flight_or_clock_uncertain"
        if request["cancel_confirmation"] == "target_reported_canceled":
            return "suspected_after_cancel_response"
        return "after_unconfirmed_cancel"
    except (KeyError, TypeError, ValueError):
        return "timing_unavailable"


def summary(rows):
    groups = defaultdict(list)
    fills = defaultdict(set)
    for r in rows:
        if r["timing_status"] == "measured":
            groups[(r["operation"], r["outcome"])].append(Decimal(r["elapsed_ms"]))
        for f in r["fills"]:
            fills[f["classification"]].add(f["event"]["event_id"])
    latency = []
    for (operation, outcome), values in sorted(groups.items()):
        values.sort()
        n = len(values)
        latency.append({"operation": operation, "outcome": outcome, "samples": n,
                        "p50_ms": format(values[(n + 1) // 2 - 1], "f"),
                        "p95_ms": format(values[(95 * n + 99) // 100 - 1], "f") if n >= 20 else None})
    return {"requests": len(rows), "unavailable": sum(r["timing_status"] != "measured" for r in rows),
            "latency": latency, "fill_classifications": {k: len(v) for k, v in sorted(fills.items())}}


def request_analysis(db, scope, session, request):
    """One bounded request analysis shared by catalog and order investigation."""
    raw = db.execute("""SELECT canonical_json FROM telemetry_records
        WHERE scope_id=? AND session_id=? AND request_id=? ORDER BY sequence LIMIT 201""",
                     (scope, session, request)).fetchall()
    records = [json.loads(r[0]) for r in raw[:200]]
    if not records:
        return evaluate([])
    lo, hi = records[0]["sequence"], records[-1]["sequence"]
    span = hi - lo + 1
    complete = span <= 5000 and db.execute("""SELECT COUNT(*) FROM telemetry_records
        WHERE scope_id=? AND session_id=? AND sequence BETWEEN ? AND ?""",
        (scope, session, lo, hi)).fetchone()[0] == span
    row = evaluate(records, complete_span=complete, truncated=len(raw) > 200)
    # Link incomplete cancellations too, but never infer timing from them.
    requests = [r for r in records if r["type"] == "ORDER_REQUEST"]
    if row["operation"] == "cancel" and len(requests) == 1 and row["reason"] != "request_identity_mismatch":
        row["order_id"] = requests[0]["payload"].get("order_id")
        if row["order_id"] and type(row["subaccount"]) is int:
            matches = db.execute("""SELECT canonical_json,received_at FROM events
                WHERE scope_id=? AND order_id=? AND instrument_id=? AND type='ORDER_FILL'
                AND json_extract(canonical_json,'$.payload.subaccount')=?
                ORDER BY occurred_at DESC,event_id LIMIT 101""",
                (scope, row["order_id"], row["instrument_id"], row["subaccount"])).fetchall()
            row["fills_truncated"] = len(matches) > 100
            for match in matches[:100]:
                fill = json.loads(match["canonical_json"])
                fill["received_at"] = match["received_at"]
                row["fills"].append({"classification": classify_fill(row, fill), "event": fill})
    return row


def catalog(reader, scope, instrument="", operation="", limit=25, offset=0):
    reader.account(scope)
    if not 1 <= limit <= 50 or not 0 <= offset <= 100000 or len(instrument) > 200 or operation not in {"", "submit", "cancel", "amend"}:
        raise ValueError("Invalid execution filter or pagination")
    db = reader.db
    where, args = "scope_id=? AND request_id IS NOT NULL", [scope]
    if instrument:
        where += " AND instrument_id=?"; args.append(instrument)
    if operation:
        where += " AND json_extract(canonical_json,'$.payload.operation')=?"; args.append(operation)
    groups = db.execute(f"""SELECT session_id,request_id,MIN(occurred_at) started_at
        FROM telemetry_records WHERE {where} GROUP BY session_id,request_id
        ORDER BY started_at DESC,session_id,request_id LIMIT ? OFFSET ?""", [*args, limit + 1, offset]).fetchall()
    rows = []
    for group in groups[:limit]:
        row = request_analysis(db, scope, group["session_id"], group["request_id"])
        rows.append(row)
    from .collector import freshness
    return {"scope_id": scope, "policy": POLICY, "rows": rows, "summary": summary(rows),
            "limit": limit, "offset": offset, "has_more": len(groups) > limit,
            "filters": {"instrument": instrument, "operation": operation}, "sync": freshness(db, scope)}
