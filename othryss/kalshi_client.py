"""Authenticated GET-only client with bounded retries and no redirect following."""

import base64
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOSTS = {"production": "https://external-api.kalshi.com", "demo": "https://external-api.demo.kalshi.co"}
ALLOWED = {"/api_keys", "/historical/cutoff", "/portfolio/orders", "/portfolio/fills", "/historical/orders", "/historical/fills", "/portfolio/positions", "/portfolio/settlements", "/portfolio/balance"}


class ImportRequestError(RuntimeError):
    def __init__(self, message, *, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class KalshiClient:
    def __init__(self, key_id, private_key_path, environment="production", *, opener=None, sleep=time.sleep, interval=0.5):
        if environment not in HOSTS or not key_id:
            raise ValueError("A supported environment and configured key ID are required")
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
        except ImportError as exc:
            raise ValueError("Install the importer dependency: python -m pip install -r requirements-import.txt") from exc
        self.key = serialization.load_pem_private_key(Path(private_key_path).read_bytes(), password=None)
        if not isinstance(self.key, RSAPrivateKey):
            raise ValueError("Expected an RSA private key")
        self.key_id = key_id
        self.fingerprint = hashlib.sha256(key_id.encode()).hexdigest()
        self.environment = environment
        self.opener = opener or build_opener(NoRedirect())
        self.sleep, self.interval = sleep, interval
        self.last_request = None

    def request(self, endpoint, params=None):
        # No arbitrary URLs, methods, credentials in query strings, or write endpoints.
        market_request = re.fullmatch(r"/markets/[A-Za-z0-9_.:-]{1,200}", endpoint) is not None
        if endpoint not in ALLOWED and not market_request:
            raise ValueError("Endpoint is outside the historical read-only allowlist")
        params = params or {}
        permitted = {"limit", "cursor", "ticker"} if endpoint not in {"/api_keys", "/historical/cutoff"} else set()
        if endpoint == "/portfolio/fills":
            permitted |= {"min_ts", "max_ts"}
        if endpoint == "/portfolio/positions":
            permitted |= {"subaccount", "count_filter"}
        if endpoint == "/portfolio/balance":
            permitted = {"subaccount"}
            if type(params.get("subaccount",0)) is not int or not 0 <= params.get("subaccount",0) <= 32:
                raise ValueError("Invalid balance subaccount")
        if endpoint == "/portfolio/settlements":
            permitted |= {"subaccount", "min_ts", "max_ts"}
        if market_request:
            permitted = set()
        if set(params) - permitted:
            raise ValueError("Unsupported history query parameter")
        path = "/trade-api/v2" + endpoint
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        for attempt in range(4):
            if self.last_request is not None:
                self.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            stamp = str(time.time_ns() // 1_000_000)
            signature = self.key.sign((stamp + "GET" + path).encode(),
                                      padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
                                      hashes.SHA256())
            url = HOSTS[self.environment] + path + ("?" + urlencode(params) if params else "")
            request = Request(url, method="GET", headers={
                "KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": stamp,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
                "Accept": "application/json", "User-Agent": "Othryss-History/0.1",
            })
            self.last_request = time.monotonic()
            try:
                with self.opener.open(request, timeout=20) as response:
                    payload = json.loads(response.read())
                    if not isinstance(payload, dict):
                        raise ImportRequestError("Expected a JSON object from history endpoint")
                    return payload
            except HTTPError as exc:
                code, retry_after = exc.code, exc.headers.get("Retry-After")
                exc.close()
                if code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise ImportRequestError(f"Kalshi GET {endpoint} returned HTTP {code}; checkpoint retained", http_status=code) from None
                wait = 0.5 * 2 ** attempt
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        try:
                            wait = max(wait, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError):
                            pass
                if wait > 30:
                    raise ImportRequestError("Rate limit requests a longer pause; resume the import later") from None
                self.sleep(wait)
            except (URLError, TimeoutError, ConnectionError):
                if attempt == 3:
                    raise ImportRequestError("History request failed after bounded retries; checkpoint retained") from None
                self.sleep(0.5 * 2 ** attempt)
            except (json.JSONDecodeError, UnicodeError):
                raise ImportRequestError("History endpoint returned invalid JSON; checkpoint retained") from None

    def verify_read_only(self):
        payload = self.request("/api_keys")
        matches = [key for key in payload.get("api_keys", []) if key.get("api_key_id") == self.key_id]
        if len(matches) != 1 or set(matches[0].get("scopes") or []) != {"read"}:
            raise ImportRequestError("The configured key could not be verified as read-only; use a dedicated key with only the read scope")
        key = matches[0]
        if key.get("fcm_subtrader_id"):
            raise ImportRequestError("FCM subtrader keys are outside this importer's initial scope")
        return {"scopes": ["read"], "subaccount": key.get("subaccount")}
