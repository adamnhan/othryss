"""Offline telemetry acceptance: no bot initialization, credentials, or network."""
import base64
import json
import queue
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from integrations.lip.othryss_telemetry import Publisher, attach_probe
from othryss.storage import Store
from othryss.telemetry import import_directory, catalog, request_detail
from othryss.history import run_import
from test_history import FakeClient

class RequestException(Exception):
    pass

class Timeout(RequestException):
    pass

# Only protocol stand-ins; this test never signs or creates a network session.
requests = types.SimpleNamespace(RequestException=RequestException, Timeout=Timeout)
padding = types.SimpleNamespace(PSS=type("PSS", (), {"DIGEST_LENGTH": 32, "__init__": lambda self, **kwargs: None}), MGF1=lambda _: None)
hashes = types.SimpleNamespace(SHA256=lambda: None)


class Response:
    def __init__(self, status=201, order="entry"):
        self.status_code = status
        self.order = order
    def json(self):
        return {"order": {"order_id": self.order, "client_order_id": "client-1", "secret": "SECRET-CANARY"}, "secret": "SECRET-CANARY"}


class Session:
    def __init__(self, results):
        self.results, self.calls = list(results), []
    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class Client:
    def __init__(self, results):
        self.sess = Session(results)
    def req(self, method, path, body=None, timeout=15):
        for _ in range(6):
            response = self.sess.request(method, path, data=body, timeout=timeout, headers={"key": "SECRET-CANARY"})
            if response.status_code != 429:
                return response
        return response


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "history.sqlite")
        self.scope = self.store.bind_account("local", "kalshi", "demo", "test", "key")
        self.publishers = []
    def tearDown(self):
        for publisher in self.publishers:
            publisher.close()
        self.store.close()
        self.temp.cleanup()
    def publisher(self, **kwargs):
        publisher = Publisher(self.root / "spool", account="test", environment="demo", ticker="TEST-MARKET", run_id="run-1", **kwargs)
        self.publishers.append(publisher)
        return publisher
    def capture(self, client, method="POST", path="/portfolio/events/orders", **kwargs):
        publisher = self.publisher()
        attach_probe(client, ticker="TEST-MARKET", run_id="run-1", publisher=publisher)
        try:
            return client.req(method, path, **kwargs)
        finally:
            publisher.close()
    def imported(self):
        result = import_directory(self.store, self.scope, self.root / "spool")
        self.assertEqual(result["errors"], 0)
        return catalog(self.store.db, self.scope)
    def trace(self):
        request = self.imported()["requests"][0]
        return request_detail(self.store.db, self.scope, request["session_id"], request["request_id"])

    def test_retry_preserves_response_arguments_and_exact_fill_link(self):
        response = Response()
        client = Client([Response(429), response])
        body = {"ticker": "TEST-MARKET", "count": "49.32", "price": "0.0600", "client_order_id": "client-1", "secret": "SECRET-CANARY"}
        self.assertIs(self.capture(client, body=body, timeout=7), response)
        self.assertEqual(len(client.sess.calls), 2)
        self.assertIs(client.sess.calls[0][1]["data"], body)
        self.assertEqual(client.sess.calls[0][1]["timeout"], 7)
        run_import(self.store, FakeClient(), self.scope, "test")
        trace = self.trace()
        self.assertEqual(sum(r["type"] == "HTTP_ATTEMPT" for r in trace["records"]), 2)
        self.assertEqual(trace["records"][-1]["payload"]["attempts"], 2)
        self.assertEqual([e["payload"]["fill_id"] for e in trace["exchange_events"] if e["type"] == "ORDER_FILL"], ["fill-buy"])
        self.assertNotIn("SECRET-CANARY", json.dumps(trace))
        self.assertEqual(import_directory(self.store, self.scope, self.root / "spool")["inserted"], 0)

    def test_timeout_keeps_exception_and_later_client_id_link(self):
        error = requests.Timeout("SECRET-CANARY")
        with self.assertRaises(requests.Timeout) as raised:
            self.capture(Client([error]), body={"ticker": "TEST-MARKET", "client_order_id": "client-1"})
        self.assertIs(raised.exception, error)
        trace = self.trace()
        self.assertEqual(trace["records"][-1]["payload"]["outcome"], "unknown")
        self.assertEqual(trace["order_ids"], [])
        exchange = FakeClient()
        for page in exchange.pages.values():
            for order in page.get("orders", []):
                order["client_order_id"] = "client-1"
        run_import(self.store, exchange, self.scope, "test")
        self.assertEqual(self.trace()["order_ids"], ["entry"])
        self.assertNotIn("SECRET-CANARY", json.dumps(self.trace()))

    def test_amend_tracks_old_and_new_ids_cancel_404_is_http_error(self):
        client = Client([Response(order="replacement"), Response(404)])
        publisher = self.publisher()
        attach_probe(client, ticker="TEST-MARKET", run_id="run-1", publisher=publisher)
        client.req("POST", "/portfolio/events/orders/entry/amend", {"ticker": "TEST-MARKET"})
        client.req("DELETE", "/portfolio/events/orders/replacement")
        publisher.close()
        requests_ = self.imported()["requests"]
        traces = [request_detail(self.store.db, self.scope, r["session_id"], r["request_id"]) for r in requests_]
        amend = next(t for t in traces if t["records"][0]["payload"]["operation"] == "amend")
        self.assertEqual(amend["order_ids"], ["entry", "replacement"])
        cancel = next(t for t in traces if t is not amend)
        self.assertEqual(cancel["records"][-1]["payload"]["outcome"], "http_error")
        self.assertEqual(cancel["exchange_events"], [])

    def test_disabled_wrong_market_and_failed_publisher_preserve_bot(self):
        response = Response()
        client = Client([response])
        original = client.req
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(attach_probe(client, ticker="TEST-MARKET", run_id="run"))
        self.assertEqual(client.req, original)
        broken = types.SimpleNamespace(emit=lambda *_: (_ for _ in ()).throw(OSError("full disk")))
        attach_probe(client, ticker="TEST-MARKET", run_id="run", publisher=broken)
        self.assertIs(client.req("POST", "/portfolio/events/orders"), response)

    def test_capacity_is_bounded_and_gaps_are_visible(self):
        publisher = self.publisher(max_bytes=1800)
        for i in range(100):
            publisher.emit("PRODUCER_START", {})
        publisher.close()
        data = self.imported()
        self.assertTrue(data["sessions"][0]["health"]["capped"])
        self.assertGreater(data["sessions"][0]["health"]["dropped"], 0)
        self.assertLessEqual(next((self.root / "spool").glob("*.jsonl")).stat().st_size, 1800)

    def test_queue_overflow_is_reported_as_gap(self):
        publisher = self.publisher()
        with patch.object(publisher.queue, "put_nowait", side_effect=queue.Full):
            publisher.emit("PRODUCER_START", {})
        publisher.close()
        session = self.imported()["sessions"][0]
        self.assertEqual(session["health"]["dropped"], 1)
        self.assertEqual(session["sequence_gaps"], 1)

    def test_unwritable_spool_does_not_fail_request(self):
        (self.root / "spool").write_text("directory unavailable")
        response = Response()
        self.assertIs(self.capture(Client([response])), response)
        self.assertEqual(import_directory(self.store, self.scope, self.root / "spool")["status"], "waiting_for_producer")

    def test_get_and_other_market_do_not_create_traces(self):
        client = Client([Response(), Response()])
        publisher = self.publisher()
        attach_probe(client, ticker="TEST-MARKET", run_id="run-1", publisher=publisher)
        client.req("GET", "/portfolio/orders")
        client.req("POST", "/portfolio/events/orders", {"ticker": "OTHER-MARKET"})
        publisher.close()
        self.assertEqual(self.imported()["requests"], [])

    def test_environment_and_market_opt_in_must_match(self):
        client = Client([])
        configured = {"OTHRYSS_TELEMETRY_DIR": str(self.root / "spool"), "OTHRYSS_TICKER": "TEST-MARKET", "OTHRYSS_ACCOUNT": "test", "OTHRYSS_ENVIRONMENT": "demo"}
        with patch.dict("os.environ", configured, clear=True), patch.dict(Client.req.__globals__, {"REST": "https://api.elections.kalshi.com/trade-api/v2"}):
            self.assertIsNone(attach_probe(client, ticker="TEST-MARKET", run_id="run"))
        with patch.dict("os.environ", configured, clear=True), patch.dict(Client.req.__globals__, {"REST": "https://demo-api.kalshi.co/trade-api/v2"}):
            self.assertIsNone(attach_probe(client, ticker="OTHER-MARKET", run_id="run"))
            publisher = attach_probe(client, ticker="TEST-MARKET", run_id="run")
            self.assertIsNotNone(publisher)
            self.publishers.append(publisher)
            publisher.close()
        self.assertEqual(self.imported()["sessions"][0]["health"]["environment"], "demo")

    def test_exchange_evidence_cannot_cross_subaccount(self):
        self.capture(Client([Response()]))
        exchange = FakeClient()
        for page in exchange.pages.values():
            for event in page.get("orders", []) + page.get("fills", []):
                event["subaccount_number"] = 1
                event["client_order_id"] = "client-1"
        run_import(self.store, exchange, self.scope, "test")
        self.assertEqual(self.trace()["exchange_events"], [])

    def test_all_lip_probes_attach_and_keep_requests_separate(self):
        from integrations.lip import othryss_telemetry as sdk
        config = {"directory": str(self.root / "spool"), "account": "test", "environment": "demo", "all_lip_probes": True}
        (self.root / "othryss_telemetry.json").write_text(json.dumps(config))
        clients = [Client([Response(order="same-id")]) for _ in range(3)]
        with patch.dict("os.environ", {}, clear=True), patch.object(sdk, "__file__", str(self.root / "othryss_telemetry.py")), patch.dict(Client.req.__globals__, {"REST": "https://demo-api.kalshi.co/trade-api/v2"}):
            for i, client in enumerate(clients):
                publisher = attach_probe(client, ticker=f"MARKET-{i}", run_id="same-run")
                self.assertIsNotNone(publisher)
                self.publishers.append(publisher)
            with ThreadPoolExecutor(max_workers=3) as pool:
                results = list(pool.map(lambda c: c.req("POST", "/portfolio/events/orders"), clients))
            self.assertTrue(all(r.status_code == 201 for r in results))
            # Explicit disable also overrides the persistent all-probes config.
            with patch.dict("os.environ", {"OTHRYSS_TELEMETRY_DISABLED": "1"}):
                self.assertIsNone(attach_probe(Client([]), ticker="MARKET-4", run_id="run"))
        for publisher in self.publishers:
            publisher.close()
        data = self.imported()
        self.assertEqual(len(data["sessions"]), 3)
        self.assertEqual(len(data["requests"]), 3)
        self.assertEqual({r["instrument_id"] for r in data["requests"]}, {"MARKET-0", "MARKET-1", "MARKET-2"})
        for r in data["requests"]:
            trace = request_detail(self.store.db, self.scope, r["session_id"], r["request_id"])
            self.assertEqual({e["instrument_id"] for e in trace["records"]}, {r["instrument_id"]})

    def test_three_processes_share_spool_without_collision(self):
        code = """import sys
from integrations.lip.othryss_telemetry import Publisher
p=Publisher(sys.argv[1],account='test',environment='demo',ticker=sys.argv[2],run_id='same-run')
for i in range(20):
    p.emit('ORDER_INTENT', {'request_id':'b'*32,'operation':'submit','client_order_id':'same-client'})
p.close()
# close() deliberately limits bot shutdown waits. This collision test needs
# the full spool, so wait for its background writer before exiting the child.
p.thread.join(timeout=10)
assert not p.thread.is_alive(), 'Synthetic spool writer did not finish'
"""
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.root / "spool"), f"MARKET-{i}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, stderr.decode())
            data = self.imported()
            self.assertEqual(len(data["sessions"]), 3)
            self.assertEqual(len(data["requests"]), 3)
            self.assertTrue(all(s["records"] == 22 and s["sequence_gaps"] == 0 and s["health"]["dropped"] == 0 for s in data["sessions"]))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.wait()

    def test_partial_line_resume_reopen_and_truncation(self):
        self.capture(Client([Response()]))
        path = next((self.root / "spool").glob("*.jsonl"))
        original = path.read_bytes()
        path.write_bytes(original[:-7])
        self.imported()
        count = self.store.db.execute("SELECT COUNT(*) FROM telemetry_records").fetchone()[0]
        self.store.close()
        self.store = Store(self.root / "history.sqlite")
        path.write_bytes(original)
        self.assertEqual(import_directory(self.store, self.scope, path.parent)["inserted"], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM telemetry_records").fetchone()[0], count + 1)
        path.write_bytes(b"")
        self.assertEqual(import_directory(self.store, self.scope, path.parent)["errors"], 1)
        self.assertEqual(catalog(self.store.db, self.scope)["file_errors"], 1)

    def test_scope_binding_and_unapproved_fields_rejected(self):
        self.capture(Client([Response()]))
        other = self.store.bind_account("local", "kalshi", "production", "other", "key")
        self.assertGreater(import_directory(self.store, other, self.root / "spool")["errors"], 0)
        self.assertEqual(catalog(self.store.db, other)["requests"], [])
        path = next((self.root / "spool").glob("*.jsonl"))
        lines = path.read_text().splitlines()
        record = json.loads(lines[0]); record["secret"] = "SECRET-CANARY"
        path.write_text(json.dumps(record) + "\n" + "\n".join(lines[1:]) + "\n")
        self.assertEqual(import_directory(self.store, self.scope, path.parent)["errors"], 1)
        self.assertEqual(catalog(self.store.db, self.scope)["requests"], [])

    @unittest.skipUnless(Path("artifacts/integrations/lip/original_req.py").exists(), "Prepare source patch for actual bot retry acceptance")
    def test_actual_bot_request_retry_implementation_offline(self):
        # Compile ONLY the inspected req function. Never import the bot or call KX.__init__.
        sleeps = []
        globals_ = {"json": json, "base64": base64, "urlparse": urlparse, "REST": "https://demo-api.kalshi.co/trade-api/v2",
                    "requests": requests, "padding": padding, "hashes": hashes,
                    "time": types.SimpleNamespace(time=lambda: 1000, sleep=sleeps.append)}
        exec(compile(Path("artifacts/integrations/lip/original_req.py").read_text(), "original_req.py", "exec"), globals_)
        response = Response()
        client = Client([requests.Timeout("SECRET-CANARY"), Response(429), response])
        client.key_id = "SECRET-CANARY"
        client.key = types.SimpleNamespace(sign=lambda *_: b"SECRET-CANARY")
        client.req = types.MethodType(globals_["req"], client)
        self.assertIs(self.capture(client, body={"ticker": "TEST-MARKET"}), response)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(len(client.sess.calls), 3)
        trace = self.trace()
        self.assertEqual(trace["records"][-1]["payload"]["attempts"], 3)
        self.assertNotIn("SECRET-CANARY", json.dumps(trace))


if __name__ == "__main__":
    unittest.main()
