"""Delivery acceptance with injected transports: no external notifications."""
import copy
import hashlib
import hmac
import io
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from othryss import alerts, alert_channels
from othryss.alerts_cli import cycle
from othryss.storage import Store


def iso(now): return datetime.fromtimestamp(now,timezone.utc).isoformat()


def route(scope,kind="discord",name="discord",**overrides):
    return alerts.DEFAULTS | {"id":name,"kind":kind,"enabled":True,"scope_id":scope,
        "destination":{"url_env":"OTHRYSS_ALERT_DISCORD_URL"} if kind=="discord" else
                      {"url_env":"OTHRYSS_ALERT_WEBHOOK_URL","signing_secret_env":"OTHRYSS_ALERT_WEBHOOK_SECRET"}} | overrides


DISCORD={"url_env":"https://discord.com/api/webhooks/123/SECRET-CANARY"}
WEBHOOK={"url_env":"https://example.com/alerts?token=SECRET-CANARY","signing_secret_env":"x"*32}


def seed_incident(store,scope,now,identity="incident-1",status="open",rule="position_mismatch"):
    incident={"incident_id":identity,"scope_id":scope,"session_id":"session","instrument_id":"TEST-MARKET","rule":rule,
              "entity":"position","status":status,"assessment":"difference","first_seen":iso(now-180),"last_seen":iso(now),
              "opened_at":iso(now),"acknowledged_at":None,"resolved_at":None,"first_check":"check","last_check":"check",
              "hits":2,"clears":0,"last_capture":"capture"}
    store.db.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(incident.values()))
    store.db.commit();return incident


def action(store,identity,event,now,key):
    store.db.execute("INSERT INTO incident_actions VALUES (?,?,?,?,?)",(key,identity,iso(now),event,"PRIVATE REVIEW NOTE"));store.db.commit()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.store=Store(self.root/"history.sqlite")
        self.scope=self.store.bind_account("local","kalshi","demo","test","key")
        self.other=self.store.bind_account("local","kalshi","demo","other","key")
        self.path=self.root/"alerts.sqlite";self.db=alerts.connect(self.path);self.now=time.time()
        self.route=route(self.scope);self.incident=seed_incident(self.store,self.scope,self.now)
        self.sync()
        self.sent=[]
    def tearDown(self): self.db.close();self.store.close();self.temp.cleanup()
    def sync(self,routes=None,resolved=None):
        alerts.sync_routes(self.db,self.store.db,routes or [self.route],resolved if resolved is not None else {self.route["id"]:DISCORD},self.now)
    def discover(self): alerts.discover(self.db,self.store.db,self.route,self.now)
    def send(self,result=None):
        def sender(*args):self.sent.append(args);return result or {"status":"accepted","provider_id":"123"}
        alerts.deliver_one(self.db,self.store.db,self.route,DISCORD,self.now,sender)
    def rows(self):return [dict(r) for r in self.db.execute("SELECT * FROM deliveries ORDER BY created_at,delivery_id")]
    def opened(self):
        action(self.store,"incident-1","opened",self.now,"opened-1");self.discover()

    def test_activation_skips_history_and_future_action_is_deduplicated(self):
        action(self.store,"incident-1","opened",self.now,"old")
        self.route["min_interval_seconds"]=20;self.sync();self.discover()
        self.assertEqual(self.rows(),[])
        action(self.store,"incident-1","opened",self.now,"new");self.discover();self.discover()
        self.assertEqual(len(self.rows()),1)
        self.send();self.send();self.assertEqual(len(self.sent),1)
        self.assertNotIn("PRIVATE REVIEW NOTE",self.rows()[0]["payload_json"])

    def test_accepted_discord_is_not_human_delivery_confirmation(self):
        self.opened();self.send()
        self.assertEqual(self.rows()[0]["status"],"accepted")
        self.assertEqual(alerts.read(self.path,self.scope)["deliveries"][0]["history"][0]["status"],"accepted")

    def test_retry_after_is_persisted_across_restart(self):
        self.opened();self.send({"status":"retry","error":"rate_limited","http_status":429,"retry_after":120})
        self.db.close();self.db=alerts.connect(self.path);alerts.recover_inflight(self.db)
        self.now+=119;self.send();self.assertEqual(len(self.sent),1)
        self.now+=1;self.send();self.assertEqual(len(self.sent),2)
        self.assertEqual(self.rows()[0]["attempts"],2)

    def test_unknown_discord_is_not_automatically_resent(self):
        self.opened();self.send({"status":"unknown","error":"transport_outcome_unknown"})
        self.now+=500;self.send();self.assertEqual(len(self.sent),1)

    def test_crash_after_attempt_start_becomes_unknown(self):
        self.opened()
        def crash(*args):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):alerts.deliver_one(self.db,self.store.db,self.route,DISCORD,self.now,crash)
        alerts.recover_inflight(self.db)
        self.assertEqual(self.rows()[0]["status"],"unknown")
        self.assertEqual(self.db.execute("SELECT status FROM attempts").fetchone()[0],"unknown")

    def test_acknowledgement_suppresses_waiting_open_and_reminders(self):
        self.opened()
        self.store.db.execute("UPDATE incidents SET status='acknowledged'");self.store.db.commit()
        self.send();self.assertEqual(self.rows()[0]["status"],"suppressed");self.assertEqual(self.sent,[])

    def test_recovery_only_follows_a_sent_opening(self):
        self.opened();self.send();self.now+=20
        self.store.db.execute("UPDATE incidents SET status='resolved',assessment='clear'");self.store.db.commit()
        action(self.store,"incident-1","resolved",self.now,"resolved-1");self.discover();self.send()
        self.assertEqual([r["status"] for r in self.rows()],["accepted","accepted"])
        self.assertEqual(self.sent[-1][2]["event"],"resolved")

    def test_fast_recovery_supersedes_unsent_opening(self):
        self.opened();self.now+=2
        self.store.db.execute("UPDATE incidents SET status='resolved',assessment='clear'");self.store.db.commit()
        action(self.store,"incident-1","resolved",self.now,"resolved-1");self.discover()
        self.send();self.send()
        self.assertEqual({r["status"] for r in self.rows()},{"suppressed"});self.assertEqual(self.sent,[])

    def test_reminders_require_fresh_unacknowledged_difference(self):
        self.route["reminder_seconds"]=300;self.sync();self.opened();self.send()
        self.now+=301;self.discover();self.assertEqual(len(self.rows()),1)
        self.store.db.execute("UPDATE incidents SET last_seen=?",(iso(self.now),));self.store.db.commit()
        self.discover();self.discover();self.assertEqual(len(self.rows()),2)
        self.store.db.execute("UPDATE incidents SET status='acknowledged'");self.store.db.commit()
        self.send();self.assertEqual(self.rows()[-1]["status"],"suppressed")

    def test_destination_change_disarms_old_queue_and_reminders(self):
        self.route["reminder_seconds"]=300;self.sync();self.opened();self.send()
        self.now+=301;self.store.db.execute("UPDATE incidents SET last_seen=?",(iso(self.now),));self.store.db.commit()
        changed={"url_env":"https://discord.com/api/webhooks/456/NEW"}
        self.sync(resolved={"discord":changed});self.discover()
        self.assertEqual(len(self.rows()),1)
        alerts.enqueue(self.db,self.route,"queued",self.incident,"test",self.now);self.db.commit()
        self.route["enabled"]=False;self.sync(resolved={})
        self.assertEqual(self.rows()[-1]["status"],"canceled")

    def test_scope_and_rule_filters_do_not_leak(self):
        second=seed_incident(self.store,self.other,self.now,"other-incident")
        action(self.store,second["incident_id"],"opened",self.now,"other-action")
        self.route["rules"]=["source_stale"];self.sync();self.opened()
        self.assertEqual(self.rows(),[])
        self.assertEqual(alerts.read(self.path,self.other)["routes"],[])

    def test_retry_limit_and_expiry(self):
        self.route["max_attempts"]=1;self.sync();self.opened();self.send({"status":"retry"})
        self.assertEqual(self.rows()[0]["status"],"failed")
        alerts.enqueue(self.db,self.route,"test-2",self.incident,"test",self.now);self.db.commit()
        self.now+=3601;self.send();self.assertEqual(next(r for r in self.rows() if r["event_key"]=="test-2")["status"],"expired")

    def test_routes_are_isolated_when_one_fails(self):
        other=route(self.scope,"webhook","hook")
        self.sync(routes=[self.route,other],resolved={"discord":DISCORD,"hook":WEBHOOK})
        self.opened();alerts.discover(self.db,self.store.db,other,self.now)
        self.send({"status":"failed"})
        alerts.deliver_one(self.db,self.store.db,other,WEBHOOK,self.now,lambda *_:{"status":"accepted"})
        self.assertEqual({r["route_id"]:r["status"] for r in self.rows()},{"discord":"failed","hook":"accepted"})

    def test_worker_configuration_reload_and_missing_credentials(self):
        config=self.root/"alerts.local.json"
        config.write_text(json.dumps({"version":1,"routes":[self.route]}))
        env=self.root/"local.env";env.write_text("OTHRYSS_ALERT_DISCORD_URL="+DISCORD["url_env"])
        with patch.dict("os.environ",{},clear=True):
            cycle(self.db,self.store.db,self.root,sender=lambda *_:{"status":"accepted"})
            self.opened()
            cycle(self.db,self.store.db,self.root,sender=lambda *_:{"status":"accepted"})
            self.assertEqual(self.rows()[0]["status"],"accepted")
            env.write_text("")
            cycle(self.db,self.store.db,self.root,sender=lambda *_:self.fail("must not send"))
            self.assertEqual(self.db.execute("SELECT active FROM routes").fetchone()[0],0)

    def test_adapter_exception_preserves_unknown_attempt(self):
        self.opened()
        def broken(*_):raise OSError("SECRET-CANARY")
        alerts.deliver_one(self.db,self.store.db,self.route,DISCORD,self.now,broken)
        self.assertEqual(self.rows()[0]["status"],"unknown")
        self.assertNotIn("SECRET-CANARY",json.dumps(alerts.read(self.path,self.scope)))

    def test_pruning_preserves_open_incident_context(self):
        self.opened();self.send();alerts.prune(self.db,self.store.db,self.now+31*86400)
        self.assertEqual(len(self.rows()),1)
        self.store.db.execute("UPDATE incidents SET status='resolved'");self.store.db.commit()
        alerts.prune(self.db,self.store.db,self.now+31*86400)
        self.assertEqual(self.rows(),[])


class Response:
    def __init__(self,code=200,body=b'{"id":"123"}',headers=None):self.status=code;self.body=body;self.headers=headers or {}
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def read(self,size):return self.body[:size]


class Opener:
    def __init__(self,response):self.response=response;self.requests=[]
    def open(self,request,timeout):
        self.requests.append(request)
        if isinstance(self.response,Exception):raise self.response
        return self.response


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.route=route("a"*64)
        self.event=alerts.envelope({"scope_id":"a"*64,"incident_id":"test","instrument_id":"@everyone", "rule":"source_stale","entity":"source","assessment":"unknown","last_seen":iso(time.time())},"opened",123)
    def test_discord_wait_mentions_and_response_id(self):
        opener=Opener(Response());result=alert_channels.send(self.route,DISCORD,self.event,"id",opener=opener)
        request=opener.requests[0]
        self.assertTrue(request.full_url.endswith("?wait=true"))
        self.assertEqual(json.loads(request.data)["allowed_mentions"],{"parse":[]})
        self.assertEqual(result,{"status":"accepted","http_status":200,"provider_id":"123"})
    def test_webhook_signature_exact_bytes_stable_id_new_timestamp(self):
        r=route("a"*64,"webhook","hook")
        request=alert_channels.request_for(r,WEBHOOK,self.event,"delivery-1",100)
        expected=hmac.new(b"x"*32,b"100."+request.data,hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("X-othryss-signature"),"sha256="+expected)
        self.assertEqual(request.get_header("Idempotency-key"),"delivery-1")
        self.assertEqual(json.loads(request.data)["delivery_id"],"delivery-1")
        self.assertEqual(alert_channels.request_for(r,WEBHOOK,self.event,"delivery-1",101).data,request.data)
    def test_transient_permanent_ambiguous_and_malformed_responses(self):
        for kind,secrets in [("discord",DISCORD),("webhook",WEBHOOK)]:
            r=route("a"*64,kind,"test")
            for response,status in [(Response(401),"failed"),(Response(302),"failed"),(Response(429,b'{"retry_after":2.5}'),"retry"),
                                    (URLError("SECRET-CANARY"),"retry" if kind=="webhook" else "unknown"),
                                    (Response(503),"retry" if kind=="webhook" else "unknown")]:
                result=alert_channels.send(r,secrets,self.event,"id",opener=Opener(response))
                self.assertEqual(result["status"],status);self.assertNotIn("SECRET-CANARY",json.dumps(result))
        self.assertEqual(alert_channels.send(self.route,DISCORD,self.event,"id",opener=Opener(Response(200,b'{}')))["status"],"unknown")
    def test_retry_after_numeric_date_and_bad_value(self):
        self.assertEqual(alert_channels.retry_after({},b'{"retry_after":2.5}',100),3)
        self.assertEqual(alert_channels.retry_after({"Retry-After":"Thu, 01 Jan 1970 00:03:20 GMT"},b'',100),100)
        self.assertIsNone(alert_channels.retry_after({"Retry-After":"NaN"},b'',100))
    def test_configuration_and_destinations_fail_closed(self):
        for url in ["http://example.com", "https://user:pass@example.com/", "https://discord.com.evil/api/webhooks/1/token"]:
            with self.assertRaises(ValueError):alert_channels.validate_destination("discord",{"url_env":url})
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"alerts.json"
            for change in [{"enabled":"true"},{"scope_id":""},{"destination":{"url_env":"SECRET VALUE"}},{"kind":"sms"},{"reminder_seconds":1}]:
                path.write_text(json.dumps({"version":1,"routes":[self.route|change]}))
                with self.assertRaises(ValueError):alerts.configuration(path)
            path.write_text(json.dumps({"version":1,"routes":[self.route]}))
            self.assertEqual(alerts.configuration(path),[self.route])
    def test_only_referenced_environment_values_loaded(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict("os.environ",{},clear=True):
            path=Path(temp)/"local.env"
            path.write_text("OTHRYSS_KALSHI_KEY_ID=DO-NOT-READ\nOTHRYSS_ALERT_DISCORD_URL='"+DISCORD["url_env"]+"'\n")
            self.assertEqual(alerts.secrets_for(self.route,path),DISCORD)


if __name__=="__main__":unittest.main()
