import base64
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlparse

from othryss.credentials import credentials
from othryss.kalshi_client import ImportRequestError, KalshiClient, NoRedirect


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "Install requirements-import.txt for signing checks")
class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.temp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.temp.name) / "test.pem"
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.path.write_bytes(cls.key.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def client(self, opener, sleep=lambda _: None):
        return KalshiClient("test-key-id", self.path, "demo", opener=opener, sleep=sleep, interval=0)

    def test_signature_is_get_path_without_query_and_never_writes(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        opener = Opener([{"fills": [], "cursor": ""}])
        client = self.client(opener)
        client.request("/portfolio/fills", {"ticker": "A&B", "cursor": "opaque+cursor="})
        request = opener.requests[0]
        headers = {key.lower(): value for key, value in request.headers.items()}
        message = headers["kalshi-access-timestamp"] + "GET" + urlparse(request.full_url).path
        self.key.public_key().verify(base64.b64decode(headers["kalshi-access-signature"]), message.encode(),
                                     padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("ticker=A%26B", request.full_url)
        for endpoint in ("/portfolio/events/orders", "https://example.com", "/api_keys/generate", "/portfolio/orders/123"):
            with self.assertRaises(ValueError):
                client.request(endpoint)
        with self.assertRaises(ValueError):
            client.request("/portfolio/orders", {"api_key": "not-a-query-option"})
        self.assertEqual(len(opener.requests), 1)

    def test_read_scope_verification_fails_closed(self):
        for scopes in (["read", "write"], [], None, ["write"]):
            with self.subTest(scopes=scopes):
                client = self.client(Opener([{"api_keys": [{"api_key_id": "test-key-id", "scopes": scopes}]}]))
                with self.assertRaisesRegex(ImportRequestError, "read-only"):
                    client.verify_read_only()
        client = self.client(Opener([{"api_keys": [{"api_key_id": "test-key-id", "scopes": ["read"], "subaccount": 0}]}]))
        self.assertEqual(client.verify_read_only(), {"scopes": ["read"], "subaccount": 0})

    def test_time_bounds_are_allowed_only_on_current_fills(self):
        opener = Opener([{"fills": [], "cursor": ""}])
        client = self.client(opener)
        client.request("/portfolio/fills", {"min_ts": 123, "max_ts": 456})
        self.assertIn("min_ts=123", opener.requests[0].full_url)
        for endpoint in ("/portfolio/orders", "/historical/fills", "/historical/orders"):
            with self.assertRaises(ValueError):
                client.request(endpoint, {"min_ts": 123})

    def test_position_reads_are_narrow_and_market_paths_cannot_escape(self):
        opener = Opener([{}, {}, {}])
        client = self.client(opener)
        client.request("/portfolio/positions", {"ticker":"TEST", "subaccount":0, "count_filter":"position,total_traded"})
        client.request("/portfolio/settlements", {"ticker":"TEST", "subaccount":0})
        client.request("/markets/TEST-MARKET")
        self.assertTrue(all(r.get_method() == "GET" for r in opener.requests))
        for path in ("/markets/../portfolio/orders", "/markets/TEST?secret=x", "/markets/TEST/subpath"):
            with self.assertRaises(ValueError):
                client.request(path)

    def test_auth_error_is_not_retried_and_does_not_echo_body(self):
        error = HTTPError("url", 401, "unauthorized", {}, io.BytesIO(b"SECRET-SENTINEL"))
        opener = Opener([error])
        with self.assertRaises(ImportRequestError) as raised:
            self.client(opener).request("/portfolio/fills")
        self.assertIn("HTTP 401", str(raised.exception))
        self.assertNotIn("SECRET-SENTINEL", str(raised.exception))
        self.assertEqual(len(opener.requests), 1)

    def test_balance_read_is_primary_scoped_and_rejects_unrelated_parameters(self):
        opener=Opener([{"balance":100,"portfolio_value":0,"updated_ts":123}])
        client=self.client(opener)
        client.request("/portfolio/balance",{"subaccount":0})
        self.assertEqual(opener.requests[0].get_method(),"GET")
        self.assertIn("subaccount=0",opener.requests[0].full_url)
        for params in ({"ticker":"TEST"},{"amount":100},{"subaccount":True},{"subaccount":-1},{"subaccount":33}):
            with self.assertRaises(ValueError):client.request("/portfolio/balance",params)

    def test_rate_limit_and_server_failure_are_retried_with_a_bound(self):
        waits = []
        opener = Opener([HTTPError("url", 429, "limited", {"Retry-After": "2"}, None),
                         HTTPError("url", 503, "busy", {}, None), {"fills": [], "cursor": ""}])
        result = self.client(opener, waits.append).request("/portfolio/fills")
        self.assertEqual(result["fills"], [])
        self.assertIn(2, waits)
        self.assertEqual(len(opener.requests), 3)
        opener = Opener([HTTPError("url", 503, "busy", {}, None) for _ in range(4)])
        with self.assertRaises(ImportRequestError):
            self.client(opener).request("/portfolio/fills")
        self.assertEqual(len(opener.requests), 4)

    def test_long_retry_delay_and_redirect_do_not_trigger_more_requests(self):
        for code, headers in ((429, {"Retry-After": "120"}), (302, {"Location": "https://example.com"})):
            opener = Opener([HTTPError("url", code, "message", headers, None)])
            with self.assertRaises(ImportRequestError):
                self.client(opener).request("/portfolio/orders")
            self.assertEqual(len(opener.requests), 1)
        self.assertIsNone(NoRedirect().redirect_request(None))


class CredentialsTests(unittest.TestCase):
    def test_local_env_supports_quoted_paths_and_does_not_execute_values(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            path = Path(tmp) / "local.env"
            path.write_text('# comment\nOTHRYSS_KALSHI_KEY_ID=test-id\nOTHRYSS_KALSHI_PRIVATE_KEY_PATH="keys/my key.pem"\nUNRELATED_SECRET=ignored\n', encoding="utf-8")
            key_id, key_path = credentials(path)
            self.assertEqual(key_id, "test-id")
            self.assertEqual(key_path, Path(tmp) / "keys/my key.pem")
            self.assertNotIn("OTHRYSS_KALSHI_KEY_ID", os.environ)

    def test_environment_overrides_file_and_missing_values_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.env"
            path.write_text('OTHRYSS_KALSHI_KEY_ID=file-id\nOTHRYSS_KALSHI_PRIVATE_KEY_PATH=file.pem\n')
            with patch.dict(os.environ, {"OTHRYSS_KALSHI_KEY_ID": "env-id"}, clear=True):
                self.assertEqual(credentials(path)[0], "env-id")
            path.write_text('OTHRYSS_KALSHI_KEY_ID=\nOTHRYSS_KALSHI_PRIVATE_KEY_PATH=\n')
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, "local.env"):
                credentials(path)


if __name__ == "__main__":
    unittest.main()
