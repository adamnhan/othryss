import json
import unittest
from copy import deepcopy

from othryss.fixture import DEFAULT_FIXTURE, build_explorer
from othryss.replay import ReplayStore


class ReplayAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.records = json.loads(DEFAULT_FIXTURE.read_text())["events"]

    def test_synthetic_partial_fill_and_exit_have_exact_economics(self):
        result = build_explorer()
        self.assertEqual(result["fill_count"], 2)
        self.assertEqual(result["totals"], {
            "volume": "20", "net_quantity": "0", "fees": "0.10",
            "gross_cash_flow": "0.00", "net_cash_flow": "-0.10",
        })
        entry, exit_order = result["orders"]
        self.assertEqual(entry["first_reported_size"], "20")
        self.assertEqual(entry["last_reported_remaining"], "10")
        self.assertEqual(entry["filled_quantity"], "10")
        self.assertIsNone(exit_order["last_reported_remaining"])

    def test_repeated_and_out_of_order_replay_do_not_inflate_fills(self):
        once = ReplayStore()
        once.ingest("run", self.records)
        replay = ReplayStore()
        replay.ingest("run", list(reversed(self.records)))
        replay.ingest("run", self.records)
        actual = replay.project()
        self.assertEqual(actual["events"], once.project()["events"])
        self.assertEqual(actual["totals"], once.project()["totals"])
        self.assertEqual(actual["duplicates_skipped"], 5)
        self.assertEqual(actual["unique_events"], 5)

    def test_same_execution_under_new_transport_id_is_deduplicated(self):
        store = ReplayStore()
        store.ingest("run", self.records)
        duplicate = deepcopy(next(e for e in self.records if e["type"] == "ORDER_FILL"))
        duplicate["event_id"] = "different-transport-id"
        duplicate["source_pointer"] = "/another-delivery"
        store.ingest("run", [duplicate])
        self.assertEqual(store.project()["fill_count"], 2)
        self.assertEqual(store.project()["duplicates_skipped"], 1)

    def test_aliases_from_different_runs_do_not_collide(self):
        store = ReplayStore()
        store.ingest("run-a", self.records)
        store.ingest("run-b", self.records)
        self.assertEqual(store.project()["fill_count"], 4)
        self.assertEqual(len(store.project()["orders"]), 4)

    def test_conflicting_batch_is_rejected_without_partial_commit(self):
        store = ReplayStore()
        store.ingest("run", self.records)
        before = store.project()
        new = deepcopy(self.records[0])
        new["event_id"] = "new-observation"
        conflict = deepcopy(next(e for e in self.records if e["type"] == "ORDER_FILL"))
        conflict["event_id"] = "new-transport-id"
        conflict["payload"]["quantity"] = "1.00"
        with self.assertRaisesRegex(ValueError, "fill identity"):
            store.ingest("run", [new, conflict])
        self.assertEqual(store.project(), before)

    def test_lagging_snapshot_and_final_cancel_remain_observations(self):
        result = build_explorer()
        last = result["events"][-1]
        self.assertEqual(last["payload"]["reason"], "final_cancel")
        self.assertEqual(last["payload"]["signed_position"], "10")
        self.assertEqual(last["type"], "BOT_STATE_OBSERVATION")
        self.assertTrue(all(o["exchange_terminal_state"] == "unknown" for o in result["orders"]))
        self.assertEqual(result["reconciliation"]["status"], "unavailable")
        self.assertIsNone(result["reconciliation"]["confirmed_incidents"])
        self.assertTrue(all(e["received_at"] is None for e in result["events"]))
        self.assertEqual(result["fixture"]["reported_final_position"], "0")

    def test_nan_and_negative_quantities_are_rejected(self):
        for value in ("NaN", "Infinity", "-1", "0"):
            with self.subTest(value=value):
                record = deepcopy(next(e for e in self.records if e["type"] == "ORDER_FILL"))
                record["payload"]["quantity"] = value
                with self.assertRaises(ValueError):
                    ReplayStore().ingest("run", [record])

    def test_expected_fixture_totals_do_not_feed_analytics(self):
        # The engine consumes events only; fixture expected totals are not inputs.
        store = ReplayStore()
        records = deepcopy(self.records)
        fill = next(e for e in records if e["type"] == "ORDER_FILL")
        fill["payload"]["fee_usd"] = "0.000001"
        store.ingest("run", records)
        self.assertEqual(store.project()["totals"]["fees"], "0.100001")


if __name__ == "__main__":
    unittest.main()
