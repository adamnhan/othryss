"""Synthetic API pages exercise recovery; fill economics match the discovery case."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from othryss.history import STREAMS, run_import
from othryss.kalshi import normalize
from othryss.replay import ReplayStore
from othryss.storage import Store

CUTOFF = {"orders_updated_ts": "2026-06-01T00:00:00Z", "trades_created_ts": "2026-06-01T00:00:00Z"}
BUY = {"order_id": "entry", "fill_id": "fill-buy", "ticker": "TEST-MARKET", "book_side": "bid",
       "outcome_side": "yes", "count_fp": "49.32", "yes_price_dollars": "0.0600",
       "fee_cost": "0.000000", "is_taker": False, "subaccount_number": 0,
       "created_time": "2026-08-18T14:16:52.776127Z"}
SELL = {**BUY, "order_id": "exit", "fill_id": "fill-sell", "book_side": "ask", "outcome_side": "no",
        "is_taker": True, "fee_cost": "0.194800", "created_time": "2026-08-18T14:16:53.898811Z"}
ORDER = {"order_id": "entry", "ticker": "TEST-MARKET", "book_side": "bid", "status": "canceled",
         "yes_price_dollars": "0.0600", "initial_count_fp": "158.00", "fill_count_fp": "49.32",
         "remaining_count_fp": "108.68", "subaccount_number": 0,
         "created_time": "2026-08-18T14:16:17.000000Z", "last_update_time": "2026-08-18T14:16:54Z"}


class FakeClient:
    environment = "demo"
    credential_subaccount = None

    def __init__(self):
        self.calls = []
        self.cutoff = deepcopy(CUTOFF)
        self.pages = {
            ("/portfolio/orders", ""): {"orders": [deepcopy(ORDER)], "cursor": ""},
            ("/portfolio/fills", ""): {"fills": [deepcopy(BUY)], "cursor": "next"},
            ("/portfolio/fills", "next"): {"fills": [deepcopy(SELL)], "cursor": ""},
            ("/historical/orders", ""): {"orders": [deepcopy(ORDER)], "cursor": ""},
            ("/historical/fills", ""): {"fills": [deepcopy(BUY), deepcopy(SELL)], "cursor": ""},
        }

    def request(self, path, params=None):
        params = params or {}
        self.calls.append((path, deepcopy(params)))
        if path == "/historical/cutoff":
            return deepcopy(self.cutoff)
        value = self.pages[(path, params.get("cursor", ""))]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite"
        self.store = Store(self.path)
        self.scope = self.store.bind_account("local", "kalshi", "demo", "test", "test-key")
        self.client = FakeClient()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_import(self, **kwargs):
        return run_import(self.store, self.client, self.scope, "test", **kwargs)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def test_all_tiers_pagination_and_repeated_imports_are_durable(self):
        first = self.run_import()
        self.assertEqual(first["status"], "traversed")
        self.assertEqual(first["stored_events_in_scope"], 3)
        self.assertEqual(sum(s["pages"] for s in first["streams"]), 5)
        self.reopen()
        second = self.run_import()
        self.assertEqual(second["stored_events_in_scope"], 3)
        self.assertEqual(sum(s["inserted"] for s in second["streams"]), 0)
        self.assertEqual(sum(s["duplicates"] for s in second["streams"]), 6)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 10)
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        engine = ReplayStore()
        engine.ingest(self.scope, [e for e in self.store.events(self.scope) if e["type"] == "ORDER_FILL"])
        self.assertEqual(engine.project()["totals"]["fees"], "0.1948")
        self.assertEqual(engine.project()["totals"]["net_quantity"], "0.00")

    def test_durable_order_observations_replay_without_double_counting(self):
        self.run_import()
        engine = ReplayStore()
        engine.ingest(self.scope, list(self.store.events(self.scope)))
        projection = engine.project()
        self.assertEqual(projection["fill_count"], 2)
        self.assertEqual(projection["totals"]["volume"], "98.64")
        entry = next(order for order in projection["orders"] if order["order_id"] == "entry")
        self.assertEqual(entry["last_exchange_observation"]["remaining_quantity"], "108.68")
        self.assertIsNone(entry["first_observed_at"])  # No bot snapshot or ACK was invented.
        self.assertIsNone(entry["last_reported_remaining"])
        self.assertEqual(projection["reconciliation"]["status"], "unavailable")

    def test_page_budget_and_process_restart_resume_saved_cursor(self):
        first = self.run_import(max_pages=2)
        self.assertEqual(first["status"], "paused")
        self.assertEqual(self.store.checkpoint(first["run_id"], "/portfolio/fills")["cursor"], "next")
        self.reopen()
        self.client.calls.clear()
        result = self.run_import(resume=first["run_id"])
        self.assertEqual(result["status"], "traversed")
        self.assertEqual(self.client.calls[0], ("/portfolio/fills", {"limit": 100, "cursor": "next"}))
        self.assertEqual(result["stored_events_in_scope"], 3)

    def test_failed_request_retains_successful_page_and_can_resume(self):
        self.client.pages[("/portfolio/fills", "next")] = RuntimeError("simulated connection loss")
        with self.assertRaisesRegex(RuntimeError, "connection loss"):
            self.run_import()
        run_id = self.store.db.execute("SELECT run_id FROM imports").fetchone()[0]
        self.assertEqual(self.store.report(run_id)["status"], "failed")
        self.reopen()
        self.client.pages[("/portfolio/fills", "next")] = {"fills": [deepcopy(SELL)], "cursor": ""}
        self.assertEqual(self.run_import(resume=run_id)["stored_events_in_scope"], 3)

    def test_missing_cursor_is_not_silently_end_of_history(self):
        self.client.pages[("/portfolio/orders", "")] = {"orders": [deepcopy(ORDER)]}
        with self.assertRaisesRegex(ValueError, "explicit pagination"):
            self.run_import()
        run_id = self.store.db.execute("SELECT run_id FROM imports").fetchone()[0]
        self.assertEqual(self.store.checkpoint(run_id, "/portfolio/orders")["done"], 0)
        self.assertEqual(self.store.report(run_id)["stored_events_in_scope"], 0)

    def test_cursor_cycle_is_detected_before_advancing(self):
        self.client.pages[("/portfolio/fills", "next")]["cursor"] = "next"
        with self.assertRaisesRegex(ValueError, "cursor cycle"):
            self.run_import()
        run_id = self.store.db.execute("SELECT run_id FROM imports").fetchone()[0]
        self.assertEqual(self.store.checkpoint(run_id, "/portfolio/fills")["cursor"], "next")

    def test_conflicting_fill_rolls_back_entire_page(self):
        conflict = {**BUY, "count_fp": "1.00"}
        self.client.pages[("/portfolio/fills", "next")] = {"fills": [deepcopy(SELL), conflict], "cursor": ""}
        with self.assertRaisesRegex(ValueError, "Conflicting immutable"):
            self.run_import()
        run_id = self.store.db.execute("SELECT run_id FROM imports").fetchone()[0]
        self.assertEqual(self.store.report(run_id)["stored_events_in_scope"], 2)
        self.assertEqual(self.store.checkpoint(run_id, "/portfolio/fills")["pages"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 2)

    def test_different_order_states_are_preserved_as_observations(self):
        self.client.pages[("/historical/orders", "")]["orders"] = [{**ORDER, "status": "resting", "last_update_time": "2026-08-18T14:16:18Z"}]
        self.run_import()
        orders = [e for e in self.store.events(self.scope) if e["type"] == "ORDER_OBSERVATION"]
        self.assertEqual([o["payload"]["status"] for o in orders], ["resting", "canceled"])
        self.assertEqual(len(orders), 2)

    def test_cutoff_change_requires_another_scan(self):
        def progress(message):
            if message.get("stream") == "/historical/fills":
                self.client.cutoff["trades_created_ts"] = "2026-06-02T00:00:00Z"
        result = self.run_import(progress=progress)
        self.assertEqual(result["status"], "needs_rescan")
        self.assertNotEqual(result["cutoff_before"], result["cutoff_after"])

    def test_unknown_response_fields_are_not_persisted(self):
        self.client.pages[("/portfolio/orders", "")]["orders"][0]["private_key"] = "SECRET-SENTINEL"
        self.run_import()
        texts = [r[0] for r in self.store.db.execute("SELECT evidence_json FROM pages")]
        self.assertNotIn("SECRET-SENTINEL", "".join(texts))
        self.assertIn("initial_count_fp", "".join(texts))

    def test_scope_rebinding_and_cross_account_resume_are_rejected(self):
        result = self.run_import(max_pages=1)
        with self.assertRaisesRegex(ValueError, "different credential"):
            self.store.bind_account("local", "kalshi", "demo", "test", "other-key")
        other = self.store.bind_account("local", "kalshi", "demo", "other", "other-key")
        with self.assertRaisesRegex(ValueError, "account does not match"):
            run_import(self.store, self.client, other, "other", resume=result["run_id"])

    def test_missing_order_update_time_is_not_fabricated(self):
        row = deepcopy(ORDER)
        del row["last_update_time"]
        event = normalize(row, "orders", self.scope, "test", "demo")
        self.assertIsNone(event["occurred_at"])
        self.assertEqual(event["type"], "ORDER_OBSERVATION")
        event["received_at"] = "2026-09-09T00:00:00Z"
        engine = ReplayStore()
        engine.ingest(self.scope, [event])
        self.assertIsNone(engine.project()["orders"][0]["last_exchange_observation"]["source_updated_at"])

    def test_new_direction_fields_override_ambiguous_legacy_side(self):
        result = normalize({**SELL, "side": "no", "action": "sell"}, "fills", self.scope, "test", "demo")
        self.assertEqual(result["payload"]["exposure_direction"], "decrease_yes")
        legacy = deepcopy(SELL)
        del legacy["book_side"], legacy["outcome_side"]
        legacy.update(side="yes", action="sell")
        self.assertEqual(normalize(legacy, "fills", self.scope, "test", "demo")["payload"]["exposure_direction"], "decrease_yes")
        with self.assertRaisesRegex(ValueError, "Conflicting explicit"):
            normalize({**SELL, "outcome_side": "yes"}, "fills", self.scope, "test", "demo")

    def test_missing_fee_does_not_become_zero(self):
        row = deepcopy(BUY)
        del row["fee_cost"]
        with self.assertRaisesRegex(ValueError, "fee is unavailable"):
            normalize(row, "fills", self.scope, "test", "demo")


if __name__ == "__main__":
    unittest.main()
