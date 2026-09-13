"""Seeded discrepancies and timing/lifecycle guards use synthetic evidence only."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from othryss.reconciliation import compare, configure_target, collect, pages, read
from othryss.storage import Store
from test_collector import SyncClient


def snapshot(identity, minute, qty="0"):
    return {"snapshot_id": identity, "scope_id": "scope", "instrument_id": "TEST-MARKET", "subaccount": 0,
            "request_started_at": f"2026-09-09T12:{minute}:00+00:00", "received_at": f"2026-09-09T12:{minute}:01+00:00", "quantity": qty,
            "evidence": {"market": {"status": "active", "market_type": "binary"}, "settlements": []}}


def fill(identity="fill", quantity="49.32", direction="increase_yes", minute="05", subaccount=0):
    return {"event_id":identity, "type":"ORDER_FILL", "scope_id":"scope", "instrument_id":"TEST-MARKET",
            "occurred_at":f"2026-09-09T12:{minute}:00+00:00", "payload": {"order_id":"entry", "fill_id":identity, "quantity":quantity,
            "subaccount":subaccount, "price_basis":"yes_outcome", "exposure_direction":direction}}


AUDIT = {"run_id":"audit", "status":"traversed", "started_at":"2026-09-09T12:13:00+00:00", "finished_at":"2026-09-09T12:13:02+00:00"}


class ComparisonTests(unittest.TestCase):
    def check(self, baseline=None, target=None, fills=None, audit=None):
        return compare(baseline or snapshot("base", "00"), target or snapshot("later", "10", "49.32"), fills if fills is not None else [fill()], audit=audit or AUDIT)

    def test_exact_fractional_fills_and_short_exposure(self):
        result = self.check()
        self.assertEqual(result["status"], "consistent")
        self.assertEqual(result["expected_quantity"], "49.32")
        self.assertEqual(result["difference"], "0")
        short = self.check(target=snapshot("later", "10", "-49.32"), fills=[fill(direction="decrease_yes")])
        self.assertEqual(short["status"], "consistent")

    def test_fixed_baseline_and_partial_exit(self):
        result = self.check(baseline=snapshot("base", "00", "10"), target=snapshot("later", "10", "50"), fills=[fill(), fill("exit", "9.32", "decrease_yes", "06")])
        self.assertEqual(result["net_fill_quantity"], "40")
        self.assertEqual(result["status"], "consistent")

    def test_seeded_discrepancy_retains_evidence_and_does_not_claim_cause(self):
        result = self.check(target=snapshot("later", "10", "50"))
        self.assertEqual(result["status"], "unexplained_difference")
        self.assertEqual(result["difference"], "0.68")
        self.assertEqual(result["fills"][0]["payload"]["fill_id"], "fill")
        self.assertIn("do not identify a cause", result["interpretation"])

    def test_late_fill_repair_recomputes_difference(self):
        self.assertEqual(self.check(fills=[])["status"], "unexplained_difference")
        self.assertEqual(self.check(fills=[fill()])["status"], "consistent")

    def test_fill_at_either_boundary_is_pending_even_if_numbers_match(self):
        for minute in ("00", "10"):
            self.assertEqual(self.check(fills=[fill(minute=minute)])["status"], "pending_timing")

    def test_visibility_grace_and_incomplete_audit_block_comparison(self):
        self.assertEqual(self.check(audit=AUDIT | {"started_at":"2026-09-09T12:11:00+00:00"})["status"], "waiting_fills")
        self.assertEqual(self.check(audit=AUDIT | {"status":"paused"})["status"], "waiting_fills")

    def test_missing_row_never_becomes_zero(self):
        self.assertEqual(self.check(target=snapshot("later", "10", None))["status"], "unavailable")
        self.assertEqual(self.check(target=snapshot("later", "10", "0"), fills=[])["status"], "consistent")

    def test_settlement_and_closed_market_do_not_raise_mismatch(self):
        target = snapshot("later", "10", "0")
        target["evidence"]["settlements"] = [{"settled_time":"2026-09-09T12:06:00+00:00"}]
        self.assertEqual(self.check(target=target)["status"], "lifecycle_blocked")
        target["evidence"]["settlements"] = []
        target["evidence"]["market"]["status"] = "finalized"
        self.assertEqual(self.check(target=target)["status"], "lifecycle_blocked")

    def test_subaccounts_are_not_merged_and_unknown_scope_blocks(self):
        self.assertEqual(self.check(fills=[fill(), fill("other", "99", subaccount=1)])["status"], "consistent")
        self.assertEqual(self.check(fills=[fill(subaccount=None)])["status"], "unavailable")
        wrong = fill(); wrong["scope_id"] = "other"
        with self.assertRaises(ValueError):
            self.check(fills=[wrong])

    def test_duplicates_do_not_inflate_and_conflicting_fills_fail(self):
        self.assertEqual(self.check(fills=[fill(), fill()])["fill_count"], 1)
        with self.assertRaises(ValueError):
            self.check(fills=[fill(), fill(quantity="99")])


class PositionClient(SyncClient):
    def request(self, endpoint, params=None):
        if endpoint == "/portfolio/positions":
            return {"market_positions":[{"ticker":"TEST-MARKET", "position_fp":"0.00", "last_updated_ts":"2026-09-09T12:00:00Z", "secret":"discard"}], "cursor":""}
        if endpoint == "/portfolio/settlements":
            return {"settlements":[], "cursor":""}
        if endpoint == "/markets/TEST-MARKET":
            return {"market":{"ticker":"TEST-MARKET", "status":"active", "market_type":"binary", "secret":"discard"}}
        return super().request(endpoint, params)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "recon.sqlite"
        self.store = Store(self.path)
        self.scope = self.store.bind_account("local", "kalshi", "demo", "test", "key")
        configure_target(self.store, self.scope, "TEST-MARKET")
        self.client = PositionClient()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_capture_persists_explicit_zero_and_allowlisted_evidence(self):
        result = collect(self.store, self.client, self.scope, "test")
        self.assertEqual(result["status"], "waiting_baseline")
        self.assertEqual(result["latest_capture"]["quantity"], "0")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(read(self.store.db, self.scope)["result"]["status"], "waiting_baseline")
        self.assertEqual(len(read(self.store.db, self.scope)["recent_checks"]), 1)

    def test_settlement_failure_does_not_commit_an_incomplete_snapshot(self):
        original = self.client.request
        def request(endpoint, params=None):
            if endpoint == "/portfolio/settlements":
                return {"settlements":[]}  # Missing cursor is incomplete evidence.
            return original(endpoint, params)
        self.client.request = request
        with self.assertRaises(ValueError):
            collect(self.store, self.client, self.scope, "test")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM position_observations").fetchone()[0], 0)
        self.assertIsNotNone(read(self.store.db, self.scope)["target"]["last_error"])

    def test_cursor_cycle_and_partial_pages_are_rejected(self):
        self.client.request = lambda *_: {"market_positions":[], "cursor":"repeat"}
        with self.assertRaisesRegex(ValueError, "cursor cycle"):
            pages(self.client, "/portfolio/positions", "market_positions", {})

    def test_target_is_durable_and_cannot_silently_change_or_cross_account(self):
        collect(self.store, self.client, self.scope, "test")
        check_id = read(self.store.db, self.scope)["recent_checks"][0]["check_id"]
        self.store.close(); self.store = Store(self.path)
        self.assertEqual(read(self.store.db, self.scope, check_id)["target"]["instrument_id"], "TEST-MARKET")
        with self.assertRaises(ValueError):
            configure_target(self.store, self.scope, "OTHER")
        other = self.store.bind_account("local", "kalshi", "demo", "other", "other-key")
        configure_target(self.store, other, "TEST-MARKET")
        with self.assertRaises(ValueError):
            read(self.store.db, other, check_id)

    def test_wrong_position_scope_rejected(self):
        original = self.client.request
        def request(endpoint, params=None):
            if endpoint == "/portfolio/positions":
                return {"market_positions":[{"ticker":"WRONG", "position_fp":"0"}], "cursor":""}
            return original(endpoint, params)
        self.client.request = request
        with self.assertRaises(ValueError):
            collect(self.store, self.client, self.scope, "test")

    def test_mature_baseline_survives_restart_and_later_snapshot_reconciles(self):
        def at(minute):
            now = f"2026-09-09T12:{minute}:00.000000+00:00"
            report = AUDIT | {"started_at": now, "finished_at": now}
            with patch("othryss.reconciliation.utc_now", return_value=now), patch("othryss.reconciliation.run_import", return_value=report):
                return collect(self.store, self.client, self.scope, "test")
        self.assertEqual(at("00")["status"], "waiting_baseline")
        self.assertEqual(at("03")["status"], "waiting_snapshot")
        baseline_id = read(self.store.db, self.scope)["target"]["baseline_id"]
        self.store.close(); self.store = Store(self.path)
        result = at("06")
        self.assertEqual(result["status"], "consistent")
        self.assertEqual(result["baseline"]["snapshot_id"], baseline_id)
        self.assertEqual(result["expected_quantity"], "0")
        self.assertEqual(len(read(self.store.db, self.scope)["recent_checks"]), 3)

    def test_capture_denies_wrong_key_subaccount_before_api_reads(self):
        self.client.subaccount = 1
        with self.assertRaisesRegex(ValueError, "outside"):
            collect(self.store, self.client, self.scope, "test")
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
