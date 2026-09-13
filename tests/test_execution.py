"""Isolated execution evidence; never contacts an exchange."""
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from othryss.execution import catalog, classify_fill, evaluate, summary
from othryss.history_reader import HistoryReader
from othryss.storage import Store

BASE = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def trace(operation="cancel", code=200, status="canceled", retry=False, session="a" * 32):
    common = {"request_id": "request-1", "operation": operation, "order_id": "order-1"}
    specs = [("ORDER_INTENT", 0, {}), ("ORDER_REQUEST", .001, {}),
             ("HTTP_ATTEMPT", .002, {"attempt": 1}),
             ("HTTP_RESPONSE", .033, {"attempt": 1, "duration_ns": "31000000", "http_status": 429 if retry else code})]
    if retry:
        specs.extend([("HTTP_ATTEMPT", .09, {"attempt": 2}), ("HTTP_RESPONSE", .19, {"attempt": 2, "duration_ns": "100000000", "http_status": code})])
    specs.append(("ORDER_RESPONSE", .2, {"duration_ns": "200000000", "attempts": 2 if retry else 1,
                  "http_status": code, "outcome": "unknown" if code is None else "http_success" if 200 <= code < 300 else "http_error", "status": status}))
    return [{"schema_version": "0.3.0", "producer": "test", "account": "test", "environment": "demo", "workspace": "local",
             "session_id": session, "event_id": f"{session}:{i}", "sequence": i, "instrument_id": "TEST-MARKET", "subaccount": 0,
             "strategy_id": "incentives", "run_id": "run-1", "occurred_at": at(seconds),
             "monotonic_ns": str(1000000000 + round(seconds * 1e9)), "type": kind, "payload": common | payload}
            for i, (kind, seconds, payload) in enumerate(specs, 1)]


def fill(seconds=3, received=10, event_id="fill-1", subaccount=0, market="TEST-MARKET", order="order-1"):
    return {"event_id": event_id, "type": "ORDER_FILL", "instrument_id": market,
            "occurred_at": at(seconds), "received_at": at(received), "payload": {"order_id": order, "subaccount": subaccount,
            "fill_id": event_id, "quantity": "1", "price_usd": "0.5", "fee_usd": "0", "price_basis": "yes_outcome",
            "currency": "USD", "quantity_unit": "contracts", "exposure_direction": "increase_yes", "liquidity": "maker"}}


def insert_trace(store, scope, records):
    for r in records:
        store.db.execute("INSERT INTO telemetry_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (scope, r["event_id"], r["session_id"], r["sequence"], r["payload"].get("request_id"),
                          r["instrument_id"], r["subaccount"], r["occurred_at"], at(20), r["type"], json.dumps(r)))


def insert_fill(store, scope, event):
    event = dict(event, scope_id=scope)
    store.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (event["event_id"], scope, event["type"],
                     event["instrument_id"], event["payload"]["order_id"], event["occurred_at"], event["received_at"], json.dumps(event)))


def seed(path):
    with Store(path) as store:
        scope = store.bind_account("local", "kalshi", "demo", "test", "test")
        other = store.bind_account("local", "kalshi", "demo", "empty", "test")
        insert_trace(store, scope, trace(retry=True))
        insert_trace(store, scope, trace("submit", session="b" * 32))
        insert_trace(store, scope, trace(code=None, status=None, session="c" * 32))
        insert_fill(store, scope, fill())
        insert_fill(store, scope, fill(-5, event_id="fill-earlier"))
        store.db.commit()
    return scope, other


class ExecutionTests(unittest.TestCase):
    def test_retry_elapsed_is_separate_from_transport_sum(self):
        r = evaluate(trace(retry=True))
        self.assertEqual((r["elapsed_ms"], r["transport_ms"], r["retries"]), ("200", "131", 1))
        self.assertEqual([a["elapsed_ms"] for a in r["attempts"]], ["31", "100"])
        self.assertTrue(r["wall_clock_valid"])

    def test_timeout_measured_but_outcome_unknown(self):
        r = evaluate(trace(code=None, status=None))
        self.assertEqual((r["timing_status"], r["outcome"], r["cancel_confirmation"]), ("measured", "unknown", "unconfirmed"))
        self.assertEqual(classify_fill(r, fill()), "after_unconfirmed_cancel")

    def test_http_success_and_404_do_not_confirm_cancel(self):
        for code, status in [(200, None), (404, "canceled"), (200, "executed")]:
            r = evaluate(trace(code=code, status=status))
            self.assertEqual(r["cancel_confirmation"], "unconfirmed")
            self.assertEqual(classify_fill(r, fill()), "after_unconfirmed_cancel")

    def test_terminal_response_must_identify_target(self):
        records = trace()
        records[-1]["payload"]["order_id"] = "different-order"
        self.assertEqual(evaluate(records)["cancel_confirmation"], "unconfirmed")

    def test_earlier_fill_imported_late_is_not_stale(self):
        r = evaluate(trace())
        self.assertEqual(classify_fill(r, fill(-5)), "late_observation_only")
        self.assertEqual(classify_fill(r, fill(-5, -4)), "before_request")

    def test_guard_in_flight_and_suspected_classifications(self):
        r = evaluate(trace())
        self.assertEqual(classify_fill(r, fill(.5)), "timing_uncertain")
        self.assertEqual(classify_fill(r, fill(1.1)), "cancel_in_flight_or_clock_uncertain")
        self.assertEqual(classify_fill(r, fill(3)), "suspected_after_cancel_response")

    def test_clock_jump_preserves_monotonic_latency_but_blocks_fill_timing(self):
        records = trace()
        records[-1]["occurred_at"] = at(5)
        r = evaluate(records)
        self.assertEqual(r["timing_status"], "measured")
        self.assertFalse(r["wall_clock_valid"])
        self.assertEqual(classify_fill(r, fill(10)), "timing_unavailable")

    def test_incomplete_and_dropped_evidence_not_zero_latency(self):
        for records, kwargs in [(trace()[:-1], {}), (trace(), {"complete_span": False}),
                                (trace(), {"truncated": True}), (trace()[:2] + trace()[3:], {})]:
            r = evaluate(records, **kwargs)
            self.assertEqual((r["timing_status"], r["elapsed_ms"]), ("unavailable", None))
            self.assertEqual(classify_fill(r, fill()), "timing_unavailable")

    def test_identity_mismatch_is_rejected(self):
        for key, value in [("subaccount", 1), ("instrument_id", "OTHER"), ("session_id", "other"), ("run_id", "other")]:
            records = trace()
            records[2][key] = value
            self.assertEqual(evaluate(records)["reason"], "request_identity_mismatch")

    def test_attempt_order_and_invalid_duration_rejected(self):
        records = trace(retry=True)
        records[4]["payload"]["attempt"] = 1
        self.assertEqual(evaluate(records)["reason"], "invalid_attempt_order")
        for value in ["-1", "NaN", True, "1.5", "1" * 100]:
            records = trace()
            records[-1]["payload"]["duration_ns"] = value
            self.assertEqual(evaluate(records)["timing_status"], "unavailable")
        records = trace()
        records[-1]["payload"]["duration_ns"] = "1"
        self.assertEqual(evaluate(records)["reason"], "inconsistent_durations")

    def test_percentiles_split_outcomes_and_deduplicate_fills(self):
        r = evaluate(trace())
        r["fills"] = [{"classification": "suspected_after_cancel_response", "event": fill()}]
        self.assertIsNone(summary([r] * 19)["latency"][0]["p95_ms"])
        result = summary([r] * 20 + [evaluate(trace(code=None))])
        self.assertEqual(result["latency"][0]["p95_ms"], "200")
        self.assertEqual(len(result["latency"]), 2)
        self.assertEqual(result["fill_classifications"]["suspected_after_cancel_response"], 1)


class ExecutionCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite"
        self.scope, self.other = seed(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def read(self, **kwargs):
        with HistoryReader(self.path) as reader:
            return catalog(reader, kwargs.pop("scope", self.scope), **kwargs)

    def test_pagination_filters_and_scope(self):
        page = self.read(limit=1)
        self.assertTrue(page["has_more"])
        self.assertNotEqual(page["rows"][0]["session_id"], self.read(limit=1, offset=1)["rows"][0]["session_id"])
        self.assertEqual(len(self.read(operation="cancel")["rows"]), 2)
        self.assertEqual(self.read(scope=self.other)["rows"], [])
        self.assertEqual(self.read(instrument="MISSING")["rows"], [])
        for kwargs in [{"scope": "missing"}, {"operation": "POST"}, {"limit": 51}, {"offset": -1}]:
            with self.assertRaises(ValueError): self.read(**kwargs)

    def test_fill_links_require_scope_market_subaccount_and_order(self):
        with Store(self.path) as store:
            for i, kwargs in enumerate([{"subaccount": 1}, {"subaccount": None}, {"market": "OTHER"}, {"order": "OTHER"}]):
                insert_fill(store, self.scope, fill(event_id=f"wrong-{i}", **kwargs))
            insert_fill(store, self.other, fill(event_id="wrong-scope"))
            store.db.commit()
        for r in self.read(operation="cancel")["rows"]:
            self.assertEqual({f["event"]["event_id"] for f in r["fills"]}, {"fill-1", "fill-earlier"})
        self.assertEqual(self.read(operation="submit")["rows"][0]["fills"], [])

    def test_interleaved_bot_state_is_not_a_gap(self):
        records = trace(session="d" * 32)
        for r in records[2:]:
            r["sequence"] += 1
            r["event_id"] = f'{r["session_id"]}:{r["sequence"]}'
        state = copy.deepcopy(records[1])
        state.update(sequence=3, event_id=f'{state["session_id"]}:3', type="BOT_STATE", payload={})
        with Store(self.path) as store:
            insert_trace(store, self.scope, records + [state]); store.db.commit()
        row = next(r for r in self.read()["rows"] if r["session_id"] == "d" * 32)
        self.assertEqual(row["timing_status"], "measured")
        with Store(self.path) as store:
            store.db.execute("DELETE FROM telemetry_records WHERE session_id=? AND sequence=3", ("d" * 32,)); store.db.commit()
        row = next(r for r in self.read()["rows"] if r["session_id"] == "d" * 32)
        self.assertEqual(row["reason"], "incomplete_trace")

    def test_fill_cap_explicit_and_latest_first(self):
        with Store(self.path) as store:
            for i in range(101): insert_fill(store, self.scope, fill(i + 100, event_id=f"extra-{i}"))
            store.db.commit()
        row = self.read(operation="cancel")["rows"][0]
        self.assertTrue(row["fills_truncated"])
        self.assertEqual(len(row["fills"]), 100)
        self.assertEqual(row["fills"][0]["event"]["event_id"], "extra-100")


if __name__ == "__main__":
    unittest.main()
