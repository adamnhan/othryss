"""Order investigation linkage, bounded evidence and read-only API acceptance."""
import copy
import json
import tempfile
import threading
import unittest
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from othryss.history_reader import HistoryReader
from othryss.incidents import record_check
from othryss.order_detail import LIMITS, investigate
from othryss.reference import ReferenceStore
from othryss.server import Handler
from othryss.storage import Store
from tests.test_execution import BASE, at, fill, insert_fill, insert_trace, seed as execution_seed, trace
from tests.test_markouts import quotes


def incident(store, scope, rule, entity, *, session="a" * 32, subaccount=0, market="TEST-MARKET", seconds=0):
    monitor = {"scope_id": scope, "session_id": session, "instrument_id": market, "subaccount": subaccount, "grace_seconds": 1}
    if rule != "inventory_limit":
        store.db.execute("INSERT OR IGNORE INTO bot_monitors(scope_id,session_id,instrument_id,subaccount,grace_seconds) VALUES (?,?,?,?,?)",
                         (scope, session, market, subaccount, 1))
    result = {"status": "difference", "reason": "Synthetic acceptance evidence", "findings": [
        {"rule": rule, "entity": entity, "message": "Review needed", "details": {}}], "evaluated_rules": [rule]}
    for n in (0, 2):
        record_check(store, monitor, f"capture:{session}:{rule}:{entity}:{n}", result, BASE + timedelta(seconds=seconds+n))


def seed(history, refs):
    scope, other = execution_seed(history)
    with Store(history) as store:
        observation = fill(event_id="snapshot")
        observation.update(type="ORDER_OBSERVATION")
        observation["payload"] = {"order_id": "order-1", "subaccount": 0, "client_order_id": "client-1",
                                  "status": "canceled", "initial_quantity": "1", "filled_quantity": "1", "remaining_quantity": "0"}
        insert_fill(store, scope, observation)
        incident(store, scope, "remaining_mismatch", "order-1")
        incident(store, scope, "position_mismatch", "position")
        incident(store, scope, "missing_local_fill", "fill-1")
        incident(store, scope, "remaining_mismatch", "other-order", session="d" * 32)
        incident(store, scope, "position_mismatch", "position", session="e" * 32, subaccount=1)
        incident(store, scope, "source_stale", "source", session="f" * 32, seconds=1000)
        incident(store, other, "remaining_mismatch", "order-1")
        store.db.commit()
    reference = ReferenceStore(refs)
    try:
        reference.start(scope, "acceptance"); reference.watch(scope, {"TEST-MARKET": {}})
        for q in quotes(scope, BASE + timedelta(seconds=3)):
            q["instrument_id"] = "TEST-MARKET"
            reference.append(scope, "connection", q)
    finally:
        reference.db.close()
    return scope, other


class OrderInvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.history = Path(self.temp.name) / "history.sqlite"
        self.refs = Path(self.temp.name) / "quotes.sqlite"
        self.scope, self.other = seed(self.history, self.refs)

    def tearDown(self):
        self.temp.cleanup()

    def read(self, **kwargs):
        with HistoryReader(self.history) as reader:
            return investigate(reader, kwargs.pop("reference_path", self.refs), kwargs.pop("scope", self.scope),
                               kwargs.pop("instrument", "TEST-MARKET"), kwargs.pop("order", "order-1"), now=BASE+timedelta(seconds=100), **kwargs)

    def test_combines_exact_evidence_without_changing_analyses(self):
        data = self.read()
        self.assertEqual((data["total"], data["fill_count"], len(data["requests"]["rows"])), (3, 2, 3))
        self.assertEqual(data["identity"]["subaccount"], 0)
        retry = next(r for r in data["requests"]["rows"] if r["session_id"] == "a" * 32)
        self.assertEqual((retry["elapsed_ms"], retry["transport_ms"], retry["retries"]), ("200", "131", 1))
        self.assertEqual({f["classification"] for f in retry["fills"]}, {"suspected_after_cancel_response", "late_observation_only"})
        fill_marks = next(r for r in data["markouts"]["rows"] if r["fill"]["event_id"] == "fill-1")
        self.assertTrue(all(m["status"] == "estimate" and m["per_contract_usd"] == "0" for m in fill_marks["markouts"]))
        self.assertIn("interval_digest", fill_marks["markouts"][0]["evidence"])
        self.assertEqual({e["source"] for e in data["timeline"]}, {"exchange", "bot", "reference", "incident"})
        self.assertIn("not causal", data["timeline_coverage"])

    def test_incidents_direct_vs_market_context_and_scope(self):
        linked = self.read()["incidents"]["rows"]
        self.assertEqual({(i["rule"], i["relation"]) for i in linked}, {
            ("remaining_mismatch", "direct"), ("missing_local_fill", "direct"), ("position_mismatch", "market_context")})
        self.assertTrue(all(i["scope_id"] == self.scope and i["session_id"] == "a" * 32 for i in linked))
        self.assertTrue(all(i["first_evidence"]["result"] and i["actions"] for i in linked))
        with self.assertRaises(ValueError): self.read(scope=self.other)
        with self.assertRaises(ValueError): self.read(instrument="WRONG")

    def test_unknown_or_conflicting_subaccounts_do_not_link(self):
        for sub in (None, 1):
            with Store(self.history) as store:
                insert_fill(store, self.scope, fill(event_id=f"ambiguous-{sub}", subaccount=sub))
                store.db.commit()
            data = self.read()
            self.assertEqual(data["identity"]["status"], "unknown_or_ambiguous")
            self.assertEqual(data["requests"]["rows"], [])
            self.assertEqual(data["incidents"]["rows"], [])

    def test_wrong_market_subaccount_and_conflicting_trace_are_excluded(self):
        with Store(self.history) as store:
            for session, key, value in [("g"*32, "subaccount", 1), ("h"*32, "instrument_id", "OTHER")]:
                records = trace(session=session)
                for r in records: r[key] = value
                insert_trace(store, self.scope, records)
            records = trace(session="i"*32)
            records[1]["subaccount"] = 1
            insert_trace(store, self.scope, records)
            insert_trace(store, self.other, trace(session="j"*32))
            store.db.commit()
        data = self.read()
        self.assertEqual(len(data["requests"]["rows"]), 3)
        self.assertEqual(data["requests"]["excluded_requests"], 1)

    def test_timeout_can_link_by_unique_client_but_reuse_is_rejected(self):
        records = trace("submit", code=None, status=None, session="g"*32)
        for r in records:
            r["payload"].pop("order_id")
            r["payload"]["client_order_id"] = "client-1"
        with Store(self.history) as store:
            insert_trace(store, self.scope, records); store.db.commit()
        linked = self.read()["requests"]["rows"]
        client = next(r for r in linked if r["session_id"] == "g"*32)
        self.assertEqual((client["link_basis"], client["outcome"]), ("unique_client_order_id", "unknown"))
        with Store(self.history) as store:
            other = fill(event_id="reused", order="other-order")
            other["payload"]["client_order_id"] = "client-1"
            insert_fill(store, self.scope, other); store.db.commit()
        result = self.read()["requests"]
        self.assertEqual(result["ambiguous_client_ids"], ["client-1"])
        self.assertEqual(len(result["rows"]), 3)

    def test_client_id_does_not_override_explicit_order_and_amend_keeps_fills_separate(self):
        records = trace("submit", session="g"*32)
        for r in records: r["payload"].update(order_id="different", client_order_id="client-1")
        amend = trace("amend", session="h"*32)
        amend[-1]["payload"].update(order_id="replacement", previous_order_id="order-1")
        with Store(self.history) as store:
            insert_trace(store, self.scope, records); insert_trace(store, self.scope, amend)
            insert_fill(store, self.scope, fill(event_id="replacement-fill", order="replacement"))
            store.db.commit()
        data = self.read()
        self.assertEqual(data["fill_count"], 2)
        self.assertEqual(data["requests"]["excluded_requests"], 1)
        self.assertEqual(next(r for r in data["requests"]["rows"] if r["operation"] == "amend")["related_order_ids"], ["replacement"])

    def test_incomplete_trace_and_missing_reference_are_explicit(self):
        with Store(self.history) as store:
            insert_trace(store, self.scope, trace(session="g"*32)[:-1]); store.db.commit()
        for ref in (None, Path(self.temp.name)/"missing.sqlite"):
            data = self.read(reference_path=ref)
            self.assertEqual(data["references"]["status"], "unavailable")
            self.assertTrue(all(m["per_contract_usd"] is None for r in data["markouts"]["rows"] for m in r["markouts"]))
            incomplete = next(r for r in data["requests"]["rows"] if r["session_id"] == "g"*32)
            self.assertIsNone(incomplete["elapsed_ms"])
        corrupt = Path(self.temp.name)/"corrupt.sqlite"; corrupt.write_text("invalid sqlite")
        self.assertEqual(self.read(reference_path=corrupt)["references"]["reason"], "reference_store_unavailable")
        self.assertFalse((Path(self.temp.name)/"missing.sqlite").exists())

    def test_caps_are_explicit_and_totals_not_silently_reduced(self):
        with Store(self.history) as store:
            for n in range(101): insert_fill(store, self.scope, fill(event_id=f"fill-extra-{n}"))
            store.db.commit()
        with patch.dict(LIMITS, {"exchange_events": 100, "requests": 1, "markout_fills": 1,
                                 "reference_quotes": 1, "incidents": 1, "timeline": 1, "check_bytes": 1}):
            data = self.read()
        self.assertEqual((data["fill_count"], len(data["events"])), (103, 100))
        self.assertTrue(data["exchange_truncated"] and data["timeline_truncated"])
        self.assertTrue(all(data[k]["truncated"] for k in ("requests", "markouts", "references", "incidents")))
        self.assertTrue(data["incidents"]["rows"][0]["first_evidence"]["truncated"])
        self.assertEqual(data["totals"]["volume"], "103")

    def test_receipt_fallback_and_primary_inventory_context(self):
        with Store(self.history) as store:
            event = fill(event_id="no-source", received=4); event["occurred_at"] = None
            insert_fill(store, self.scope, event)
            incident(store, self.scope, "inventory_limit", "TEST-MARKET", session="risk:primary:1", market="PRIMARY-ACCOUNT")
            incident(store, self.scope, "inventory_limit", "primary:total", session="risk:primary:2", market="PRIMARY-ACCOUNT")
            store.db.commit()
        data = self.read()
        self.assertEqual(next(e for e in data["timeline"] if e["id"] == "exchange:no-source")["clock"], "import_receipt_fallback")
        risk = [i for i in data["incidents"]["rows"] if i["rule"] == "inventory_limit"]
        self.assertEqual([(i["entity"], i["relation"]) for i in risk], [("TEST-MARKET", "market_context")])

    def test_read_only_api_and_invalid_identity(self):
        handler = type("TestHandler", (Handler,), {"history_db": self.history, "reference_db": self.refs, "log_message": lambda *_: None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{server.server_port}/api/history/order-investigation"
        try:
            with urlopen(base+f"?scope={self.scope}&instrument=TEST-MARKET&order=order-1") as response:
                data = json.load(response)
            self.assertEqual(data["version"], "order-investigation-1")
            for query in ("", f"?scope={self.other}&instrument=TEST-MARKET&order=order-1", f"?scope={self.scope}&instrument=TEST-MARKET&order="+"x"*201):
                with self.assertRaises(HTTPError) as error: urlopen(base+query)
                self.assertEqual(error.exception.code, 400)
            with self.assertRaises(HTTPError) as error: urlopen(base, data=b"{}")
            self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__": unittest.main()
