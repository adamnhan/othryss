"""Loopback-only offline explorer. Run with: python -m othryss.server."""

import argparse
import json
import sqlite3
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .fixture import ROOT, build_explorer
from .history_reader import HistoryReader
from .reference import DEFAULT_DB as REFERENCE_DB

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/order-detail.js": ("order-detail.js", "text/javascript; charset=utf-8"),
    "/history.js": ("history.js", "text/javascript; charset=utf-8"),
    "/telemetry.js": ("telemetry.js", "text/javascript; charset=utf-8"),
    "/incidents.js": ("incidents.js", "text/javascript; charset=utf-8"),
    "/reference.js": ("reference.js", "text/javascript; charset=utf-8"),
    "/markouts.js": ("markouts.js", "text/javascript; charset=utf-8"),
    "/execution.js": ("execution.js", "text/javascript; charset=utf-8"),
    "/alerts.js": ("alerts.js", "text/javascript; charset=utf-8"),
    "/account-risk.js": ("account-risk.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


class ExplorerServer(ThreadingHTTPServer):
    # Page assets and API polling arrive in bursts, especially with multiple tabs.
    # The default five pending connections can reject local browser requests.
    request_queue_size = 128


class Handler(BaseHTTPRequestHandler):
    history_db = ROOT / "artifacts/history/othryss.sqlite"
    reference_db = REFERENCE_DB
    alerts_db = ROOT / "artifacts/alerts/delivery.sqlite"

    def respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        request = urlparse(self.path)
        if request.path in {"/api/history/order-groups", "/api/history/order-investigation", "/api/history/accounts", "/api/history/orders", "/api/history/order", "/api/history/health", "/api/history/reconciliation", "/api/history/telemetry", "/api/history/telemetry-request", "/api/history/incidents", "/api/history/incident", "/api/history/bot-check", "/api/history/references", "/api/history/markouts", "/api/history/execution", "/api/history/alerts", "/api/history/account-risk"}:
            try:
                query = parse_qs(request.query)
                def param(name, default=""):
                    return query.get(name, [default])[0]
                if not self.history_db.is_file():
                    if request.path == "/api/history/accounts":
                        result = {"accounts": [], "message": "No local history database yet. Import history to browse it here."}
                    else:
                        raise ValueError("No local history database")
                else:
                    with HistoryReader(self.history_db) as reader:
                        if request.path.endswith("/accounts"):
                            result = reader.accounts()
                        elif request.path.endswith("/order-investigation"):
                            from .order_detail import investigate
                            result = investigate(reader, self.reference_db, param("scope"), param("instrument"), param("order"))
                        elif request.path.endswith("/account-risk"):
                            from .account_risk import catalog
                            result = catalog(reader,param("scope"),param("window","24h"),int(param("offset","0")))
                        elif request.path.endswith("/alerts"):
                            from .alerts import read
                            reader.account(param("scope"))
                            result = read(self.alerts_db, param("scope"))
                        elif request.path.endswith("/execution"):
                            from .execution import catalog
                            result = catalog(reader, param("scope"), param("instrument"), param("operation"), int(param("limit", "25")), int(param("offset", "0")))
                        elif request.path.endswith("/markouts"):
                            from .markouts import catalog
                            result = catalog(reader, self.reference_db, param("scope"), param("instrument"), param("order"), int(param("limit", "25")), int(param("offset", "0")))
                        elif request.path.endswith("/references"):
                            from .reference import read
                            reader.account(param("scope"))
                            result = read(self.reference_db, param("scope"), param("instrument") or None, int(param("limit", "200")))
                        elif request.path.endswith(("/incidents", "/incident", "/bot-check")):
                            from . import incidents
                            reader.account(param("scope"))
                            if request.path.endswith("/incidents"):
                                result = incidents.catalog(reader.db, param("scope"))
                            elif request.path.endswith("/incident"):
                                result = incidents.detail(reader.db, param("scope"), param("incident"))
                            else:
                                result = incidents.check_detail(reader.db, param("scope"), param("check"))
                        elif request.path.endswith(("/telemetry", "/telemetry-request")):
                            from .telemetry import catalog, request_detail
                            reader.account(param("scope"))
                            result = catalog(reader.db, param("scope")) if request.path.endswith("/telemetry") else request_detail(reader.db, param("scope"), param("session"), param("request"))
                        elif request.path.endswith("/health"):
                            from .collector import freshness
                            reader.account(param("scope"))
                            result = {"sync": freshness(reader.db, param("scope"))}
                        elif request.path.endswith("/reconciliation"):
                            from .reconciliation import read
                            reader.account(param("scope"))
                            result = read(reader.db, param("scope"), param("check") or None)
                        elif request.path.endswith("/order-groups"):
                            result = reader.order_groups(param("scope"), param("q"), param("filled") == "1", param("group"), int(param("limit", "25")), int(param("offset", "0")), int(param("through")) if param("through") else None)
                        elif request.path.endswith("/orders"):
                            result = reader.orders(param("scope"), param("q"), param("filled") == "1", int(param("limit", "25")), int(param("offset", "0")))
                        else:
                            result = reader.order(param("scope"), param("instrument"), param("order"), int(param("limit", "50")), int(param("offset", "0")))
                self.respond(200, json.dumps(result).encode(), "application/json")
            except ValueError as exc:
                self.respond(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            except sqlite3.Error:
                self.respond(503, json.dumps({"error": "Local history is unavailable. Check the database and retry."}).encode(), "application/json")
            return
        if request.path == "/api/explorer":
            try:
                replays = int(parse_qs(request.query).get("replays", ["1"])[0])
                result = build_explorer(replays=replays)
                self.respond(200, json.dumps(result).encode(), "application/json")
            except (ValueError, KeyError) as exc:
                self.respond(400, json.dumps({"error": str(exc)}).encode(), "application/json")
            return
        if request.path in STATIC:
            filename, content_type = STATIC[request.path]
            self.respond(200, (ROOT / "web" / filename).read_bytes(), content_type)
            return
        self.respond(404, b"Not found", "text/plain")

    def do_POST(self):
        if self.path not in {"/api/history/incident-action", "/api/history/risk-settings"}:
            self.respond(404, b"Not found", "text/plain")
            return
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"} or self.headers.get("Origin") != "http://" + host or self.headers.get("X-Othryss-Review") != "1":
            self.respond(403, b'{"error":"Incident actions require the local explorer origin"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= (32768 if self.path.endswith("risk-settings") else 4096) or self.headers.get("Content-Type") != "application/json":
                raise ValueError("Expected a bounded JSON review action")
            data = json.loads(self.rfile.read(length))
            if self.path.endswith("risk-settings"):
                if not isinstance(data,dict) or set(data)!={"scope","revision","config"} or not isinstance(data["scope"],str):
                    raise ValueError("Invalid inventory settings request")
                from .account_risk import save_settings
                result=save_settings(self.history_db,data["scope"],data["revision"],data["config"])
                self.respond(200,json.dumps(result).encode(),"application/json")
                return
            if not isinstance(data, dict) or set(data) != {"scope", "incident", "action", "note"} or not all(isinstance(v, str) for v in data.values()):
                raise ValueError("Invalid review action")
            from .incidents import act
            result = act(self.history_db, data["scope"], data["incident"], data["action"], data["note"])
            self.respond(200, json.dumps(result).encode(), "application/json")
        except (ValueError, UnicodeError) as exc:
            self.respond(400, json.dumps({"error": str(exc)}).encode(), "application/json")
        except sqlite3.Error:
            self.respond(503, b'{"error":"Local review store is unavailable; retry shortly"}', "application/json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=Handler.history_db, help="Local history database (opened read-only)")
    parser.add_argument("--reference-db", type=Path, default=Handler.reference_db, help="Separate reference-price database (opened read-only)")
    parser.add_argument("--alerts-db", type=Path, default=Handler.alerts_db, help="Separate alert delivery database (opened read-only)")
    args = parser.parse_args()
    handler = type("ConfiguredHandler", (Handler,), {"history_db": args.db.resolve(), "reference_db": args.reference_db.resolve(), "alerts_db": args.alerts_db.resolve()})
    server = ExplorerServer(("127.0.0.1", args.port), handler)
    print(f"Othryss offline explorer: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
