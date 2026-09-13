"""Read-only explorer isolation, pagination, and exact whole-order economics."""

import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

from othryss.history_reader import HistoryReader
from othryss.server import Handler
from othryss.storage import Store


class HistoryReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite"
        with Store(self.path) as store:
            self.scope = store.bind_account("local", "kalshi", "demo", "test", "private-fingerprint")
            self.other = store.bind_account("local", "kalshi", "saved-report", "saved", "another-fingerprint")
            self.add(store, self.scope, "A", 125)
            self.add(store, self.scope, "B", 1)
            self.add(store, self.other, "A", 1)

    def tearDown(self):
        self.temp.cleanup()

    def add(self, store, scope, market, fills):
        run = store.create_import(scope, {"ticker": None}, {}, ["/portfolio/fills"])
        records = [{"event_id": f"{scope}-{market}-{i}", "type": "ORDER_FILL", "venue": "kalshi", "instrument_id": market,
                    "occurred_at": "2026-08-18T14:16:52.776127+00:00", "payload": {
                        "order_id": "shared-order", "fill_id": f"fill-{i}", "exposure_direction": "increase_yes",
                        "price_basis": "yes_outcome", "quantity": "0.01", "price_usd": "0.06", "fee_usd": "0.0001", "liquidity": "maker"}}
                   for i in range(fills)]
        records.append({"event_id": f"{scope}-{market}-snapshot", "type": "ORDER_OBSERVATION", "venue": "kalshi", "instrument_id": market,
                        "occurred_at": None, "payload": {"order_id": "shared-order", "status": "canceled", "initial_quantity": "10", "filled_quantity": "9", "remaining_quantity": "1"}})
        store.commit_page(run, "/portfolio/fills", store.checkpoint(run, "/portfolio/fills"), records, {}, "", "2026-09-09T00:00:00+00:00")
        store.status(run, "traversed")

    def test_accounts_exclude_credentials_and_distinguish_sources(self):
        with HistoryReader(self.path) as reader:
            accounts = reader.accounts()["accounts"]
        self.assertEqual([a["environment"] for a in accounts], ["demo", "saved-report"])
        self.assertEqual(accounts[0]["order_count"], 2)
        self.assertEqual(accounts[0]["event_counts"]["ORDER_FILL"], 126)
        self.assertNotIn("fingerprint", json.dumps(accounts))
        self.assertEqual(accounts[0]["latest_full_traversal"]["status"], "traversed")

    def test_order_list_pages_are_disjoint_and_search_is_literal(self):
        with HistoryReader(self.path) as reader:
            first = reader.orders(self.scope, limit=1)
            second = reader.orders(self.scope, limit=1, offset=1)
            self.assertEqual(first["total"], 2)
            self.assertNotEqual(first["orders"][0]["instrument_id"], second["orders"][0]["instrument_id"])
            self.assertEqual(reader.orders(self.scope, "shared-order", filled=True)["total"], 2)
            self.assertEqual(reader.orders(self.scope, "%' OR 1=1 --")["total"], 0)
            self.assertEqual(reader.orders(self.other)["total"], 1)

    def add_state(self, store, order, status, at, identity, scope=None):
        scope=scope or self.scope
        run=store.create_import(scope, {}, {}, ["/portfolio/orders"])
        record={"event_id":identity,"type":"ORDER_OBSERVATION","venue":"kalshi","instrument_id":"C",
                "occurred_at":at,"payload":{"order_id":order,"status":status}}
        store.commit_page(run,"/portfolio/orders",store.checkpoint(run,"/portfolio/orders"),[record],{},"",at)
        store.status(run,"traversed")

    def test_groups_use_latest_snapshot_and_keep_unknown_separate(self):
        with Store(self.path) as store:
            self.add_state(store,"moving","resting","2026-09-10T00:00:00Z","old")
            self.add_state(store,"moving","canceled","2026-09-11T00:00:00Z","new")
            self.add_state(store,"active","resting","2026-09-11T00:00:00Z","active")
            self.add_state(store,"unknown","unrecognized","2026-09-11T00:00:00Z","unknown")
        with HistoryReader(self.path) as reader:
            d=reader.order_groups(self.scope)
            self.assertEqual([o["order_id"] for o in d["groups"]["active"]["orders"]],["active"])
            self.assertEqual(d["groups"]["inactive"]["total"],3)
            self.assertEqual(d["groups"]["unknown"]["total"],1)
            self.assertEqual(reader.order_groups(self.other)["groups"]["inactive"]["total"],1)
            self.assertEqual(reader.order_groups(self.scope,filled=True)["groups"]["inactive"]["total"],2)
            self.assertTrue(all(g["total"]==0 for g in reader.order_groups(self.scope,"%' OR 1=1 --")["groups"].values()))

    def test_expanded_pages_pin_the_snapshot_and_do_not_repeat_orders(self):
        with Store(self.path) as store:
            for i in range(35):
                self.add_state(store,f"archive-{i:02}","executed","2026-09-11T00:00:00Z",f"archive-{i}")
        with HistoryReader(self.path) as reader:
            first=reader.order_groups(self.scope)
        self.assertEqual(len(first["groups"]["inactive"]["orders"]),5)
        with Store(self.path) as store:
            self.add_state(store,"new-order","canceled","2026-09-12T00:00:00Z","new-order")
            self.add_state(store,"archive-00","resting","2026-09-12T00:00:00Z","changed")
        with HistoryReader(self.path) as reader:
            more=reader.order_groups(self.scope,group="inactive",offset=5,through=first["through"])
            current=reader.order_groups(self.scope)
            with self.assertRaises(ValueError):reader.order_groups(self.scope,group="invalid")
            with self.assertRaises(ValueError):reader.order_groups(self.scope,through=-1)
        a={(o["instrument_id"],o["order_id"]) for o in first["groups"]["inactive"]["orders"]}
        b={(o["instrument_id"],o["order_id"]) for o in more["groups"]["inactive"]["orders"]}
        self.assertFalse(a & b)
        self.assertEqual(len(b),25)
        self.assertEqual(more["groups"]["inactive"]["total"],37)
        self.assertNotIn(("C","new-order"),b)
        self.assertEqual(current["groups"]["active"]["total"],1)

    def test_event_pages_preserve_whole_order_decimals_and_null_source_time(self):
        with HistoryReader(self.path) as reader:
            first = reader.order(self.scope, "A", "shared-order", limit=100)
            second = reader.order(self.scope, "A", "shared-order", limit=100, offset=100)
        self.assertEqual(first["total"], 126)
        self.assertEqual(first["fill_count"], 125)
        self.assertEqual(first["totals"]["volume"], "1.25")
        self.assertEqual(first["totals"]["fees"], "0.0125")
        self.assertEqual(first["totals"]["net_cash_flow"], "-0.0875")
        self.assertEqual(first["totals"], second["totals"])
        self.assertEqual(len({e["event_id"] for e in first["events"] + second["events"]}), 126)
        self.assertIsNone(first["latest_observation"]["occurred_at"])
        self.assertEqual(first["latest_observation"]["payload"]["filled_quantity"], "9")
        self.assertEqual(first["events"][0]["evidence"][0]["stream"], "/portfolio/fills")
        self.assertNotIn("evidence_json", json.dumps(first))

    def test_selected_order_does_not_merge_instruments_or_accounts(self):
        with HistoryReader(self.path) as reader:
            self.assertEqual(reader.order(self.scope, "B", "shared-order")["fill_count"], 1)
            self.assertEqual(reader.order(self.other, "A", "shared-order")["fill_count"], 1)
            with self.assertRaises(ValueError):
                reader.order(self.other, "B", "shared-order")
            with self.assertRaises(ValueError):
                reader.orders("unknown")

    def test_reader_cannot_write_or_create_database(self):
        with HistoryReader(self.path) as reader:
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("DELETE FROM events")
        absent = Path(self.temp.name) / "absent.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            HistoryReader(absent)
        self.assertFalse(absent.exists())

    def test_invalid_pagination_rejected(self):
        with HistoryReader(self.path) as reader:
            for limit, offset in [(0, 0), (101, 0), (25, -1)]:
                with self.assertRaises(ValueError):
                    reader.orders(self.scope, limit=limit, offset=offset)

    def test_http_empty_history_invalid_inputs_and_file_allowlist(self):
        handler = type("TestHandler", (Handler,), {"history_db": Path(self.temp.name) / "absent.sqlite", "log_message": lambda *_: None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/api/history/accounts") as response:
                self.assertEqual(json.load(response)["accounts"], [])
            handler.history_db = self.path
            for path, status in [("/api/history/orders?limit=no", 400), ("/api/history/order?scope=unknown", 400), ("/local.env", 404), ("/artifacts/history/othryss.sqlite", 404)]:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base + path)
                self.assertEqual(caught.exception.code, status)
            with urlopen(base + "/api/history/orders?scope=" + self.scope) as response:
                self.assertEqual(json.load(response)["total"], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
