"""Seeded divergence and workflow tests, isolated from production trading."""
import copy
import json
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from othryss.bot_state import compare, validate_state, collect, evaluate, source_status
from othryss.incidents import record_check, catalog, detail, act
from othryss.storage import Store, encode
from othryss.telemetry import import_directory
from othryss.server import Handler
from integrations.lip.othryss_telemetry import Publisher, attach_probe, capture_state
from test_telemetry import Client, Response
from test_history import FakeClient

NOW = datetime.now(timezone.utc) - timedelta(seconds=130)
SESSION = "a" * 32


def bot(sequence=1, seconds=-10, **payload):
    p = {"phase": "after_step", "step_ok": True, "position": "0", "inventory": "0", "baseline_position": "0", "position_basis": "baseline_plus_run_inventory_yes",
         "orders": [{"order_id": "entry", "remaining": "108.68", "role": "entry"}], "owned_order_ids": ["entry"], "seen_fill_ids": [],
         "orders_complete": True, "ownership_complete": True, "fills_complete": True} | payload
    return {"schema_version": "0.2.0", "producer": "lip-requote-probe", "session_id": SESSION,
            "workspace": "local", "environment": "demo", "account": "test", "strategy_id": "lip-incentives", "run_id": "run",
            "instrument_id": "TEST-MARKET", "subaccount": 0, "event_id": f"{SESSION}:{sequence}", "sequence": sequence,
            "occurred_at": (NOW+timedelta(seconds=seconds)).isoformat(), "monotonic_ns": str(sequence), "type": "BOT_STATE", "payload": p}


def exchange(scope="scope", **evidence):
    return {"snapshot_id": "capture", "scope_id": scope, "instrument_id": "TEST-MARKET", "subaccount": 0,
            "started_at": NOW.isoformat(), "received_at": (NOW+timedelta(seconds=1)).isoformat(),
            "evidence": {"complete": True, "position": "0", "orders": [{"order_id": "entry", "status": "resting", "remaining_quantity": "108.68"}],
                         "fills": [], "market": {"status": "active", "market_type": "binary"}, "settlements": []} | evidence}


class ComparisonTests(unittest.TestCase):
    def check(self, **evidence):
        return compare(bot(), bot(2, 10), exchange(**evidence))
    def test_matching_state_decimal_tolerance_and_baseline(self):
        self.assertEqual(self.check()["status"], "consistent")
        first = bot(position="59.31999999999999", inventory="49.31999999999999", baseline_position="10")
        last = bot(2, 10, **first["payload"])
        self.assertEqual(compare(first,last,exchange(position="59.32"))["findings"], [])
    def test_position_missing_orders_unknown_orders_remaining_and_fill_mismatches(self):
        result = self.check(position="49.32", orders=[{"order_id":"unknown","status":"resting","remaining_quantity":"1"}], fills=[{"fill_id":"missing","order_id":"entry"}])
        self.assertEqual({f["rule"] for f in result["findings"]}, {"position_mismatch","local_order_missing","unknown_exchange_order","missing_local_fill"})
        result = self.check(orders=[{"order_id":"entry","status":"resting","remaining_quantity":"100"}])
        self.assertEqual(result["findings"][0]["rule"], "remaining_mismatch")
        first=bot(seen_fill_ids=["local-only"]); last=bot(2,10,**first["payload"])
        self.assertEqual(compare(first,last,exchange())["findings"][0]["rule"], "unmatched_local_fill")
    def test_partial_fill_race_and_incomplete_data_do_not_assert_difference(self):
        self.assertEqual(compare(bot(),bot(2,10,position="49.32",inventory="49.32"),exchange(position="49.32"))["status"],"pending")
        self.assertEqual(self.check(complete=False)["status"],"unavailable")
        self.assertNotIn("position_mismatch",self.check(position=None)["evaluated_rules"])
        self.assertEqual(self.check(settlements=[{"settled_time":"now"}])["status"],"unavailable")
        self.assertEqual(self.check(market={"status":"settled","market_type":"binary"})["status"],"unavailable")
    def test_scope_session_time_and_completeness_guards(self):
        wrong=bot(2,10); wrong["subaccount"]=1
        with self.assertRaises(ValueError): compare(bot(),wrong,exchange())
        wrong=bot(2,10); wrong["session_id"]="b"*32
        with self.assertRaises(ValueError): compare(bot(),wrong,exchange())
        self.assertEqual(compare(bot(seconds=-200),bot(2,10),exchange())["status"],"pending")
        first=bot(fills_complete=False,orders_complete=False); last=bot(2,10,**first["payload"])
        self.assertEqual(compare(first,last,exchange())["evaluated_rules"],["position_mismatch"])
    def test_contract_rejects_secrets_nonfinite_quantities_and_oversized_sets(self):
        for change in ({"secret":"x"},{"position":"NaN"},{"seen_fill_ids":[str(i) for i in range(65)]},{"orders_complete":"true"}):
            with self.assertRaises(ValueError): validate_state(bot(**change)["payload"])


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/"history.sqlite"
        self.store=Store(self.path); self.scope=self.store.bind_account("local","kalshi","demo","test","key")
        self.monitor={"scope_id":self.scope,"session_id":SESSION,"instrument_id":"TEST-MARKET","subaccount":0,"grace_seconds":120}
        self.store.db.execute("INSERT INTO bot_monitors(scope_id,session_id,instrument_id,subaccount,grace_seconds) VALUES (?,?,?,?,?)",tuple(self.monitor.values())); self.store.db.commit()
    def tearDown(self):
        self.store.close(); self.temp.cleanup()
    def check(self,n,difference=True,**kwargs):
        result={"status":"difference" if difference else "consistent","reason":"Synthetic seeded discrepancy" if difference else "Synthetic recovery", "rule_version":"bot-state-1",
                "findings":[{"rule":"position_mismatch","entity":"position","message":"Synthetic discrepancy","details":{"bot":"0","exchange":"1"}}] if difference else [],
                "evaluated_rules":["position_mismatch"]} | kwargs
        record_check(self.store,self.monitor,str(n),result,NOW+timedelta(seconds=n))
    def current(self):
        return catalog(self.store.db,self.scope)["incidents"][0]
    def opened(self):
        for n in (0,60,120): self.check(n)
        return self.current()["incident_id"]
    def test_grace_idempotence_acknowledgement_recovery_and_recurrence(self):
        self.check(0); self.check(0); self.assertEqual(self.current()["hits"],1)
        self.check(60); self.assertEqual(self.current()["status"],"pending")
        self.check(120); incident=self.current()["incident_id"]
        self.assertEqual(self.current()["status"],"open")
        act(self.path,self.scope,incident,"acknowledge","Investigating")
        act(self.path,self.scope,incident,"acknowledge","duplicate")
        self.assertEqual(self.current()["status"],"acknowledged")
        self.assertEqual(len(detail(self.store.db,self.scope,incident)["actions"]),2)
        with self.assertRaises(ValueError): act(self.path,self.scope,incident,"resolve")
        self.check(125,False); self.assertEqual(self.current()["status"],"acknowledged")
        self.check(130,False); self.assertEqual(self.current()["status"],"resolved")
        self.check(135); self.assertEqual(self.current()["status"],"pending")
        self.assertNotEqual(self.current()["incident_id"],incident)
    def test_unknown_does_not_resolve_and_breaks_pending_persistence(self):
        self.check(0)
        self.check(60,False,status="pending",evaluated_rules=[])
        self.assertEqual(self.current()["assessment"],"unknown")
        self.check(120); self.assertEqual(self.current()["status"],"pending")
        self.check(180); self.check(240); self.assertEqual(self.current()["status"],"open")
        self.check(250,False,status="unavailable",evaluated_rules=[])
        self.assertEqual(self.current()["status"],"open")
        self.assertEqual(self.current()["assessment"],"unknown")
    def test_manual_resolution_requires_fresh_clear_evidence_and_scope(self):
        incident=self.opened(); self.check(125,False)
        with self.assertRaises(ValueError): act(self.path,"other",incident,"acknowledge")
        self.assertEqual(act(self.path,self.scope,incident,"resolve","Verified recovery")["status"],"resolved")
        self.assertEqual(detail(self.store.db,self.scope,incident)["first_evidence"]["result"]["findings"][0]["details"]["exchange"],"1")
    def test_restart_preserves_incident_and_clear_evidence_cannot_be_reused(self):
        self.opened(); self.check(125,False)
        self.store.close(); self.store=Store(self.path)
        self.check(125,False); self.assertEqual(self.current()["clears"],1)
        self.assertEqual(self.current()["status"],"open")
    def test_source_failure_keeps_open_difference_unknown(self):
        self.opened()
        record_check(self.store,self.monitor,"health:5",{"status":"unavailable","reason":"stale","findings":[],"evaluated_rules":["source_stale"]},NOW+timedelta(seconds=130))
        self.assertEqual(self.current()["assessment"],"unknown")
    def test_http_actions_reject_cross_origin_and_keep_get_read_only(self):
        incident=self.opened()
        handler=type("TestHandler",(Handler,),{"history_db":self.path,"log_message":lambda *_:None})
        server=ThreadingHTTPServer(("127.0.0.1",0),handler); worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
        base=f"http://127.0.0.1:{server.server_port}"
        body=json.dumps({"scope":self.scope,"incident":incident,"action":"acknowledge","note":"reviewed"}).encode()
        try:
            for origin in (None,"https://untrusted.invalid"):
                request=Request(base+"/api/history/incident-action",data=body,headers={"Content-Type":"application/json","X-Othryss-Review":"1",**({"Origin":origin} if origin else {})})
                with self.assertRaises(HTTPError) as raised: urlopen(request)
                self.assertEqual(raised.exception.code,403)
            request=Request(base+"/api/history/incident-action",data=body,headers={"Content-Type":"application/json","X-Othryss-Review":"1","Origin":base})
            with urlopen(request) as response: self.assertEqual(json.load(response)["status"],"acknowledged")
        finally:
            server.shutdown(); server.server_close(); worker.join()


class CaptureTests(unittest.TestCase):
    def test_step_wrapper_preserves_result_exception_and_captures_only_operational_state(self):
        records=[]; publisher=types.SimpleNamespace(emit=lambda kind,payload:records.append((kind,payload)))
        result=object(); client=Client([])
        probe=types.SimpleNamespace(step=lambda live:result,baseline_position=10,inventory=49.32,order_id="entry",order_size=108.68,
                                    exit_mgr=types.SimpleNamespace(order_id="exit"),owned_order_ids={"entry","exit"},fills=[{"fill_id":"fill-1","secret":"CANARY"}],secret="CANARY")
        attach_probe(client,ticker="TEST-MARKET",run_id="run",publisher=publisher,probe=probe)
        self.assertIs(probe.step(False),result)
        self.assertFalse(records)
        self.assertIs(probe.step(True),result)
        payload=next(p for kind,p in records if kind=="BOT_STATE")
        validate_state(payload); self.assertEqual(payload["position"],"59.32")
        self.assertIsNone(payload["orders"][1]["remaining"]); self.assertNotIn("CANARY",json.dumps(records))
        failed=RuntimeError("CANARY")
        probe.step=lambda live:(_ for _ in ()).throw(failed)
        attach_probe(Client([]),ticker="TEST-MARKET",run_id="run",publisher=publisher,probe=probe)
        with self.assertRaises(RuntimeError) as caught: probe.step(True)
        self.assertIs(caught.exception,failed)
        self.assertFalse(records[-1][1]["step_ok"])
    def test_snapshot_missing_fields_reports_unavailable_not_empty_state(self):
        records=[]
        capture_state(types.SimpleNamespace(),types.SimpleNamespace(emit=lambda *args:records.append(args)),"after_step",True)
        self.assertEqual(records,[("BOT_STATE_UNAVAILABLE",{})])
    def test_new_state_spool_imports_and_old_contract_remains_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); publisher=Publisher(root/"spool",account="test",environment="demo",ticker="TEST-MARKET",run_id="run")
            publisher.emit("BOT_STATE",bot()["payload"]); publisher.close()
            with Store(root/"test.sqlite") as store:
                scope=store.bind_account("local","kalshi","demo","test","key")
                self.assertEqual(import_directory(store,scope,root/"spool")["errors"],0)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM telemetry_records WHERE type='BOT_STATE'").fetchone()[0],1)


class WorkerTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    tearDown = WorkflowTests.tearDown
    def seed_source(self):
        for r in (bot(),bot(2,10)):
            self.store.db.execute("INSERT INTO telemetry_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",(self.scope,r["event_id"],SESSION,r["sequence"],None,r["instrument_id"],0,r["occurred_at"],r["occurred_at"],"BOT_STATE",encode(r)))
        health={"heartbeat_at":NOW.isoformat(),"stopped":False,"dropped":0,"write_failures":0,"capped":False}
        self.store.db.execute("INSERT INTO telemetry_health VALUES (?,?,?,?)",(self.scope,SESSION,NOW.isoformat(),encode(health))); self.store.db.commit()
    def test_complete_capture_evaluation_and_failed_followup_preserve_evidence(self):
        self.seed_source()
        class StateClient(FakeClient):
            def request(self,path,params=None):
                if path=="/portfolio/positions": return {"market_positions":[{"ticker":"TEST-MARKET","subaccount_number":0,"position_fp":"49.32"}],"cursor":""}
                if path=="/portfolio/settlements": return {"settlements":[],"cursor":""}
                if path=="/markets/TEST-MARKET": return {"market":{"ticker":"TEST-MARKET","status":"active","market_type":"binary"}}
                return super().request(path,params)
        with patch("othryss.bot_state.utc_now",return_value=NOW.isoformat()): collect(self.store,StateClient(),self.scope,"test")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM bot_exchange").fetchone()[0],1)
        evaluate(self.store,self.scope,now=NOW+timedelta(seconds=20))
        check=self.store.db.execute("SELECT result_json FROM bot_checks WHERE snapshot_id NOT LIKE 'health:%'").fetchone()
        rules={f["rule"] for f in json.loads(check[0])["findings"]}
        self.assertIn("position_mismatch",rules); self.assertIn("missing_local_fill",rules)
        count=self.store.db.execute("SELECT COUNT(*) FROM bot_checks").fetchone()[0]
        evaluate(self.store,self.scope,now=NOW+timedelta(seconds=20))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM bot_checks").fetchone()[0],count)
        with patch.object(StateClient,"request",side_effect=RuntimeError("network unavailable")): collect(self.store,StateClient(),self.scope,"test")
        self.assertIsNotNone(self.store.db.execute("SELECT last_error FROM bot_monitors").fetchone()[0])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM bot_exchange").fetchone()[0],1)
    def test_health_staleness_and_graceful_stop_are_distinct(self):
        self.seed_source()
        self.assertEqual(source_status(self.store.db,self.scope,SESSION,NOW+timedelta(seconds=20))[0],"healthy")
        self.assertEqual(source_status(self.store.db,self.scope,SESSION,NOW+timedelta(seconds=200))[0],"unavailable")
        self.store.db.execute("UPDATE telemetry_health SET health_json=?",(encode({"stopped":True}),)); self.store.db.commit()
        self.assertEqual(source_status(self.store.db,self.scope,SESSION,NOW+timedelta(seconds=200))[0],"stopped")


if __name__=="__main__": unittest.main()
