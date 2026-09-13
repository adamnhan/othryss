"""Repeatable setup and readiness gates use isolated state and no venue requests."""
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from othryss import onboarding
from othryss.collector import configure, update
from othryss.storage import Store


class Client:
    calls = 0
    def __init__(self, key_id, path, environment):
        self.fingerprint = hashlib.sha256(key_id.encode()).hexdigest()
    def verify_read_only(self):
        Client.calls += 1
        return {"scopes": ["read"], "subaccount": None}


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime.now(timezone.utc)
        Client.calls = 0
        self.deps = patch("othryss.onboarding.importlib.metadata.version", side_effect=lambda name: {"cryptography":"46.0.0", "websockets":"16.0"}[name])
        self.deps.start(); self.addCleanup(self.deps.stop)

    def init(self):
        return onboarding.initialize(self.root, "customer-one", "demo")

    def credentials(self):
        (self.root / "local.env").write_text("OTHRYSS_KALSHI_KEY_ID=SECRET-CANARY\nOTHRYSS_KALSHI_PRIVATE_KEY_PATH=secret-key.pem\n", encoding="utf-8")

    def inspect(self, **kwargs):
        with patch.dict("os.environ", {}, clear=True):
            return onboarding.check(self.root, client_factory=Client, now=self.now, **kwargs)

    def by_name(self, report, name):
        return next(c for c in report["checks"] if c["name"] == name)

    def seed(self):
        self.init(); self.credentials()
        with Store(self.root / "artifacts/history/othryss.sqlite") as store:
            scope = store.bind_account("local", "kalshi", "demo", "customer-one", hashlib.sha256(b"SECRET-CANARY").hexdigest())
            configure(store, scope)
            update(store, scope, fill_watermark=int(self.now.timestamp()), last_success_at=self.now.isoformat(), heartbeat_at=self.now.isoformat(), status="idle", failure_count=0)
        path = self.root / "artifacts/ops/status.json"
        path.parent.mkdir(parents=True)
        self.state = {"status":"running", "heartbeat_at":self.now.isoformat(), "services":{name:{"status":"running"} for name in ("collector", "reference", "explorer", "alerts")}}
        path.write_text(json.dumps(self.state), encoding="utf-8")
        opener = Mock()
        opener.open.side_effect = lambda *a, **k: io.BytesIO(json.dumps({"accounts":[{"scope_id":scope}]}).encode())
        patcher = patch("othryss.onboarding.build_opener", return_value=opener)
        patcher.start(); self.addCleanup(patcher.stop)
        return scope

    def test_init_rerun_preserves_keys_alerts_and_custom_settings(self):
        self.assertEqual(set(self.init()["files"].values()), {"created"})
        self.credentials()
        path = self.root / "ops.local.json"
        settings = json.loads(path.read_text()); settings["backup_keep"] = 3; settings["port"] = 8799
        path.write_text(json.dumps(settings))
        before = {p.name:p.read_bytes() for p in self.root.iterdir()}
        self.assertEqual(set(self.init()["files"].values()), {"preserved"})
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.root.iterdir()})
        self.assertIsNone(settings["reconcile_ticker"])
        self.assertEqual(json.loads((self.root / "alerts.local.json").read_text())["routes"], [])

    def test_identity_conflict_does_not_create_missing_files(self):
        self.init(); (self.root / "local.env").unlink()
        for args in (("another", "demo", None), ("customer-one", "production", None), ("customer-one", "demo", 8800)):
            with self.assertRaises(ValueError): onboarding.initialize(self.root, *args)
        self.assertFalse((self.root / "local.env").exists())

    def test_partial_setup_can_be_resumed_without_replacing_secrets(self):
        self.credentials()
        before = (self.root / "local.env").read_bytes()
        result = self.init()
        self.assertEqual(result["files"]["local.env"], "preserved")
        self.assertEqual((self.root / "local.env").read_bytes(), before)

    def test_invalid_identity_creates_nothing(self):
        with self.assertRaises(ValueError): onboarding.initialize(self.root, "../wrong", "demo")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_configuration_never_creates_database(self):
        result = self.inspect()
        self.assertFalse(result["ready"])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_preflight_does_not_contact_venue_or_claim_live_readiness(self):
        self.init(); self.credentials()
        result = self.inspect()
        self.assertTrue(result["ready"])
        self.assertEqual(result["phase"], "preflight")
        self.assertEqual(Client.calls, 0)
        self.assertEqual(self.by_name(result,"live_evidence")["status"], "pending")
        self.assertNotIn("SECRET-CANARY", json.dumps(result))
        self.assertNotIn("secret-key.pem", json.dumps(result))
        self.assertFalse((self.root / "artifacts").exists())

    def test_bad_credentials_and_dependency_fail_preflight_without_secret_output(self):
        self.init(); self.credentials()
        with patch("othryss.onboarding.importlib.metadata.version", return_value="99.0"):
            result = onboarding.check(self.root, client_factory=Mock(side_effect=ValueError("SECRET-CANARY")))
        self.assertFalse(result["ready"])
        self.assertNotIn("SECRET-CANARY", json.dumps(result))
        self.assertEqual(self.by_name(result,"credentials")["status"], "fail")

    def test_empty_account_can_pass_base_live_checks_and_report_optional_gaps(self):
        self.seed()
        result = self.inspect(live=True)
        self.assertTrue(result["ready"], result)
        self.assertEqual(Client.calls, 1)
        self.assertEqual(self.by_name(result,"telemetry")["status"], "pending")
        self.assertEqual(self.by_name(result,"alert_delivery")["status"], "pending")
        self.assertIn("0 fills", self.by_name(result,"imported_evidence")["detail"])
        with Store(self.root / "artifacts/history/othryss.sqlite") as store:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertFalse((self.root / "artifacts/alerts/delivery.sqlite").exists())

    def test_requested_integrations_must_have_evidence(self):
        self.seed()
        for options in ({"require_telemetry":True}, {"require_alerts":True}):
            result = self.inspect(live=True, **options)
            self.assertFalse(result["ready"])

    def test_stale_or_future_supervisor_and_wrong_explorer_do_not_pass(self):
        self.seed()
        for delta in (-30, 30):
            self.state["heartbeat_at"] = (self.now + timedelta(seconds=delta)).isoformat()
            (self.root / "artifacts/ops/status.json").write_text(json.dumps(self.state))
            self.assertFalse(self.inspect(live=True)["ready"])
        self.state["heartbeat_at"] = self.now.isoformat()
        (self.root / "artifacts/ops/status.json").write_text(json.dumps(self.state))
        opener = Mock(); opener.open.return_value = io.BytesIO(b'{"accounts":[]}')
        with patch("othryss.onboarding.build_opener", return_value=opener):
            self.assertEqual(self.by_name(self.inspect(live=True), "explorer")["status"], "fail")

    def test_wrong_credential_binding_is_not_accepted(self):
        self.seed()
        with Store(self.root / "artifacts/history/othryss.sqlite") as store, store.db:
            store.db.execute("UPDATE accounts SET credential_fingerprint='different'")
        result = self.inspect(live=True)
        self.assertFalse(result["ready"])
        self.assertEqual(self.by_name(result,"account_binding")["status"], "fail")

    def test_failed_live_auth_is_sanitized_and_nonprimary_key_is_rejected(self):
        self.seed()
        with patch.object(Client,"verify_read_only",side_effect=RuntimeError("SECRET-CANARY")):
            result = self.inspect(live=True)
            self.assertFalse(result["ready"])
            self.assertNotIn("SECRET-CANARY",json.dumps(result))
        with patch.object(Client,"verify_read_only",return_value={"scopes":["read"],"subaccount":2}):
            self.assertEqual(self.by_name(self.inspect(live=True),"read_only_access")["status"],"fail")

    def test_stale_collection_fails_even_with_running_processes(self):
        self.seed()
        with Store(self.root / "artifacts/history/othryss.sqlite") as store, store.db:
            store.db.execute("UPDATE sync_state SET fill_watermark=fill_watermark-600")
        result = self.inspect(live=True)
        self.assertFalse(result["ready"])
        self.assertEqual(self.by_name(result,"collection")["status"],"pending")

    def test_alert_gate_requires_matching_activation_and_provider_acceptance(self):
        from othryss import alerts
        scope = self.seed()
        raw = {"id":"discord", "kind":"discord", "scope_id":scope, "enabled":True,
               "destination":{"url_env":"OTHRYSS_ALERT_DISCORD_URL"}}
        (self.root / "alerts.local.json").write_text(json.dumps({"version":1,"routes":[raw]}))
        route = alerts.configuration(self.root / "alerts.local.json")[0]
        secrets = {"url":"SECRET-DESTINATION"}
        db = alerts.connect(self.root / "artifacts/alerts/delivery.sqlite")
        self.addCleanup(db.close)
        at = self.now.timestamp()
        with db:
            db.execute("INSERT INTO worker VALUES (1,'running',?)",(at,))
            db.execute("INSERT INTO routes VALUES (?,?,?,?,?,1,NULL,NULL,?)",("discord",scope,"discord",alerts.digest([route,secrets]),0,at-10))
        with patch("othryss.onboarding.alerts.secrets_for",return_value=secrets):
            result = self.inspect(live=True,require_alerts=True)
            self.assertFalse(result["ready"])
            self.assertEqual(self.by_name(result,"alert_delivery")["status"],"pass")
            self.assertEqual(self.by_name(result,"alert_provider_acceptance")["status"],"pending")
            with db:
                db.execute("INSERT INTO deliveries(delivery_id,route_id,event_key,scope_id,incident_id,event,payload_json,created_at,due_at,expires_at,status) VALUES ('id','discord','event',?,'incident','opened','{}',?,?,?,'accepted')",(scope,at,at,at+300))
            result = self.inspect(live=True,require_alerts=True)
            self.assertTrue(result["ready"],result)
            self.assertNotIn("SECRET-DESTINATION",json.dumps(result))
            with db: db.execute("UPDATE routes SET fingerprint='previous-configuration'")
            self.assertFalse(self.inspect(live=True,require_alerts=True)["ready"])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],1)

    def test_report_refuses_to_overwrite_and_never_echoes_exception_contents(self):
        report = self.root / "report.json"; report.write_text("original")
        with patch("othryss.onboarding.ROOT",self.root), patch("othryss.onboarding.check",return_value={"ready":True}), patch("sys.stdout",new_callable=io.StringIO) as output:
            code = onboarding.main(["check","--report",str(report)])
            self.assertEqual(code,1)
            self.assertEqual(report.read_text(),"original")
            self.assertNotIn(str(report),output.getvalue())


if __name__ == "__main__":
    unittest.main()
