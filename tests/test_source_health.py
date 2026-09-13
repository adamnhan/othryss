"""Expected retirement, recovery and notification semantics without live actions."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from integrations.lip.othryss_telemetry import Publisher
from othryss import alerts, alert_channels
from othryss.bot_state import source_status, evaluate
from othryss.incidents import record_check
from othryss.source_health import observe, retirements
from othryss.storage import Store, encode

AT = datetime(2026, 9, 10, 20, 31, 54, tzinfo=timezone.utc)
SESSION = "a" * 32


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.store = Store(self.root / "history.sqlite")
        self.scope = self.store.bind_account("local", "kalshi", "demo", "test", "key")
        self.log = self.root / "supervisor.log"
        self.monitor = {"scope_id": self.scope, "session_id": SESSION, "instrument_id": "TEST", "subaccount": 0, "grace_seconds": 1}
        self.store.db.execute("INSERT INTO bot_monitors VALUES (?,?,?,?,?,?,?)", (self.scope, SESSION, "TEST", 0, 1, None, None))
        self.record(1, 1, "PRODUCER_START"); self.record(2, 2)
        self.health(2, 2)
        self.launch = {"event": "child_launch", "ticker": "TEST", "pid": 12345, "stdout": "C:/logs/lip_portfolio_TEST_20260910T203154Z.out.log"}
        self.eviction = "[evict] TEST expected $0.00 < $1.00 sustained over 3 scans; cancelled 1 resting order(s)"
        self.log.write_text(json.dumps(self.launch) + "\n" + self.eviction + "\n")

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def record(self, seq, seconds, kind="BOT_STATE", session=SESSION):
        r = {"schema_version": "0.3.0", "producer": "lip-requote-probe", "session_id": session, "sequence": seq,
             "event_id": f"{session}:{seq}", "occurred_at": (AT+timedelta(seconds=seconds)).isoformat(), "instrument_id": "TEST", "subaccount": 0,
             "type": kind, "payload": {"phase": "after_step"}, "run_id": "run", "account": "test", "environment": "demo", "workspace": "local"}
        self.store.db.execute("INSERT INTO telemetry_records VALUES (?,?,?,?,?,?,?,?,?,?,?)", (self.scope,r["event_id"],session,seq,None,"TEST",0,r["occurred_at"],r["occurred_at"],kind,encode(r)))
        self.store.db.commit()

    def health(self, seconds, seq, **extra):
        h = {"heartbeat_at": (AT+timedelta(seconds=seconds)).isoformat(), "last_sequence": seq, "dropped": 1, "write_failures": 1,
             "queue_depth": 0, "capped": False, "stopped": False} | extra
        self.store.db.execute("INSERT OR REPLACE INTO telemetry_health VALUES (?,?,?,?)", (self.scope, SESSION, h["heartbeat_at"], encode(h))); self.store.db.commit()

    def observe(self, seconds, **kwargs):
        observe(self.store, self.scope, now=AT+timedelta(seconds=seconds), **kwargs)

    def status(self, seconds):
        return source_status(self.store.db, self.scope, SESSION, AT+timedelta(seconds=seconds))

    def test_transient_error_recovers_only_after_quiet_fresh_advancing_state(self):
        self.observe(2)
        self.assertEqual(self.status(2)[0], "unavailable")
        self.record(3, 100); self.health(100, 3); self.observe(100)
        self.assertEqual(self.status(100)[0], "unavailable")
        self.record(4, 123); self.health(123, 4); self.observe(123)
        self.assertEqual(self.status(123)[0], "healthy")
        self.assertEqual(self.status(123)[2]["dropped"], 1)
        self.health(124, 4, write_failures=2); self.observe(124)
        self.assertEqual(self.status(124)[0], "unavailable")

    def test_repeated_heartbeat_and_worker_outage_do_not_establish_recovery(self):
        self.observe(2); self.observe(123)
        self.assertEqual(self.status(123)[0], "unavailable")
        self.record(3, 500); self.health(500, 3); self.observe(500)
        self.assertEqual(self.status(500)[0], "unavailable")

    def test_sequence_gap_and_missing_import_and_missing_new_state_block_recovery(self):
        self.observe(2)
        self.record(4, 123); self.health(123, 4); self.observe(123)
        self.assertIn("sequence has gaps", self.status(123)[1])
        self.record(3, 122)
        self.health(124, 5); self.observe(124)
        self.assertIn("not all reached", self.status(124)[1])
        self.health(125, 4, dropped=2); self.observe(125)
        self.record(5, 246, "HTTP_ATTEMPT"); self.health(246, 5, dropped=2); self.observe(246)
        self.assertIn("new bot-state", self.status(246)[1])

    def test_expected_retirement_requires_explicit_log_matching_session_stale_heartbeat_and_exit(self):
        self.observe(200, supervisor_log=self.log, absent=lambda pid: False)
        self.assertEqual(self.status(200)[0], "unavailable")
        self.observe(200, supervisor_log=self.log, absent=lambda pid: pid == 12345)
        state, reason, health = self.status(200)
        self.assertEqual(state, "stopped"); self.assertIn("supervisor evicted", reason)
        self.assertTrue(health["retirement"]["process_exit_verified"])
        self.health(201, 2); self.observe(201, supervisor_log=self.log, absent=lambda pid: True)
        self.assertNotEqual(self.status(201)[0], "stopped")
        self.observe(500, supervisor_log=self.log, absent=lambda pid: True)
        self.assertNotEqual(self.status(500)[0], "stopped", "An old eviction cannot explain a later outage after the session revived")

    def test_retirement_evidence_survives_log_rotation(self):
        self.observe(200, supervisor_log=self.log, absent=lambda _: True)
        self.log.write_text("")
        self.observe(231, supervisor_log=self.log, absent=lambda _: True)
        self.assertEqual(self.status(231)[0], "stopped")
        self.observe(262, supervisor_log=self.log, absent=lambda _: False)
        self.assertEqual(self.status(262)[0], "stopped", "PID reuse cannot undo this session's previously verified exit")

    def test_missing_log_ambiguous_launch_wrong_run_or_scope_never_retires(self):
        self.observe(200, supervisor_log=self.root/"missing", absent=lambda _: True)
        self.assertEqual(self.status(200)[0], "unavailable")
        self.record(1, 3, "PRODUCER_START", "b"*32)
        self.observe(200, supervisor_log=self.log, absent=lambda _: True)
        self.assertEqual(self.status(200)[0], "unavailable")
        self.log.write_text(json.dumps(self.launch | {"stdout": "lip_portfolio_TEST_20260910T213154Z.out.log"})+"\n"+self.eviction)
        self.observe(200, supervisor_log=self.log, absent=lambda _: True)
        self.assertEqual(self.status(200)[0], "unavailable")
        other = self.store.bind_account("local", "kalshi", "demo", "other", "key")
        observe(self.store, other, supervisor_log=self.log, now=AT+timedelta(seconds=200), absent=lambda _: True)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM source_health WHERE scope_id=?", (other,)).fetchone()[0], 0)

    def test_eviction_for_previous_run_does_not_retire_new_launch(self):
        self.log.write_text(json.dumps(self.launch)+"\n"+self.eviction+"\n"+json.dumps(self.launch | {"pid": 23456, "stdout":"lip_portfolio_TEST_20260910T213154Z.out.log"}))
        items = retirements(self.log)
        self.assertEqual([i["pid"] for i in items], [12345])

    def test_historical_heartbeat_errors_do_not_block_fresh_complete_source(self):
        self.health(2,2,dropped=0,write_failures=0,heartbeat_failures=1)
        self.observe(2)
        self.assertEqual(self.status(2)[0],"healthy")
        self.health(3,2,dropped=0,write_failures=0,heartbeat_failures=2)
        self.observe(3)
        self.assertEqual(self.status(3)[0],"healthy")
        self.assertEqual(self.status(200)[0],"unavailable", "Actually missing heartbeats still alert")

    def test_real_gaps_take_priority_over_transient_error_recovery(self):
        self.record(4,3);self.health(3,4);self.observe(3)
        self.assertIn("sequence has gaps",self.status(3)[1])

    def test_real_gap_keeps_ordinary_opening_grace(self):
        self.store.db.execute("UPDATE bot_monitors SET grace_seconds=120");self.store.db.commit()
        self.record(4,3);self.health(3,4);self.observe(3)
        evaluate(self.store,self.scope,now=AT+timedelta(seconds=3))
        self.record(5,124);self.health(124,5);self.observe(124)
        evaluate(self.store,self.scope,now=AT+timedelta(seconds=124))
        self.assertEqual(self.store.db.execute("SELECT status FROM incidents WHERE rule='source_stale'").fetchone()[0],"open")

    def test_transient_errors_do_not_open_at_recovery_boundary_but_persistent_errors_do(self):
        self.observe(2)
        for n,sec in enumerate((2,33,64,95,126,157),3):
            self.record(n,sec);self.health(sec,n);self.observe(sec)
            # A complete fresh exchange capture exists; no real data outage is hidden.
            self.store.db.execute("INSERT INTO bot_exchange VALUES (?,?,?,?,?,?,?)",(f"x{sec}",self.scope,"TEST",0,(AT+timedelta(seconds=sec)).isoformat(),(AT+timedelta(seconds=sec)).isoformat(),encode({})))
            self.store.db.commit()
            with patch("othryss.bot_state.compare",return_value={"status":"consistent","findings":[],"evaluated_rules":[]}):
                evaluate(self.store,self.scope,now=AT+timedelta(seconds=sec))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM incident_actions WHERE action='opened'").fetchone()[0],0)
        # Continuous new writer failures are not silenced indefinitely.
        for n,sec in enumerate(range(200,561,31),9):
            self.record(n,sec);self.health(sec,n,write_failures=n);self.observe(sec)
            self.store.db.execute("INSERT INTO bot_exchange VALUES (?,?,?,?,?,?,?)",(f"x{sec}",self.scope,"TEST",0,(AT+timedelta(seconds=sec)).isoformat(),(AT+timedelta(seconds=sec)).isoformat(),encode({})))
            self.store.db.commit()
            evaluate(self.store,self.scope,now=AT+timedelta(seconds=sec))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM incident_actions WHERE action='opened'").fetchone()[0],1)

    def test_prior_verified_shutdown_can_repair_pid_reuse_regression(self):
        self.observe(200,supervisor_log=self.log,absent=lambda _:True)
        evaluate(self.store,self.scope,now=AT+timedelta(seconds=200))
        row=self.store.db.execute("SELECT retirement_json FROM source_health").fetchone()
        value=json.loads(row[0]);value["process_exit_verified"]=False
        self.store.db.execute("UPDATE source_health SET retirement_json=?",(encode(value),));self.store.db.commit()
        self.observe(231,supervisor_log=self.log,absent=lambda _:False)
        self.assertEqual(self.status(231)[0],"stopped")
        self.assertIn("restored_from_check",self.status(231)[2]["retirement"])

    def test_expected_shutdown_resolves_source_only_and_has_specific_alert(self):
        for rule, entity in (("source_stale", "source"), ("position_mismatch", "position")):
            result = {"status": "difference", "reason": "test", "findings": [{"rule": rule,"entity": entity}], "evaluated_rules": [rule]}
            for sec in (0, 2): record_check(self.store,self.monitor,f"seed:{rule}:{sec}",result,AT+timedelta(seconds=sec))
        self.observe(200, supervisor_log=self.log, absent=lambda _: True)
        evaluate(self.store,self.scope,now=AT+timedelta(seconds=200))
        evaluate(self.store,self.scope,now=AT+timedelta(seconds=231))
        rows = {r["rule"]:dict(r) for r in self.store.db.execute("SELECT * FROM incidents")}
        self.assertEqual(rows["source_stale"]["status"], "resolved")
        self.assertEqual(rows["position_mismatch"]["status"], "open")
        event = alerts.envelope(alerts.inventory_context(self.store.db, rows["source_stale"]), "resolved", AT.isoformat())
        text = alert_channels.message(event)
        self.assertIn("Expected shutdown", text)
        self.assertNotIn("Comparable checks confirmed recovery", text)
        self.assertNotIn(str(self.log), text)

    def test_alert_source_reason_and_counters_without_raw_error_or_notes(self):
        result = {"reason": "Producer heartbeat is missing or stale", "health": {"heartbeat_at": AT.isoformat(), "dropped":1, "write_failures":1, "raw_error":"PRIVATE CANARY"}, "findings":[]}
        self.store.db.execute("INSERT INTO bot_checks VALUES (?,?,?,?,?,?,?)",("check",self.scope,SESSION,"snapshot",AT.isoformat(),"unavailable",encode(result)))
        i = {"scope_id":self.scope,"incident_id":"incident","instrument_id":"TEST","rule":"source_stale","entity":"source","assessment":"difference","last_seen":AT.isoformat(),"last_check":"check"}
        event = alerts.envelope(alerts.inventory_context(self.store.db,i),"opened",AT.isoformat())
        text = alert_channels.message(event)
        self.assertIn("Bot heartbeat is missing or stale",text); self.assertIn("writer errors: 1",text)
        self.assertNotIn("CANARY",json.dumps(event))


class PublisherHealthTests(unittest.TestCase):
    def test_heartbeat_failure_does_not_claim_event_loss_and_retains_only_error_type(self):
        publisher = Publisher.__new__(Publisher)
        publisher.lock = threading.Lock(); publisher.heartbeat_failures = publisher.dropped = publisher.write_failures = 0
        with patch.object(publisher,"_publish_health",side_effect=PermissionError("SECRET CANARY")):
            self.assertFalse(publisher._health_cycle())
        self.assertEqual((publisher.heartbeat_failures,publisher.dropped,publisher.write_failures),(1,0,0))
        self.assertEqual((publisher.last_error_stage,publisher.last_error_type),("heartbeat","PermissionError"))
        self.assertNotIn("CANARY",str(publisher.__dict__))

    def test_windows_sharing_retry_is_bounded_and_off_trading_thread(self):
        with tempfile.TemporaryDirectory() as root:
            publisher = Publisher.__new__(Publisher); publisher.directory=Path(root); publisher.session=SESSION
            publisher.health=lambda:{"test":True}
            with patch("integrations.lip.othryss_telemetry.os.replace",side_effect=[PermissionError(),PermissionError(),None]) as replace, patch("integrations.lip.othryss_telemetry.time.sleep") as sleep:
                publisher._publish_health()
                self.assertEqual(replace.call_count,3); self.assertEqual(sleep.call_count,2)
            with patch("integrations.lip.othryss_telemetry.os.replace",side_effect=PermissionError()) as replace, patch("integrations.lip.othryss_telemetry.time.sleep"):
                with self.assertRaises(PermissionError):publisher._publish_health()
                self.assertEqual(replace.call_count,3)


if __name__=="__main__":unittest.main()
