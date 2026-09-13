"""Repeatable local setup and read-only readiness checks. Never starts services."""
import argparse
import importlib.metadata
import json
import re
import socket
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import HTTPRedirectHandler, build_opener

from . import alerts, ops
from .credentials import credentials
from .fixture import ROOT
from .kalshi_client import KalshiClient
from .replay import timestamp

ENV_TEMPLATE = "# Dedicated read-only Kalshi key; private key stays in a separate file.\nOTHRYSS_KALSHI_KEY_ID=\nOTHRYSS_KALSHI_PRIVATE_KEY_PATH=\n"


def initialize(root, account, environment, port=None):
    root = Path(root).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", account):
        raise ValueError("Use a stable account label with letters, digits, dots, underscores or hyphens")
    if environment not in {"demo", "production"} or (port is not None and not 1024 <= port <= 65535):
        raise ValueError("Choose demo or production and a port from 1024 to 65535")
    settings = ops.DEFAULTS | {"account": account, "environment": environment, "port": port or 8766, "reconcile_ticker": None}
    # Validate identity before creating anything, including on a partial rerun.
    if (root / "ops.local.json").exists():
        existing = ops.config(root)
        if any(existing[k] != settings[k] for k in ("account", "environment")) or (port is not None and existing["port"] != port):
            raise ValueError("Existing installation identity or port differs; use a separate checkout for another account")
    root.mkdir(parents=True, exist_ok=True)
    files = {"ops.local.json": json.dumps(settings, indent=2) + "\n", "local.env": ENV_TEMPLATE,
             "alerts.local.json": '{"version":1,"routes":[]}\n'}
    result = {}
    for name, content in files.items():
        try:
            with (root / name).open("x", encoding="utf-8") as out:
                out.write(content)
            result[name] = "created"
        except FileExistsError:
            result[name] = "preserved"
    return {"version": 1, "action": "init", "files": result,
            "next": "Fill local.env, install requirements-reference.txt, then run python -m othryss.onboarding check"}


def recent(value, seconds, now):
    try:
        return -5 <= (now - timestamp(value)).total_seconds() <= seconds
    except (ValueError, TypeError):
        return False


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def check(root, *, live=False, require_telemetry=False, require_alerts=False, client_factory=KalshiClient, now=None):
    root = Path(root).resolve()
    now = now or datetime.now(timezone.utc)
    checks = []

    def add(name, status, detail, next_step="", required=True):
        checks.append({"name": name, "status": status, "required": required, "detail": detail, "next": next_step if status != "pass" else ""})

    def finish():
        return {"version": 1, "checked_at": now.isoformat(), "phase": "live" if live else "preflight",
                "requirements": {"telemetry": require_telemetry, "alerts": require_alerts},
                "ready": all(c["status"] == "pass" for c in checks if c["required"]), "checks": checks}

    add("python", "pass" if sys.version_info >= (3, 11) else "fail", "Python 3.11 or later required", "Use Python 3.11 or later")
    for package, low, high in (("cryptography", 43, 48), ("websockets", 16, 17)):
        try:
            version = importlib.metadata.version(package)
            valid = low <= int(version.split(".")[0]) < high
        except (importlib.metadata.PackageNotFoundError, ValueError):
            valid = False
        add(package, "pass" if valid else "fail", "Installed dependency satisfies the supported major-version range" if valid else "Dependency missing or unsupported",
            "python -m pip install -r requirements-reference.txt")
    try:
        if not (root / "ops.local.json").is_file():
            raise ValueError()
        settings = ops.config(root)
        add("configuration", "pass", "Operational configuration is valid")
        raw = json.loads((root / "ops.local.json").read_text(encoding="utf-8-sig"))
        if not {"account", "environment"} <= raw.keys():
            add("explicit_identity", "pending", "Legacy configuration inherits account or environment defaults",
                "Record the intended account and environment explicitly in ops.local.json", required=False)
    except (OSError, ValueError, TypeError):
        add("configuration", "fail", "Local operational configuration is missing or invalid", "Run onboarding init with an explicit account and environment")
        return finish()
    client = None
    try:
        key_id, key_path = credentials(root / "local.env")
        client = client_factory(key_id, key_path, settings["environment"])
        add("credentials", "pass", "Configured private key can be loaded; permissions are not yet verified")
    except Exception:
        add("credentials", "fail", "Credential settings or private key could not be loaded", "Fill local.env with the key ID and path to an unencrypted RSA private-key file")
    try:
        routes = alerts.configuration(root / "alerts.local.json")
        enabled = [r for r in routes if r["enabled"]]
        for route in enabled:
            alerts.secrets_for(route, root / "local.env")
        add("alert_configuration", "pass", "Alert configuration and enabled destination settings are valid; no message sent")
    except Exception:
        routes, enabled = [], []
        add("alert_configuration", "fail", "Invalid alert configuration or missing enabled destination settings", "See docs/alert-delivery.md")
    log = settings.get("lip_supervisor_log")
    if log and not Path(log).is_file():
        add("retirement_log", "fail", "Configured supervisor log is missing", "Correct lip_supervisor_log or set it to null")
    if not live:
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", settings["port"]))
            add("port", "pass", "Explorer port is available")
        except OSError:
            add("port", "pending", "Explorer port is already occupied", "If Othryss is running, use check --live; otherwise choose another port", required=False)
        add("live_evidence", "pending", "Read-only access and collection have not been verified", "Start Othryss, then run check --live", required=False)
        return finish()
    if client is None:
        add("read_only_access", "fail", "Cannot verify access without loadable credentials", "Fix credentials and rerun check --live")
    else:
        try:
            access = client.verify_read_only()
            # The managed account/risk and LIP integration currently use primary subaccount 0.
            if access.get("subaccount") not in (None, 0):
                raise ValueError()
            add("read_only_access", "pass", "Kalshi verified read-only access compatible with the primary-subaccount integration")
        except Exception:
            add("read_only_access", "fail", "Read-only access could not be verified for this environment and primary subaccount", "Check connectivity, environment and a dedicated key with only read scope")
    try:
        state = json.loads((root / "artifacts/ops/status.json").read_text(encoding="utf-8"))
        running = state.get("status") == "running" and recent(state.get("heartbeat_at"), 10, now)
        running = running and not (root / "artifacts/ops/STOP").exists()
        running = running and all(state.get("services", {}).get(name, {}).get("status") == "running" for name in ("collector", "reference", "explorer", "alerts"))
        add("services", "pass" if running else "fail", "Supervisor and all four services are current" if running else "Supervisor or a required service is not current", "python -m othryss.ops status")
        backup = state.get("backup", {})
        backup_ok = backup.get("status") == "complete" and recent(backup.get("finished_at"), settings["backup_interval_seconds"] + 300, now)
        add("backup", "pass" if backup_ok else "pending", "Supervisor recorded a recent verified backup" if backup_ok else "No recent completed backup is recorded", "Wait for the first scheduled backup; inspect ops status", required=False)
    except (OSError, ValueError, TypeError):
        add("services", "fail", "No readable supervisor status", "python -m othryss.ops start")
    scope = None
    try:
        with closing(connect(root / "artifacts/history/othryss.sqlite")) as db:
            account = db.execute("SELECT * FROM accounts WHERE workspace='local' AND venue='kalshi' AND account=? AND environment=?", (settings["account"], settings["environment"])).fetchone()
            if not account or client is None or account["credential_fingerprint"] != client.fingerprint:
                add("account_binding", "fail", "No imported account bound to this configured identity and key", "Allow initial collection; investigate any key/account mismatch before reusing history")
            else:
                scope = account["scope_id"]
                add("account_binding", "pass", "Imported account matches the configured identity and key")
                from .collector import freshness
                health = freshness(db, scope, now=now)
                ok = health.get("freshness") == "recent" and health.get("worker_heartbeat") == "recent" and health.get("status") in {"idle", "running"} and not health.get("failure_count")
                add("collection", "pass" if ok else "pending", "A recent account-wide collection cycle completed" if ok else "Account-wide collection is not yet current", "Wait for initial traversal or inspect collector status")
                counts = {r[0]: r[1] for r in db.execute("SELECT type,COUNT(*) FROM events WHERE scope_id=? AND type IN ('ORDER_FILL','ORDER_OBSERVATION') GROUP BY type", (scope,))}
                add("imported_evidence", "pass", f"Retained {counts.get('ORDER_OBSERVATION', 0)} order observations and {counts.get('ORDER_FILL', 0)} fills; an empty account is valid")
                from .bot_state import source_status
                sessions = [r[0] for r in db.execute("SELECT session_id FROM telemetry_health WHERE scope_id=?", (scope,))]
                healthy = sum(source_status(db, scope, session, now)[0] == "healthy" for session in sessions)
                add("telemetry", "pass" if healthy else "pending", f"{healthy} healthy bot sessions; this does not prove fill linkage", "Configure the bot integration with the same account/environment; see docs/onboarding.md", required=require_telemetry)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        add("history", "fail", "History is missing, unreadable or incompatible", "Allow the collector to create/import history, then rerun")
    if scope:
        try:
            from .reference import read
            refs = read(root / "artifacts/reference/quotes.sqlite", scope)
            eligible = sum(bool(m.get("eligible")) for m in refs.get("markets", []))
            add("reference_evidence", "pass" if eligible else "pending", f"{eligible} markets have eligible reference evidence", "Reference capture needs an active supported bot market; historical quotes cannot be backfilled", required=require_telemetry)
        except Exception:
            add("reference_evidence", "pending", "Reference evidence is unavailable", "Inspect the reference worker and active bot markets", required=require_telemetry)
        try:
            matching = [r for r in enabled if r["scope_id"] == scope]
            with closing(connect(root / "artifacts/alerts/delivery.sqlite")) as db:
                worker = db.execute("SELECT * FROM worker WHERE id=1").fetchone()
                alive = bool(worker and worker["status"] == "running" and -5 <= now.timestamp() - worker["heartbeat_at"] <= 120)
                active, accepted = [], 0
                for route in matching:
                    row = db.execute("SELECT active,fingerprint,error,activated_at FROM routes WHERE route_id=? AND scope_id=?", (route["id"], scope)).fetchone()
                    expected = alerts.digest([route, alerts.secrets_for(route, root / "local.env")])
                    if row and row["active"] and not row["error"] and row["fingerprint"] == expected:
                        active.append(route)
                        accepted += bool(db.execute("SELECT 1 FROM deliveries WHERE route_id=? AND scope_id=? AND status='accepted' AND created_at>=? LIMIT 1", (route["id"], scope, row["activated_at"])).fetchone())
            ok = alive and bool(matching) and len(active) == len(matching)
            add("alert_delivery", "pass" if ok else "pending", "Configured routes are active; provider acceptance is checked separately" if ok else "No fully active alert integration for this account", "See docs/alert-delivery.md; onboarding never sends a test message", required=require_alerts)
            proven = ok and accepted == len(matching)
            add("alert_provider_acceptance", "pass" if proven else "pending", "Every enabled route has a provider-accepted delivery since activation; recipient receipt is not proven" if proven else "Delivery acceptance is not recorded for every enabled route", "Observe a genuine alert or explicitly request a route test, then confirm receipt", required=require_alerts)
        except Exception:
            add("alert_delivery", "pending", "Alert worker evidence is unavailable", "Configure and activate a route for this account", required=require_alerts)
        try:
            # Do not follow a redirect from an unrelated local service.
            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            with build_opener(NoRedirect()).open(f"http://127.0.0.1:{settings['port']}/api/history/accounts", timeout=5) as response:
                page = json.loads(response.read(1024 * 1024))
            ok = any(a.get("scope_id") == scope for a in page.get("accounts", []))
            add("explorer", "pass" if ok else "fail", "Local explorer serves the configured account" if ok else "Explorer account does not match", "Inspect the explorer port and database configuration")
        except Exception:
            add("explorer", "fail", "Local explorer did not serve valid account evidence", "Inspect ops status and explorer port")
    else:
        add("integration_evidence", "pending", "Telemetry, references, alerts and explorer require a matching imported account", "Complete account binding and rerun")
    return finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="Create missing local configuration; preserve existing files")
    init.add_argument("--account", required=True)
    init.add_argument("--environment", choices=["demo", "production"], required=True)
    init.add_argument("--port", type=int)
    inspect = sub.add_parser("check", help="Check setup; --live also verifies GET-only access and running evidence")
    inspect.add_argument("--live", action="store_true")
    inspect.add_argument("--require-telemetry", action="store_true")
    inspect.add_argument("--require-alerts", action="store_true")
    inspect.add_argument("--report", type=Path, help="Write a new sanitized JSON report; refuses existing files")
    args = parser.parse_args(argv)
    if args.action == "check" and (args.require_telemetry or args.require_alerts) and not args.live:
        parser.error("Integration requirements need --live")
    try:
        if args.action == "init":
            result = initialize(ROOT, args.account, args.environment, args.port)
        else:
            result = check(ROOT, live=args.live, require_telemetry=args.require_telemetry, require_alerts=args.require_alerts)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                with args.report.open("x", encoding="utf-8") as out:
                    json.dump(result, out, indent=2)
                    out.write("\n")
        print(json.dumps(result, indent=2))
        return 0 if result.get("ready", True) else 1
    except Exception as exc:
        # No arbitrary exception text, credential IDs, key paths, or destinations.
        print(json.dumps({"ready": False, "error": type(exc).__name__, "next": "Check configuration, existing installation identity and report path; existing files are preserved"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
