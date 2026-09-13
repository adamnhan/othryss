"""Conservative position comparisons from explicit snapshots and audited fills."""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .history import run_import
from .kalshi import fixed, instant
from .replay import number, timestamp
from .storage import encode, utc_now

BOUNDARY_SECONDS = 2


def configure_target(store, scope, ticker, subaccount=0, grace=120):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", ticker) or type(subaccount) is not int or not 0 <= subaccount <= 63 or not 30 <= grace <= 3600:
        raise ValueError("Invalid reconciliation ticker, subaccount or grace period")
    old = store.db.execute("SELECT * FROM reconciliation_targets WHERE scope_id=?", (scope,)).fetchone()
    if old and (old["instrument_id"], old["subaccount"], old["grace_seconds"]) != (ticker, subaccount, grace):
        raise ValueError("This account already has a different reconciliation target; target migration is not automatic")
    with store.db:
        store.db.execute("INSERT OR IGNORE INTO reconciliation_targets(scope_id,instrument_id,subaccount,grace_seconds) VALUES (?,?,?,?)", (scope, ticker, subaccount, grace))


def pages(client, endpoint, key, params, *, max_pages=20, progress=None):
    rows, cursor, seen = [], "", set()
    for _ in range(max_pages):
        response = client.request(endpoint, params | ({"cursor": cursor} if cursor else {}))
        if not isinstance(response.get(key), list) or not isinstance(response.get("cursor"), str):
            raise ValueError("Incomplete position/settlement pagination")
        if not all(isinstance(row, dict) for row in response[key]):
            raise ValueError("Malformed position/settlement row")
        rows.extend(response[key])
        if progress:
            progress({"phase": "reconciliation", "endpoint": endpoint})
        cursor = response["cursor"]
        if not cursor:
            return rows
        if cursor in seen:
            raise ValueError("Position/settlement cursor cycle")
        seen.add(cursor)
    raise ValueError("Position/settlement page budget exceeded")


def observation(row):
    value = dict(row)
    value["evidence"] = json.loads(value.pop("evidence_json"))
    return value


def compare(baseline, target, fills, *, audit, grace=120):
    """Pure decimal comparison. All ambiguous windows stay pending or unavailable."""
    result = {"status": "unavailable", "reason": "Position evidence is unavailable", "baseline": baseline, "target": target,
              "expected_quantity": None, "observed_quantity": target.get("quantity") if target else None,
              "difference": None, "net_fill_quantity": None, "fill_count": 0, "fills": [], "audit": audit,
              "boundary_seconds": BOUNDARY_SECONDS, "grace_seconds": grace,
              "interpretation": "A difference requires review. These REST observations do not identify a cause or prove a trading incident."}
    def status(value, reason):
        result.update(status=value, reason=reason)
        return result
    if not target or target["quantity"] is None:
        return result
    if not baseline:
        return status("waiting_baseline", "Waiting for a quiet, explicit position baseline and a later mature snapshot")
    if (baseline["scope_id"], baseline["instrument_id"], baseline["subaccount"]) != (target["scope_id"], target["instrument_id"], target["subaccount"]):
        raise ValueError("Position comparison scope mismatch")
    if baseline["quantity"] is None:
        return result
    if baseline["snapshot_id"] == target["snapshot_id"]:
        return status("waiting_snapshot", "Baseline established; waiting for a later snapshot")
    low, high = timestamp(baseline["received_at"]), timestamp(target["request_started_at"])
    if low >= high:
        return status("pending_timing", "Snapshot request windows overlap")
    end = timestamp(target["received_at"])
    if not audit or audit["status"] != "traversed" or timestamp(audit["started_at"]) < end + timedelta(seconds=grace):
        return status("waiting_fills", "Waiting for a complete fill audit after the snapshot's visibility grace period")
    for snap in (baseline, target):
        market = snap["evidence"]["market"]
        if market.get("market_type") != "binary":
            return status("unavailable", "Only binary market position semantics are supported")
        if market.get("status") != "active":
            return status("lifecycle_blocked", "Market is not active; settlement or closure requires separate accounting")
    settlements = target["evidence"]["settlements"]
    if any(timestamp(s["settled_time"]) >= timestamp(baseline["request_started_at"]) - timedelta(seconds=BOUNDARY_SECONDS) for s in settlements):
        return status("lifecycle_blocked", "Settlement evidence overlaps this baseline; no fill-only mismatch is asserted")
    margin = timedelta(seconds=BOUNDARY_SECONDS)
    start_boundary = timestamp(baseline["request_started_at"]) - margin
    end_boundary = end + margin
    selected = []
    identities = {}
    for event in fills:
        if event["scope_id"] != target["scope_id"] or event["instrument_id"] != target["instrument_id"]:
            raise ValueError("Fill comparison scope mismatch")
        at = timestamp(event["occurred_at"])
        if not start_boundary <= at <= end_boundary:
            continue
        p = event["payload"]
        if p.get("subaccount") is None:
            return status("unavailable", "A fill in this window lacks a subaccount identity")
        if p["subaccount"] != target["subaccount"]:
            continue
        if at <= low + margin or at >= high - margin:
            return status("pending_timing", "A fill overlaps a snapshot request window or its clock margin")
        identity = p["fill_id"]
        if identity in identities:
            if identities[identity] != (at, p):
                raise ValueError("Conflicting fill identity in position comparison")
            continue
        identities[identity] = (at, p)
        selected.append(event)
    delta = Decimal(0)
    for event in selected:
        p = event["payload"]
        if p["price_basis"] != "yes_outcome" or p["exposure_direction"] not in {"increase_yes", "decrease_yes"}:
            raise ValueError("Unsupported fill exposure semantics")
        if number(p["quantity"]) <= 0:
            raise ValueError("Fill quantity must be positive")
        delta += number(p["quantity"]) * (1 if p["exposure_direction"] == "increase_yes" else -1)
    expected = number(baseline["quantity"]) + delta
    difference = number(target["quantity"]) - expected
    result.update(expected_quantity=fixed(expected), difference=fixed(difference), net_fill_quantity=fixed(delta), fill_count=len(selected),
                  fills=selected[:100], fills_truncated=len(selected) > 100)
    return status("consistent" if difference == 0 else "unexplained_difference",
                  "Baseline plus audited net fills matches the later position" if difference == 0 else "Observed position differs from baseline plus audited net fills; inspect the evidence")


def collect(store, client, scope, account, *, progress=None):
    target = store.db.execute("SELECT * FROM reconciliation_targets WHERE scope_id=?", (scope,)).fetchone()
    if target is None:
        return None
    with store.db:
        store.db.execute("UPDATE reconciliation_targets SET last_attempt_at=? WHERE scope_id=?", (utc_now(), scope))
    try:
        access = client.verify_read_only()
        if access["subaccount"] is not None and access["subaccount"] != target["subaccount"]:
            raise ValueError("Position target is outside the key subaccount scope")
        if access["subaccount"] != getattr(client, "credential_subaccount", None):
            raise ValueError("Credential scope changed during collection")
        ticker, sub = target["instrument_id"], target["subaccount"]
        # A complete ticker audit catches delayed fills beyond the normal overlap.
        report = run_import(store, client, scope, account, ticker=ticker, page_size=500, max_pages=100,
                            collection={"reconciliation_audit": True}, progress=progress)
        if report["status"] != "traversed":
            raise ValueError("Reconciliation fill audit was not complete and cutoff-stable")
        started = utc_now()
        rows = pages(client, "/portfolio/positions", "market_positions", {"ticker": ticker, "subaccount": sub, "count_filter": "position,total_traded", "limit": 100}, progress=progress)
        received = utc_now()
        if any(row.get("ticker") != ticker for row in rows) or len(rows) > 1:
            raise ValueError("Position response has unexpected market or duplicate rows")
        row = rows[0] if rows else None
        if row and row.get("position_fp") is None:
            raise ValueError("Fixed-point position quantity is unavailable")
        if row and row.get("subaccount_number", sub) != sub:
            raise ValueError("Position subaccount mismatch")
        market = client.request("/markets/" + ticker).get("market")
        if not isinstance(market, dict) or market.get("ticker") != ticker:
            raise ValueError("Market identity mismatch")
        settlement_rows = pages(client, "/portfolio/settlements", "settlements", {"ticker": ticker, "subaccount": sub, "limit": 100}, progress=progress)
        settlements = []
        for s in settlement_rows:
            if s.get("ticker") != ticker or s.get("subaccount_number", sub) != sub or not s.get("settled_time"):
                raise ValueError("Settlement scope or timestamp unavailable")
            settlements.append({"ticker": ticker, "settled_time": instant(s["settled_time"]), "market_result": s.get("market_result"),
                                "yes_count": fixed(s["yes_count_fp"]) if s.get("yes_count_fp") is not None else None,
                                "no_count": fixed(s["no_count_fp"]) if s.get("no_count_fp") is not None else None})
        evidence = {"position": {"quantity": fixed(row["position_fp"]), "source_updated_at": instant(row["last_updated_ts"]) if row.get("last_updated_ts") else None} if row else None,
                    "market": {k: market.get(k) for k in ("ticker", "status", "market_type", "close_time", "settlement_ts", "result")},
                    "settlements": settlements, "settlements_checked_at": utc_now(), "request_scope": {"ticker": ticker, "subaccount": sub},
                    "position_endpoint": "/portfolio/positions", "settlement_endpoint": "/portfolio/settlements", "audit_run_id": report["run_id"]}
        snapshot_id = uuid.uuid4().hex
        with store.db:
            store.db.execute("INSERT INTO position_observations VALUES (?,?,?,?,?,?,?,?)", (snapshot_id, scope, ticker, sub, started, received, fixed(row["position_fp"]) if row else None, encode(evidence)))
        result = evaluate(store, target, report)
        with store.db:
            store.db.execute("INSERT INTO reconciliation_checks VALUES (?,?,?,?,?)", (uuid.uuid4().hex, scope, utc_now(), result["status"], encode(result)))
            store.db.execute("UPDATE reconciliation_targets SET last_success_at=?,last_error=NULL WHERE scope_id=?", (utc_now(), scope))
        return result
    except (Exception, KeyboardInterrupt):
        with store.db:
            store.db.execute("UPDATE reconciliation_targets SET last_error=? WHERE scope_id=?", ("Position check incomplete; prior results retained. Inspect collection logs or retry.", scope))
        raise


def evaluate(store, target, report):
    scope = target["scope_id"]
    audit = {k: report[k] for k in ("run_id", "status", "started_at", "finished_at")}
    mature_before = timestamp(report["started_at"]) - timedelta(seconds=target["grace_seconds"])
    current = observation(store.db.execute("SELECT * FROM position_observations WHERE scope_id=? ORDER BY received_at DESC,snapshot_id DESC LIMIT 1", (scope,)).fetchone())
    base_row = store.db.execute("SELECT * FROM position_observations WHERE scope_id=? AND snapshot_id=?", (scope, target["baseline_id"])).fetchone()
    baseline = observation(base_row) if base_row else None
    # Once anchored, only the baseline and latest mature/current snapshots are read.
    candidates = 1 if baseline else 50
    mature = [observation(r) for r in store.db.execute("SELECT * FROM position_observations WHERE scope_id=? AND received_at<=? ORDER BY received_at DESC,snapshot_id DESC LIMIT ?", (scope, mature_before.isoformat(timespec="microseconds"), candidates))][::-1]
    first = baseline or (mature[0] if mature else current)
    low = (timestamp(first["request_started_at"]) - timedelta(seconds=BOUNDARY_SECONDS)).isoformat(timespec="microseconds")
    high = (timestamp(current["received_at"]) + timedelta(seconds=BOUNDARY_SECONDS)).isoformat(timespec="microseconds")
    fills = [json.loads(r[0]) for r in store.db.execute("SELECT canonical_json FROM events WHERE scope_id=? AND instrument_id=? AND type='ORDER_FILL' AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at,event_id", (scope, target["instrument_id"], low, high))]
    if baseline is None:
        margin = timedelta(seconds=BOUNDARY_SECONDS)
        for s in mature:
            if s["quantity"] is None or s["evidence"]["market"].get("status") != "active" or s["evidence"]["market"].get("market_type") != "binary":
                continue
            start, end = timestamp(s["request_started_at"]) - margin, timestamp(s["received_at"]) + margin
            if any(start <= timestamp(f["occurred_at"]) <= end and f["payload"].get("subaccount") in (None, target["subaccount"]) for f in fills):
                continue
            baseline = s
            with store.db:
                store.db.execute("UPDATE reconciliation_targets SET baseline_id=? WHERE scope_id=?", (s["snapshot_id"], scope))
            break
    latest = mature[-1] if mature else current
    # Current settlement/lifecycle evidence must also block a historical green result.
    result = compare(baseline, latest, fills, audit=audit, grace=target["grace_seconds"])
    result["latest_capture"] = current
    current_evidence = current["evidence"]
    if current_evidence["market"].get("status") != "active" or current_evidence["settlements"]:
        result.update(status="lifecycle_blocked", reason="Market closure or settlement is recorded; fill-only comparisons are suspended")
    elif current["quantity"] is None or current_evidence["market"].get("market_type") != "binary":
        result.update(status="unavailable", reason="Latest position is missing or market type is unsupported; no zero position is inferred")
    return result


def read(db, scope, check_id=None):
    if db.execute("PRAGMA user_version").fetchone()[0] < 3:
        return {"configured": False}
    row = db.execute("SELECT * FROM reconciliation_targets WHERE scope_id=?", (scope,)).fetchone()
    if row is None:
        return {"configured": False}
    target = dict(row)
    recent = [dict(r) for r in db.execute("SELECT check_id,checked_at,status FROM reconciliation_checks WHERE scope_id=? ORDER BY checked_at DESC,check_id DESC LIMIT 20", (scope,))]
    if check_id:
        latest = db.execute("SELECT checked_at,result_json FROM reconciliation_checks WHERE scope_id=? AND check_id=?", (scope, check_id)).fetchone()
        if latest is None:
            raise ValueError("Unknown reconciliation check for this account")
    else:
        latest = db.execute("SELECT checked_at,result_json FROM reconciliation_checks WHERE scope_id=? ORDER BY checked_at DESC,check_id DESC LIMIT 1", (scope,)).fetchone()
    return {"configured": True, "target": target, "checked_at": latest[0] if latest else None,
            "recent_checks": recent,
            "result": json.loads(latest[1]) if latest else None,
            "stale": not latest or (datetime.now(timezone.utc) - timestamp(latest[0])).total_seconds() > 300}
