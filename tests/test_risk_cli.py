import json
import tempfile
import unittest
from pathlib import Path

from othryss.account_risk import DEFAULT, settings
from othryss.risk_cli import apply_config
from othryss.storage import Store


class RiskConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "history.sqlite"
        self.file = Path(self.temp.name) / "limits.json"
        self.store = Store(self.path)
        self.scope = self.store.bind_account("local", "kalshi", "demo", "one", "key")
        self.other = self.store.bind_account("local", "kalshi", "demo", "two", "key")
        self.data = {"scope_id": self.scope, "subaccount": 0, "revision": 0,
                     "config": DEFAULT | {"enabled": True, "per_market_limit": "10.25"}}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self):
        self.file.write_text(json.dumps(self.data), encoding="utf-8-sig")

    def test_check_does_not_write_apply_preserves_exact_limits_and_account_scope(self):
        self.write()
        self.assertTrue(apply_config(self.path, self.file, check=True)["valid"])
        self.assertEqual(settings(self.store.db, self.scope)["revision"], 0)
        result = apply_config(self.path, self.file)
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["config"]["per_market_limit"], "10.25")
        self.assertEqual(settings(self.store.db, self.other)["revision"], 0)
        with self.assertRaisesRegex(ValueError, "Settings changed"):
            apply_config(self.path, self.file)

    def test_rejects_wrong_subaccount_unknown_scope_and_invalid_limit(self):
        for update in ({"subaccount": 1}, {"scope_id": "missing"},
                       {"config": DEFAULT | {"enabled": True, "total_limit": "-1"}}):
            original = self.data.copy()
            self.data.update(update)
            self.write()
            with self.assertRaises(ValueError):
                apply_config(self.path, self.file)
            self.data = original
        self.assertEqual(settings(self.store.db, self.scope)["revision"], 0)
