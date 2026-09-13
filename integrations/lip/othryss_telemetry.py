"""Othryss telemetry 0.3.1: optional, stdlib-only, bounded local spool for LIP probes.

This module never sends an exchange request. The wrapped bot retains its client,
arguments, retry policy, response objects and exceptions.
"""

import atexit
import functools
import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

VERSION = "0.3.1"
RESPONSE_FIELDS = ("order_id", "client_order_id", "status", "remaining_count_fp", "fill_count_fp")
REQUEST_FIELDS = ("ticker", "client_order_id", "side", "count", "price", "post_only", "time_in_force", "expiration_time")


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (str, int, float)):
        return str(value)[:256]
    return None


def pick(data, fields):
    return {key: scalar(data[key]) for key in fields if key in data} if isinstance(data, dict) else {}


class Publisher:
    def __init__(self, directory, *, account, environment, ticker, run_id, strategy="lip-incentives", subaccount=0,
                 queue_size=1024, max_bytes=32 * 1024 * 1024, segment_bytes=1024 * 1024):
        self.directory = Path(directory)
        self.session = uuid.uuid4().hex
        self.context = {"schema_version": VERSION, "producer": "lip-requote-probe", "session_id": self.session,
                        "account": account, "environment": environment, "workspace": "local", "instrument_id": ticker,
                        "strategy_id": strategy, "run_id": run_id, "subaccount": subaccount}
        self.queue = queue.Queue(maxsize=queue_size)
        self.max_bytes = max_bytes
        self.segment_bytes = min(segment_bytes, max_bytes)
        self.seq = self.dropped = self.write_failures = 0
        self.heartbeat_failures = 0
        self.last_error_stage = self.last_error_type = self.last_error_at = None
        self.last_write_at = None
        self.capped = False
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._writer, name="othryss-spool", daemon=True)
        self.thread.start()
        self.emit("PRODUCER_START", {})
        atexit.register(self.close)

    def emit(self, kind, payload):
        try:
            with self.lock:
                self.seq += 1
                event = self.context | {"event_id": f"{self.session}:{self.seq}", "sequence": self.seq,
                                        "occurred_at": utc(), "monotonic_ns": str(time.monotonic_ns()), "type": kind, "payload": payload}
                try:
                    self.queue.put_nowait(event)
                except queue.Full:
                    self.dropped += 1
        except Exception:
            # Observability must not become a trading dependency.
            pass

    def health(self):
        with self.lock:
            return self.context | {"heartbeat_at": utc(), "last_write_at": self.last_write_at,
                                   "last_sequence": self.seq, "dropped": self.dropped, "write_failures": self.write_failures,
                                   "heartbeat_failures": self.heartbeat_failures, "last_error_stage": self.last_error_stage,
                                   "last_error_type": self.last_error_type, "last_error_at": self.last_error_at,
                                   "queue_depth": self.queue.qsize(), "capped": self.capped, "stopped": self.stop.is_set()}

    def _publish_health(self):
        path = self.directory / f"{self.session}.health.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.health(), separators=(",", ":")), encoding="utf-8")
        # Windows readers may briefly hold the destination without delete sharing.
        # Retry only on the background writer; never delay the trading thread.
        for attempt in range(3):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.02)

    def _health_cycle(self):
        try:
            self._publish_health()
            return True
        except Exception as exc:
            with self.lock:
                self.heartbeat_failures += 1
                self.last_error_stage = "heartbeat"
                self.last_error_type = type(exc).__name__[:80]
                self.last_error_at = utc()
            return False

    def _writer(self):
        handle = None
        written = 0
        segment = 0
        segment_hash = hashlib.sha256()
        path = None
        last_health = 0.0
        def seal():
            nonlocal handle
            if handle is not None:
                handle.flush(); os.fsync(handle.fileno()); handle.close(); handle = None
                marker = path.with_name(path.name + ".closed")
                temporary = marker.with_name(marker.name + ".tmp")
                temporary.write_text(json.dumps({"size":written,"sha256":segment_hash.hexdigest()}),encoding="utf-8")
                os.replace(temporary,marker)
        try:
            while not self.stop.is_set() or not self.queue.empty():
                stage = "open"
                try:
                    if handle is None:
                        self.directory.mkdir(parents=True, exist_ok=True)
                        name = f"{self.session}.jsonl" if segment == 0 else f"{self.session}.{segment:06d}.jsonl"
                        path = self.directory / name
                        handle = path.open("xb")
                    try:
                        event = self.queue.get(timeout=0.25)
                    except queue.Empty:
                        event = None
                    if event is not None:
                        stage = "serialize"
                        data = (json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n").encode()
                        if written and written + len(data) > self.segment_bytes:
                            stage = "rotate"
                            seal(); segment += 1; written = 0; segment_hash = hashlib.sha256()
                            path = self.directory / f"{self.session}.{segment:06d}.jsonl"
                            handle = path.open("xb")
                        stage = "spool_budget"
                        retained = 0
                        for pending in self.directory.glob(f"{self.session}*.jsonl"):
                            try: retained += pending.stat().st_size
                            except FileNotFoundError: pass  # Collector-acknowledged reclamation.
                        if retained + len(data) > self.max_bytes or len(data) > 16384:
                            with self.lock:
                                self.dropped += 1
                                self.capped = True
                        else:
                            stage = "event_write"
                            handle.write(data)
                            handle.flush()
                            segment_hash.update(data)
                            written += len(data)
                            self.last_write_at = utc()
                            self.capped = False
                    if time.monotonic() - last_health >= 2 or self.stop.is_set():
                        stage = "flush"
                        handle.flush()
                        os.fsync(handle.fileno())
                        self._health_cycle()
                        last_health = time.monotonic()
                except Exception as exc:
                    with self.lock:
                        self.write_failures += 1
                        self.last_error_stage = stage
                        self.last_error_type = type(exc).__name__[:80]
                        self.last_error_at = utc()
                        # A failed write may have left a partial line; ingestion stops
                        # there, visibly. Never silently claim durable delivery.
                        self.dropped += 1
                    if self.stop.wait(0.5):
                        break
        finally:
            if handle is not None:
                try:
                    seal()
                except Exception:
                    try: handle.close()
                    except Exception: pass

    def close(self):
        if not self.stop.is_set():
            self.emit("PRODUCER_STOP", {})
            self.stop.set()
            self.thread.join(timeout=1)


def response_fields(response):
    result = {"http_status": int(response.status_code)}
    if 200 <= response.status_code < 300:
        try:
            body = response.json()
            order = body.get("order", body) if isinstance(body, dict) else {}
            result.update(pick(order, RESPONSE_FIELDS))
        except Exception:
            result["body_unavailable"] = True
    return result


def capture_state(probe, publisher, phase, step_ok):
    """Read only operational fields on the bot thread; never inspect its model."""
    try:
        def quantity(value):
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError("Nonfinite bot quantity")
            return format(result, "f")
        orders = []
        if probe.order_id:
            orders.append({"order_id": str(probe.order_id), "remaining": quantity(probe.order_size), "role": "entry"})
        if probe.exit_mgr.order_id:
            # ExitManager does not retain its remaining order quantity.
            orders.append({"order_id": str(probe.exit_mgr.order_id), "remaining": None, "role": "exit"})
        owned = sorted(str(i) for i in probe.owned_order_ids)
        fills = sorted({str(f["fill_id"]) for f in probe.fills})
        publisher.emit("BOT_STATE", {"phase": phase, "step_ok": step_ok,
            "position": quantity(Decimal(quantity(probe.baseline_position)) + Decimal(quantity(probe.inventory))),
            "baseline_position": quantity(probe.baseline_position), "inventory": quantity(probe.inventory),
            "position_basis": "baseline_plus_run_inventory_yes", "orders": orders,
            "owned_order_ids": owned[:64], "seen_fill_ids": fills[:64],
            "orders_complete": True, "ownership_complete": len(owned) <= 64, "fills_complete": len(fills) <= 64})
    except Exception:
        # An incomplete observation is never emitted as an empty snapshot.
        try:
            publisher.emit("BOT_STATE_UNAVAILABLE", {})
        except Exception:
            pass


def attach_probe(client, *, ticker, run_id, publisher=None, probe=None):
    """Attach to a single Probe-owned KX instance; disabled unless explicitly set."""
    try:
        if getattr(client, "_othryss_attached", False):
            return None
        if publisher is None:
            env = os.environ
            # Nonsecret per-install configuration survives watchdog replacements
            # and reboot. Environment variables can override it for a deployment.
            config = {}
            config_path = Path(__file__).with_name("othryss_telemetry.json")
            if config_path.exists():
                if config_path.stat().st_size > 4096:
                    return None
                config = json.loads(config_path.read_text(encoding="utf-8-sig"))
                if not isinstance(config, dict):
                    return None
            if env.get("OTHRYSS_TELEMETRY_DISABLED") == "1":
                return None
            directory = env.get("OTHRYSS_TELEMETRY_DIR", config.get("directory"))
            all_probes = env.get("OTHRYSS_LIP_ALL_PROBES", "1" if config.get("all_lip_probes") is True else "0") == "1"
            selected = env.get("OTHRYSS_TICKER", config.get("ticker"))
            if not directory or not (all_probes or selected == ticker):
                return None
            account = env.get("OTHRYSS_ACCOUNT", config.get("account"))
            environment = env.get("OTHRYSS_ENVIRONMENT", config.get("environment"))
            if not account or environment not in {"production", "demo"}:
                return None
            base = getattr(client.req, "__globals__", {}).get("REST", "")
            host = urlparse(base).hostname
            allowed = {"production": {"api.elections.kalshi.com", "external-api.kalshi.com"}, "demo": {"demo-api.kalshi.co"}}
            if host not in allowed[environment]:
                return None
            # This LIP probe omits subaccount in its writes, which means primary.
            publisher = Publisher(directory, account=account, environment=environment, ticker=ticker, run_id=run_id, subaccount=0)
        original_request, original_transport = client.req, client.sess.request
        context = threading.local()

        def safe_emit(kind, payload):
            try:
                publisher.emit(kind, payload)
            except Exception:
                pass

        @functools.wraps(original_transport)
        def transport(*args, **kwargs):
            current = getattr(context, "current", None)
            if current is None:
                return original_transport(*args, **kwargs)
            current["attempt"] += 1
            started = time.monotonic_ns()
            common = {"request_id": current["request_id"], "operation": current["operation"], "attempt": current["attempt"],
                      "order_id": current.get("order_id")}
            safe_emit("HTTP_ATTEMPT", common)
            try:
                response = original_transport(*args, **kwargs)
            except BaseException as exc:
                safe_emit("HTTP_RESPONSE", common | {"outcome": "unknown", "error_type": type(exc).__name__, "duration_ns": str(time.monotonic_ns() - started)})
                raise
            duration = str(time.monotonic_ns() - started)
            try:
                safe_emit("HTTP_RESPONSE", common | response_fields(response) | {"duration_ns": duration, "outcome": "response_received"})
            except Exception:
                pass
            return response

        @functools.wraps(original_request)
        def request(method, path, body=None, timeout=15):
            if isinstance(body, dict) and body.get("ticker", ticker) != ticker:
                return original_request(method, path, body=body, timeout=timeout)
            match = re.fullmatch(r"/portfolio/events/orders(?:/([A-Za-z0-9_-]+)(/amend)?)?", path)
            operation = "submit" if method.upper() == "POST" and path == "/portfolio/events/orders" else "amend" if match and match[2] and method.upper() == "POST" else "cancel" if match and match[1] and not match[2] and method.upper() == "DELETE" else None
            if operation is None:
                return original_request(method, path, body=body, timeout=timeout)
            previous = getattr(context, "current", None)
            current = {"request_id": uuid.uuid4().hex, "operation": operation, "attempt": 0, "order_id": match[1]}
            context.current = current
            started = time.monotonic_ns()
            common = {k: current[k] for k in ("request_id", "operation", "order_id")}
            intent = pick(body, REQUEST_FIELDS)
            safe_emit("ORDER_INTENT", common | intent | {"intent_scope": "outgoing_request_after_bot_guards"})
            safe_emit("ORDER_REQUEST", common | {"client_order_id": intent.get("client_order_id")})
            try:
                response = original_request(method, path, body=body, timeout=timeout)
            except BaseException as exc:
                safe_emit("ORDER_RESPONSE", common | {"outcome": "unknown", "error_type": type(exc).__name__, "duration_ns": str(time.monotonic_ns() - started), "attempts": current["attempt"]})
                raise
            else:
                try:
                    fields = response_fields(response)
                    safe_emit("ORDER_RESPONSE", common | fields | {"previous_order_id": common["order_id"], "duration_ns": str(time.monotonic_ns() - started), "attempts": current["attempt"],
                                                                  "outcome": "http_success" if 200 <= fields["http_status"] < 300 else "http_error"})
                except Exception:
                    pass
                return response
            finally:
                context.current = previous

        client.req, client.sess.request = request, transport
        client._othryss_attached = True
        if probe is not None:
            original_step = probe.step
            last_state = [float("-inf")]
            def sampled(ok):
                current = time.monotonic()
                if current - last_state[0] >= 10 or not ok:
                    capture_state(probe, publisher, "after_step", ok)
                    last_state[0] = current
            @functools.wraps(original_step)
            def step(*args, **kwargs):
                if (args[0] if args else kwargs.get("live")) is not True:
                    return original_step(*args, **kwargs)
                try:
                    result = original_step(*args, **kwargs)
                except BaseException:
                    sampled(False)
                    raise
                sampled(True)
                return result
            probe.step = step
        return publisher
    except Exception:
        return None
