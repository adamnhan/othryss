"""Deterministic recovery tests; never submit orders or access real credentials."""

import json
import io
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from othryss.collector import configure, cycle, epoch, freshness, retry_delay, state, update
from othryss.collector_cli import main
from othryss.kalshi_client import ImportRequestError
from othryss.storage import Store, import_lock
from test_history import FakeClient, BUY

NOW = datetime(2026, 8, 18, 14, 20, tzinfo=timezone.utc)


class SyncClient(FakeClient):
    fingerprint = "key"
    def verify_read_only(self):
        if getattr(self, "deny", False):
            raise ImportRequestError("Read-only verification failed")
        return {"subaccount": getattr(self, "subaccount", None)}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite"
        self.store = Store(self.path)
        self.scope = self.store.bind_account("local", "kalshi", "demo", "test", "key")
        configure(self.store, self.scope)
        self.client = SyncClient()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_cycle(self, **kwargs):
        return cycle(self.store, self.client, self.scope, "test", now=kwargs.pop("now", NOW), **kwargs)

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def test_bootstrap_then_overlap_catches_late_fills_and_old_order_changes(self):
        first = self.run_cycle()
        self.assertEqual(first["config"]["mode"], "full")
        self.assertEqual(state(self.store, self.scope)["fill_watermark"], int(NOW.timestamp()))
        late = {**BUY, "fill_id": "late", "created_time": "2026-08-18T14:19:00Z"}
        self.client.pages[("/portfolio/fills", "next")]["fills"].append(late)
        self.client.pages[("/portfolio/orders", "")]["orders"][0]["remaining_count_fp"] = "0.00"
        self.client.calls.clear()
        result = self.run_cycle(now=NOW + timedelta(minutes=1))
        self.assertEqual(result["config"]["mode"], "incremental")
        requests = [params for endpoint, params in self.client.calls if endpoint == "/portfolio/fills"]
        self.assertTrue(all(p["min_ts"] == int(NOW.timestamp()) - 300 for p in requests))
        self.assertTrue(all(p["max_ts"] == int(NOW.timestamp()) + 60 for p in requests))
        self.assertFalse(any(endpoint.startswith("/historical/") and endpoint != "/historical/cutoff" for endpoint, _ in self.client.calls))
        self.assertNotIn("min_ts", next(params for endpoint, params in self.client.calls if endpoint == "/portfolio/orders"))
        self.assertEqual(result["stored_events_in_scope"], 5)  # 3 initial + late execution + changed snapshot

    def test_pause_reopen_resume_keeps_original_window_and_watermark(self):
        self.run_cycle()
        old = state(self.store, self.scope)["fill_watermark"]
        paused = self.run_cycle(now=NOW + timedelta(minutes=1), max_pages=2)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(state(self.store, self.scope)["fill_watermark"], old)
        self.reopen()
        self.client.calls.clear()
        done = self.run_cycle(now=NOW + timedelta(hours=1))
        self.assertEqual(done["run_id"], paused["run_id"])
        self.assertEqual(state(self.store, self.scope)["fill_watermark"], old + 60)
        fill_request = next(p for e, p in self.client.calls if e == "/portfolio/fills")
        self.assertEqual(fill_request["cursor"], "next")
        self.assertEqual(fill_request["max_ts"], old + 60)

    def test_failed_page_retries_without_advancing_coverage(self):
        self.run_cycle()
        old = state(self.store, self.scope)["fill_watermark"]
        good = deepcopy(self.client.pages[("/portfolio/fills", "next")])
        self.client.pages[("/portfolio/fills", "next")] = ImportRequestError("network failure")
        with self.assertRaises(ImportRequestError):
            self.run_cycle(now=NOW + timedelta(minutes=1))
        failed = state(self.store, self.scope)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["fill_watermark"], old)
        self.assertEqual(retry_delay(failed), 60)
        self.client.pages[("/portfolio/fills", "next")] = good
        self.reopen()
        done = self.run_cycle(now=NOW + timedelta(minutes=3))
        self.assertEqual(done["run_id"], failed["active_run"])
        self.assertEqual(state(self.store, self.scope)["failure_count"], 0)

    def test_crash_between_run_completion_and_watermark_commit_is_recovered(self):
        with patch("othryss.collector.finish", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                self.run_cycle()
        active = state(self.store, self.scope)["active_run"]
        self.assertEqual(self.store.report(active)["status"], "traversed")
        self.assertIsNone(state(self.store, self.scope)["fill_watermark"])
        self.reopen()
        self.client.calls.clear()
        result = self.run_cycle(now=NOW + timedelta(minutes=2))
        self.assertEqual(result["run_id"], active)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(state(self.store, self.scope)["fill_watermark"], int(NOW.timestamp()))

    def test_periodic_archive_repair_and_cutoff_change_force_full_scan(self):
        self.run_cycle()
        full = self.run_cycle(now=NOW + timedelta(days=1))
        self.assertEqual(full["config"]["mode"], "full")
        self.client.cutoff["orders_updated_ts"] = "2026-07-01T00:00:00Z"
        self.assertEqual(self.run_cycle(now=NOW + timedelta(days=1, minutes=1))["config"]["mode"], "full")

    def test_moving_cutoff_does_not_advance_and_next_cycle_repairs(self):
        self.run_cycle()
        old = state(self.store, self.scope)["fill_watermark"]
        def move(value):
            if value.get("stream") == "/portfolio/fills":
                self.client.cutoff["orders_updated_ts"] = "2026-07-01T00:00:00Z"
        result = self.run_cycle(now=NOW + timedelta(minutes=1), progress=move)
        self.assertEqual(result["status"], "needs_rescan")
        self.assertEqual(state(self.store, self.scope)["fill_watermark"], old)
        self.assertEqual(self.run_cycle(now=NOW + timedelta(minutes=2))["config"]["mode"], "full")

    def test_expired_cursor_restarts_without_skipping_data(self):
        self.run_cycle()
        self.run_cycle(now=NOW + timedelta(minutes=1), max_pages=2)
        old = state(self.store, self.scope)["fill_watermark"]
        self.client.pages[("/portfolio/fills", "next")] = ImportRequestError("Bad cursor", http_status=400)
        with self.assertRaises(ImportRequestError):
            self.run_cycle(now=NOW + timedelta(minutes=2))
        self.assertIsNone(state(self.store, self.scope)["active_run"])
        self.assertEqual(state(self.store, self.scope)["fill_watermark"], old)
        self.client.pages[("/portfolio/fills", "next")] = {"fills": [], "cursor": ""}
        self.assertEqual(self.run_cycle(now=NOW + timedelta(minutes=3))["config"]["mode"], "full")

    def test_duplicate_poll_does_not_store_duplicate_payload_evidence(self):
        self.run_cycle()
        before = self.store.db.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0]
        again = self.run_cycle(now=NOW + timedelta(minutes=1))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0], before)
        pages = self.store.db.execute("SELECT evidence_json FROM pages WHERE run_id=?", (again["run_id"],))
        self.assertTrue(all(json.loads(p[0]) == {"retention": "new_events_only", "rows": []} for p in pages))
        self.assertGreater(sum(s["duplicates"] for s in again["streams"]), 0)

    def test_verification_failure_has_no_trading_history_requests(self):
        self.client.deny = True
        with self.assertRaises(ImportRequestError):
            self.run_cycle()
        self.assertEqual(self.client.calls, [])
        self.assertIsNone(state(self.store, self.scope)["fill_watermark"])
        self.assertEqual(state(self.store, self.scope)["status"], "error")

    def test_subaccount_change_rejected_even_between_completed_cycles(self):
        self.run_cycle()
        self.client.subaccount = 0
        self.client.calls.clear()
        with self.assertRaisesRegex(ValueError, "subaccount"):
            self.run_cycle()
        self.assertEqual(self.client.calls, [])

    def test_freshness_ages_without_new_events_or_worker_heartbeat(self):
        self.run_cycle()
        update(self.store, self.scope, heartbeat_at=NOW.isoformat())
        recent = freshness(self.store.db, self.scope, now=NOW + timedelta(seconds=60))
        self.assertEqual(recent["freshness"], "recent")
        stale = freshness(self.store.db, self.scope, now=NOW + timedelta(minutes=10))
        self.assertEqual(stale["freshness"], "stale")
        self.assertEqual(stale["worker_heartbeat"], "missing")

    def test_import_lock_prevents_two_workers(self):
        with import_lock(self.path):
            with self.assertRaises(ValueError):
                with import_lock(self.path):
                    self.fail("A second collector acquired the import lock")

    def test_cli_once_restarts_a_paused_run_and_records_stopped_worker(self):
        args = ["--db", str(self.path), "--account", "test", "--environment", "demo", "--once"]
        with patch("othryss.collector_cli.credentials", return_value=("key", None)), patch("othryss.collector_cli.KalshiClient", return_value=self.client), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(args + ["--max-pages", "1"]), 2)
            active = state(self.store, self.scope)["active_run"]
            self.assertEqual(state(self.store, self.scope)["status"], "stopped")
            self.assertEqual(main(args), 0)
        self.assertEqual(self.store.report(active)["status"], "traversed")
        self.assertIsNone(state(self.store, self.scope)["active_run"])

    def test_schema_one_migrates_without_changing_events_and_reader_can_open_it(self):
        self.run_cycle()
        count = self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        # Restore the pre-collector schema shape to exercise the real migration.
        self.store.db.executescript("DROP TABLE source_health; DROP TABLE account_snapshots; DROP TABLE risk_settings; DROP TABLE incident_actions; DROP TABLE incidents; DROP TABLE bot_monitors; DROP TABLE bot_checks; DROP TABLE bot_exchange; DROP TABLE telemetry_health; DROP TABLE telemetry_files; DROP TABLE telemetry_records; DROP TABLE sync_state; DROP INDEX imports_scope_started; DROP TABLE reconciliation_checks; DROP TABLE position_observations; DROP TABLE reconciliation_targets; DROP INDEX events_instrument_time; PRAGMA user_version=1;")
        self.reopen()
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 7)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], count)
        self.assertEqual(freshness(self.store.db, self.scope)["status"], "not_configured")

    def test_stop_file_stops_after_committed_page_and_restart_resumes(self):
        stop_path = Path(self.temp.name) / "collector.stop"
        original = self.client.request
        def request(endpoint, params=None):
            response = original(endpoint, params)
            if endpoint == "/portfolio/orders":
                stop_path.touch()
            return response
        self.client.request = request
        args = ["--db", str(self.path), "--account", "test", "--environment", "demo", "--stop-file", str(stop_path)]
        with patch("othryss.collector_cli.credentials", return_value=("key", None)), patch("othryss.collector_cli.KalshiClient", return_value=self.client), patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(args), 0)
            active = state(self.store, self.scope)["active_run"]
            self.assertEqual(self.store.checkpoint(active, "/portfolio/orders")["pages"], 1)
            self.assertEqual(state(self.store, self.scope)["status"], "stopped")
            self.assertIsNone(state(self.store, self.scope)["fill_watermark"])
            stop_path.unlink()
            self.client.request = original
            self.assertEqual(main(args + ["--once"]), 0)
        self.assertEqual(self.store.report(active)["status"], "traversed")

    def test_existing_full_import_can_seed_collection_but_incremental_cannot(self):
        full = self.run_cycle()
        self.run_cycle(now=NOW + timedelta(minutes=1))
        with self.store.db:
            self.store.db.execute("DELETE FROM sync_state WHERE scope_id=?", (self.scope,))
        seeded = configure(self.store, self.scope)
        self.assertEqual(seeded["fill_watermark"], epoch(full["started_at"]))
        self.assertIsNone(seeded["last_success_at"])
