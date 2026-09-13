"""Small outbound adapters. No exchange credentials or trading client."""
import hashlib
import hmac
import json
import math
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .kalshi_client import NoRedirect
from .storage import encode


def validate_destination(kind, secrets):
    url = urlsplit(secrets["url_env"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise ValueError("Alert endpoint must be HTTPS without user info or fragment")
    if kind == "discord" and (url.hostname != "discord.com" or url.port not in (None,443) or url.query or
            not re.fullmatch(r"/api(?:/v10)?/webhooks/[0-9]+/[A-Za-z0-9_.-]+", url.path)):
        raise ValueError("Expected a Discord incoming webhook URL")
    if kind == "webhook" and len(secrets["signing_secret_env"]) < 32:
        raise ValueError("Webhook signing secret must contain at least 32 characters")


def message(event):
    incident = event["incident"]
    return (f"Othryss {event['event'].upper()}: {incident['rule'].replace('_',' ')}\n"
            f"{incident['instrument_id']}\n{incident['summary']}\n"
            f"Assessment: {incident['assessment']}; checked {incident['checked_at']}\n"
            f"Incident {incident['incident_id']} · Review in local Othryss.")


def request_for(route, secrets, event, delivery_id, now=None):
    validate_destination(route["kind"], secrets)
    kind = route["kind"]
    headers = {"User-Agent": "Othryss-Alerts/1", "Accept": "application/json"}
    if kind == "discord":
        url = secrets["url_env"] + "?wait=true"
        body = encode({"content": message(event)[:1900], "allowed_mentions": {"parse": []}}).encode()
        headers["Content-Type"] = "application/json"
    else:
        url = secrets["url_env"]
        body = encode(dict(event, delivery_id=delivery_id)).encode()
        stamp = str(int(time.time() if now is None else now))
        signature = hmac.new(secrets["signing_secret_env"].encode(), stamp.encode()+b"."+body, hashlib.sha256).hexdigest()
        headers.update({"Content-Type": "application/json", "Idempotency-Key": delivery_id,
                        "X-Othryss-Timestamp": stamp, "X-Othryss-Signature": "sha256="+signature})
    return Request(url, data=body, headers=headers, method="POST")


def retry_after(headers, body, now):
    value = headers.get("Retry-After")
    if value is None:
        try: value = json.loads(body).get("retry_after")
        except (ValueError, AttributeError): pass
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try: delay = (parsedate_to_datetime(value)-datetime.fromtimestamp(now,timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError): return None
    return max(1, math.ceil(delay)) if math.isfinite(delay) else None


def send(route, secrets, event, delivery_id, *, opener=None, now=None):
    """Return sanitized outcomes; never return request URLs, headers, or raw errors."""
    now = time.time() if now is None else now
    try:
        request = request_for(route, secrets, event, delivery_id, now)
    except (ValueError, KeyError):
        return {"status": "failed", "error": "invalid_destination"}
    try:
        with (opener or build_opener(NoRedirect())).open(request, timeout=10) as response:
            code, headers, body = response.status, response.headers, response.read(65537)
    except HTTPError as exc:
        code, headers, body = exc.code, exc.headers, exc.read(65537)
        exc.close()
    except Exception:
        return {"status": "retry" if route["kind"] == "webhook" else "unknown", "error": "transport_outcome_unknown"}
    if code == 429:
        return {"status": "retry", "http_status": code, "error": "rate_limited", "retry_after": retry_after(headers, body, now)}
    if 500 <= code <= 599 or code == 408:
        return {"status": "retry" if route["kind"] == "webhook" else "unknown", "http_status": code, "error": "provider_outcome_unknown"}
    if not 200 <= code < 300:
        return {"status": "failed", "http_status": code, "error": "provider_rejected"}
    result = {"status": "accepted", "http_status": code}
    if route["kind"] != "webhook":
        try:
            parsed = json.loads(body)
            provider_id = parsed["id"]
            if not isinstance(provider_id,str) or not re.fullmatch(r"[A-Za-z0-9]{1,80}", provider_id): raise ValueError()
            result["provider_id"] = provider_id
        except (KeyError, ValueError, TypeError):
            return {"status": "unknown", "http_status": code, "error": "unverified_acceptance"}
    return result
