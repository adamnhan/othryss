"""Bot-state contract, scoped exchange captures, and conservative comparisons."""
import json
import re
import uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from .history import run_import
from .kalshi import fixed, normalize
from .reconciliation import pages
from .replay import timestamp
from .storage import encode, utc_now

STATE_KEYS = set("phase step_ok position baseline_position inventory position_basis orders owned_order_ids seen_fill_ids orders_complete ownership_complete fills_complete".split())
RULES = {"position_mismatch", "local_order_missing", "unknown_exchange_order", "remaining_mismatch", "missing_local_fill", "unmatched_local_fill"}
TOLERANCE = Decimal("0.000001")  # Legacy bot quantities originate as Python floats.
FRESH_SECONDS = 180
WRITER_RECOVERY_REASON = "Producer telemetry errors; waiting for 120 seconds without new errors and advancing evidence"
WRITER_OPEN_GRACE_SECONDS = 300


def identity(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value))


def quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"-?\d{1,18}(?:\.\d{1,18})?", value):
        raise ValueError("Invalid bounded decimal bot quantity")
    return Decimal(value)


def validate_state(p):
    if set(p) != STATE_KEYS or p["phase"] not in {"before_step", "after_step"} or p["position_basis"] != "baseline_plus_run_inventory_yes":
        raise ValueError("Unsupported bot state contract")
    for key in ("step_ok", "orders_complete", "ownership_complete", "fills_complete"):
        if type(p[key]) is not bool:
            raise ValueError("Explicit snapshot completeness flags required")
    if abs(quantity(p["baseline_position"]) + quantity(p["inventory"]) - quantity(p["position"])) > TOLERANCE:
        raise ValueError("Bot position does not equal declared baseline plus inventory")
    for key in ("owned_order_ids", "seen_fill_ids"):
        if not isinstance(p[key], list) or len(p[key]) > 64 or not all(identity(i) for i in p[key]) or len(set(p[key])) != len(p[key]):
            raise ValueError("Invalid bounded snapshot identity list")
    if not isinstance(p["orders"], list) or len(p["orders"]) > 2:
        raise ValueError("Invalid bot order snapshot")
    seen = set()
    for order in p["orders"]:
        if not isinstance(order, dict) or set(order) != {"order_id", "remaining", "role"} or not identity(order["order_id"]) or order["role"] not in {"entry", "exit"} or order["order_id"] in seen:
            raise ValueError("Invalid local order")
        seen.add(order["order_id"])
        if order["remaining"] is not None and quantity(order["remaining"]) < 0:
            raise ValueError("Negative remaining quantity")


def comparable(p):
    return {k: p[k] for k in STATE_KEYS - {"phase", "step_ok"}}


def compare(before, after, exchange):
    """No wall-clock subtraction is used to claim an exchange latency."""
    result = {"status": "pending", "reason": "Waiting for stable bot observations around the exchange capture",
              "findings": [], "evaluated_rules": [], "unknown_entities": [], "before": before, "after": after,
              "exchange_snapshot_id": exchange["snapshot_id"], "tolerance": str(TOLERANCE), "rule_version": "bot-state-1",
              "interpretation": "Bot position means its starting account position plus run inventory. Manual trading or another strategy can explain a difference. Unknown exchange orders are unattributed activity, not proof of a bot fault."}
    if not before or not after:
        return result
    for record in (before, after):
        if record["instrument_id"] != exchange["instrument_id"] or record["subaccount"] != exchange["subaccount"] or record.get("_scope_id", exchange["scope_id"]) != exchange["scope_id"]:
            raise ValueError("Comparison scope mismatch")
    if before["session_id"] != after["session_id"] or before["run_id"] != after["run_id"]:
        raise ValueError("Bot session changed across comparison")
    start, end = timestamp(exchange["started_at"]), timestamp(exchange["received_at"])
    low, high = timestamp(before["occurred_at"]), timestamp(after["occurred_at"])
    if not low <= start - timedelta(seconds=2) or not high >= end + timedelta(seconds=2) or (start-low).total_seconds() > FRESH_SECONDS or (high-end).total_seconds() > FRESH_SECONDS:
        return result
    left, right = before["payload"], after["payload"]
    if not left["step_ok"] or not right["step_ok"] or comparable(left) != comparable(right):
        return result
    evidence = exchange["evidence"]
    if not evidence.get("complete"):
        result.update(status="unavailable", reason="Exchange capture is incomplete")
        return result
    if evidence["market"].get("status") != "active" or evidence["market"].get("market_type") != "binary" or evidence.get("settlements"):
        result.update(status="unavailable", reason="Closure, settlement or unsupported instrument semantics")
        return result
    result.update(status="consistent", reason="Compared stable, scoped bot state with independent exchange evidence")
    def difference(rule, entity, message, **details):
        result["findings"].append({"rule": rule, "entity": entity, "message": message, "details": details})
    if evidence.get("position") is not None:
        result["evaluated_rules"].append("position_mismatch")
        if abs(quantity(left["position"]) - Decimal(evidence["position"])) > TOLERANCE:
            difference("position_mismatch", "position", "Bot position differs from exchange position", bot=left["position"], exchange=evidence["position"])
    else:
        result["position_coverage"] = "No explicit position row; zero was not inferred"
    active = {o["order_id"]: o for o in left["orders"]}
    resting = {o["order_id"]: o for o in evidence["orders"] if o["status"] == "resting"}
    if left["orders_complete"]:
        result["evaluated_rules"] += ["local_order_missing", "unknown_exchange_order", "remaining_mismatch"]
        for order_id, order in active.items():
            if order_id not in resting:
                result["unknown_entities"].append(["remaining_mismatch", order_id])
                difference("local_order_missing", order_id, "Bot retains an active order absent from the complete exchange resting-order view", order_id=order_id)
            elif order["remaining"] is not None and resting[order_id].get("remaining_quantity") is not None:
                if abs(quantity(order["remaining"]) - Decimal(resting[order_id]["remaining_quantity"])) > TOLERANCE:
                    difference("remaining_mismatch", order_id, "Remaining order quantity disagrees", bot=order["remaining"], exchange=resting[order_id]["remaining_quantity"], order_id=order_id)
            else:
                result["unknown_entities"].append(["remaining_mismatch", order_id])
        for order_id in resting.keys() - active.keys():
            difference("unknown_exchange_order", order_id, "Exchange resting order is absent from the bot's active order set; ownership needs review", order_id=order_id, bot_previously_owned=order_id in left["owned_order_ids"])
    if left["fills_complete"] and left["ownership_complete"]:
        result["evaluated_rules"] += ["missing_local_fill", "unmatched_local_fill"]
        seen = set(left["seen_fill_ids"])
        owned = set(left["owned_order_ids"])
        fills = {f["fill_id"]: f for f in evidence["fills"] if f["order_id"] in owned}
        for fill_id in fills.keys() - seen:
            difference("missing_local_fill", fill_id, "Exchange fill on a bot-owned order is missing from local fill tracking", fill_id=fill_id, order_id=fills[fill_id]["order_id"])
        for fill_id in seen - fills.keys():
            difference("unmatched_local_fill", fill_id, "Local fill ID has no match in the complete exchange history audit", fill_id=fill_id)
    if result["findings"]:
        result["status"] = "difference"
        result["reason"] = "Differences require persistence through the configured grace period before opening incidents"
    elif set(result["evaluated_rules"]) != RULES or result["unknown_entities"]:
        result.update(status="partial", reason="Available comparisons agree; some position, order-quantity or fill coverage remains unavailable")
    if not result["evaluated_rules"]:
        result.update(status="unavailable", reason="Snapshot coverage does not permit comparison")
    return result


def discover(store, scope, grace=120):
    for row in store.db.execute("SELECT session_id,instrument_id,subaccount FROM telemetry_records WHERE scope_id=? AND type='BOT_STATE' GROUP BY session_id,instrument_id,subaccount", (scope,)).fetchall():
        with store.db:
            store.db.execute("INSERT OR IGNORE INTO bot_monitors(scope_id,session_id,instrument_id,subaccount,grace_seconds) VALUES (?,?,?,?,?)", (scope, row["session_id"], row["instrument_id"], row["subaccount"], grace))


def source_status(db, scope, session, now):
    row = db.execute("SELECT health_json FROM telemetry_health WHERE scope_id=? AND session_id=?", (scope, session)).fetchone()
    health = json.loads(row[0]) if row else None
    if health and health["stopped"]:
        return "stopped", "Producer stopped", health
    from .source_health import context, recovered
    source = context(db, scope, session, health)
    if source and source["retirement_json"]:
        retirement = json.loads(source["retirement_json"])
        if retirement.get("process_exit_verified") and not retirement.get("invalidated_by_heartbeat") and retirement.get("heartbeat_at") == health.get("heartbeat_at"):
            health = health | {"retirement": retirement}
            return "stopped", "Expected probe retirement: supervisor evicted this market", health
    if not health or not -5 <= (now-timestamp(health["heartbeat_at"])).total_seconds() <= FRESH_SECONDS:
        return "unavailable", "Producer heartbeat is missing or stale", health
    errors = health["dropped"] or health["write_failures"]
    state = db.execute("SELECT occurred_at,sequence FROM telemetry_records WHERE scope_id=? AND session_id=? AND type='BOT_STATE' ORDER BY sequence DESC LIMIT 1", (scope, session)).fetchone()
    if not state or not -5 <= (now-timestamp(state[0])).total_seconds() <= FRESH_SECONDS:
        return "unavailable", "Bot-state observations are missing or stale", health
    seq = db.execute("SELECT MAX(sequence),COUNT(*) FROM telemetry_records WHERE scope_id=? AND session_id=?", (scope, session)).fetchone()
    if seq[0] != seq[1]:
        return "unavailable", "Imported producer sequence has gaps", health
    if health.get("last_sequence", 0) > (seq[0] or 0):
        return "unavailable", "Producer records have not all reached the collector", health
    if db.execute("SELECT 1 FROM telemetry_files WHERE scope_id=? AND last_error IS NOT NULL LIMIT 1", (scope,)).fetchone():
        return "unavailable", "Spool verification failed for this account", health
    if health["capped"] or (errors and not recovered(source, health, now)):
        return "unavailable", WRITER_RECOVERY_REASON, health
    if errors and state[1] <= source["baseline_sequence"]:
        return "unavailable", "Waiting for a new bot-state observation after telemetry errors", health
    if not errors and health.get("heartbeat_failures", 0):
        return "healthy", "Fresh heartbeat and contiguous bot records; historical heartbeat-file errors retained", health
    return "healthy", "Fresh telemetry recovered after a quiet period; historical error counters retained" if errors else "Recent producer and bot state", health


def collect(store, client, scope, account, *, grace=120, progress=None):
    discover(store, scope, grace)
    now = datetime.now(timezone.utc)
    monitors = [dict(r) for r in store.db.execute("SELECT * FROM bot_monitors WHERE scope_id=?", (scope,))]
    active = []
    for m in monitors:
        status, reason, health = source_status(store.db, scope, m["session_id"], now)
        if status == "healthy" or (reason == WRITER_RECOVERY_REASON and not health["capped"]):
            active.append(m)  # Keep read-only exchange capture fresh during a quiet period.
    groups = {}
    for m in active:
        groups.setdefault((m["instrument_id"], m["subaccount"]), []).append(m)
    for (ticker, sub), group in list(groups.items())[:10]:
        try:
            if len(group) != 1:
                raise ValueError("Multiple producers in one market/subaccount")
            started = utc_now()
            audit = run_import(store, client, scope, account, ticker=ticker, page_size=500, max_pages=50, collection={"bot_state_audit": True}, progress=progress)
            if audit["status"] != "traversed":
                raise ValueError("Incomplete history audit")
            order_rows = pages(client, "/portfolio/orders", "orders", {"ticker": ticker, "limit": 500}, progress=progress)
            orders = []
            for raw in order_rows:
                event = normalize(raw, "orders", scope, account, client.environment)
                if event["instrument_id"] != ticker or event["payload"].get("subaccount") is None:
                    raise ValueError("Order scope unavailable")
                if event["payload"]["subaccount"] == sub:
                    orders.append(event["payload"])
            position_rows = pages(client, "/portfolio/positions", "market_positions", {"ticker": ticker, "subaccount": sub, "limit": 100, "count_filter": "position,total_traded"}, progress=progress)
            if len(position_rows) > 1 or any(p.get("ticker") != ticker or p.get("subaccount_number", sub) != sub or p.get("position_fp") is None for p in position_rows):
                raise ValueError("Position scope unavailable")
            market = client.request("/markets/" + ticker).get("market")
            if not isinstance(market, dict) or market.get("ticker") != ticker:
                raise ValueError("Market scope unavailable")
            settled = pages(client, "/portfolio/settlements", "settlements", {"ticker": ticker, "subaccount": sub, "limit": 100}, progress=progress)
            if any(s.get("ticker") != ticker or s.get("subaccount_number", sub) != sub for s in settled):
                raise ValueError("Settlement scope unavailable")
            fills = []
            for row in store.db.execute("SELECT canonical_json FROM events WHERE scope_id=? AND instrument_id=? AND type='ORDER_FILL'", (scope, ticker)):
                p = json.loads(row[0])["payload"]
                if p.get("subaccount") is None:
                    raise ValueError("Fill scope unavailable")
                if p["subaccount"] == sub:
                    fills.append({k: p[k] for k in ("fill_id", "order_id")})
            evidence = {"complete": True, "audit_run_id": audit["run_id"], "orders": orders, "fills": fills,
                        "position": fixed(position_rows[0]["position_fp"]) if position_rows else None,
                        "market": {k: market.get(k) for k in ("ticker", "status", "market_type")},
                        "settlements": [{"settled_time": s.get("settled_time")} for s in settled]}
            with store.db:
                store.db.execute("INSERT INTO bot_exchange VALUES (?,?,?,?,?,?,?)", (uuid.uuid4().hex, scope, ticker, sub, started, utc_now(), encode(evidence)))
                store.db.execute("UPDATE bot_monitors SET last_attempt_at=?,last_error=NULL WHERE scope_id=? AND session_id=?", (utc_now(), scope, group[0]["session_id"]))
        except Exception:
            with store.db:
                for m in group:
                    store.db.execute("UPDATE bot_monitors SET last_attempt_at=?,last_error=? WHERE scope_id=? AND session_id=?", (utc_now(), "Exchange state capture incomplete or producer scope ambiguous; comparisons suspended", scope, m["session_id"]))


def evaluate(store, scope, *, grace=120, now=None):
    from .incidents import record_check
    now = now or datetime.now(timezone.utc)
    discover(store, scope, grace)
    for monitor in store.db.execute("SELECT * FROM bot_monitors WHERE scope_id=?", (scope,)).fetchall():
        session = monitor["session_id"]
        health_status, reason, health = source_status(store.db, scope, session, now)
        # Health checks have their own cadence and never clear trading discrepancies.
        health_key = "health:" + str(int(now.timestamp()) // 30)
        if not store.db.execute("SELECT 1 FROM bot_checks WHERE scope_id=? AND session_id=? AND snapshot_id=?", (scope, session, health_key)).fetchone():
            latest_exchange = store.db.execute("SELECT received_at FROM bot_exchange WHERE scope_id=? AND instrument_id=? AND subaccount=? ORDER BY received_at DESC LIMIT 1", (scope, monitor["instrument_id"], monitor["subaccount"])).fetchone()
            stale_exchange = not latest_exchange or not -5 <= (now-timestamp(latest_exchange[0])).total_seconds() <= FRESH_SECONDS
            issue = reason if health_status in {"unavailable", "stopped"} else monitor["last_error"] or ("Exchange state capture is missing or stale" if stale_exchange else reason)
            failure = health_status != "stopped" and (health_status == "unavailable" or bool(monitor["last_error"]) or stale_exchange)
            check = {"status": "unavailable" if failure else health_status, "reason": issue,
                     "rule_version": "bot-state-1", "findings": [{"rule": "source_stale", "entity": "source", "message": issue, "details": {}}] if failure else [],
                     "evaluated_rules": ["source_stale"], "health": health}
            transient = issue == WRITER_RECOVERY_REASON and not health["capped"] and not monitor["last_error"] and not stale_exchange
            health_monitor = dict(monitor)
            if transient:
                health_monitor["grace_seconds"] = max(WRITER_OPEN_GRACE_SECONDS, monitor["grace_seconds"])
            check["opening_grace_seconds"] = health_monitor["grace_seconds"]
            record_check(store, health_monitor, health_key, check, now)
        if health_status != "healthy" or monitor["last_error"]:
            continue
        snapshots = store.db.execute("SELECT * FROM bot_exchange WHERE scope_id=? AND instrument_id=? AND subaccount=? ORDER BY received_at DESC LIMIT 3", (scope, monitor["instrument_id"], monitor["subaccount"])).fetchall()
        for row in reversed(snapshots):
            if store.db.execute("SELECT 1 FROM bot_checks WHERE scope_id=? AND session_id=? AND snapshot_id=?", (scope, session, row["snapshot_id"])).fetchone():
                continue
            if (now-timestamp(row["received_at"])).total_seconds() > FRESH_SECONDS:
                continue
            def near(bound, direction, order):
                value = store.db.execute(f"SELECT canonical_json FROM telemetry_records WHERE scope_id=? AND session_id=? AND type='BOT_STATE' AND json_extract(canonical_json,'$.payload.phase')='after_step' AND occurred_at {direction} ? ORDER BY occurred_at {order},sequence {order} LIMIT 1", (scope, session, bound)).fetchone()
                return json.loads(value[0]) if value else None
            before = near((timestamp(row["started_at"])-timedelta(seconds=2)).isoformat(), "<=", "DESC")
            after = near((timestamp(row["received_at"])+timedelta(seconds=2)).isoformat(), ">=", "ASC")
            if not after:
                continue
            exchange = dict(row); exchange["evidence"] = json.loads(exchange.pop("evidence_json"))
            result = compare(before, after, exchange)
            record_check(store, monitor, row["snapshot_id"], result, now)
