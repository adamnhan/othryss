"""Incident delivery worker and explicit preview/test commands."""
import argparse
import json
import signal
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import alerts
from .fixture import ROOT
from .storage import import_lock


def heartbeat(db, status):
    with db: db.execute("INSERT OR REPLACE INTO worker VALUES (1,?,?)",(status,time.time()))


def cycle(db, history, root, sender=None):
    routes = alerts.configuration(root/"alerts.local.json")
    resolved = {}
    for route in routes:
        if route["enabled"]:
            try:
                if not history.execute("SELECT 1 FROM accounts WHERE scope_id=?",(route["scope_id"],)).fetchone(): continue
                resolved[route["id"]] = alerts.secrets_for(route,root/"local.env")
            except ValueError: pass
    now = time.time()
    alerts.sync_routes(db,history,routes,resolved,now)
    for route in routes:
        if route["id"] not in resolved: continue
        heartbeat(db,"running")
        alerts.discover(db,history,route,time.time())
        alerts.deliver_one(db,history,route,resolved[route["id"]],time.time(),**({"sender":sender} if sender else {}))
    heartbeat(db,"running" if resolved else "unconfigured")
    alerts.prune(db,history,time.time())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["run","preview","test","status"])
    parser.add_argument("--root",type=Path,default=ROOT)
    parser.add_argument("--route")
    parser.add_argument("--stop-file",type=Path)
    parser.add_argument("--once",action="store_true")
    args = parser.parse_args(); root=args.root.resolve()
    path=root/"artifacts/alerts/delivery.sqlite"
    try:
        if args.action=="status":
            routes=alerts.configuration(root/"alerts.local.json")
            print(json.dumps({scope:alerts.read(path,scope) for scope in sorted({r['scope_id'] for r in routes})},indent=2));return 0
        if args.action in {"preview","test"}:
            route=next((r for r in alerts.configuration(root/"alerts.local.json") if r["id"]==args.route),None)
            if not route: raise ValueError("Specify a configured --route")
            incident={"scope_id":route["scope_id"],"incident_id":"test-"+uuid.uuid4().hex,"instrument_id":"TEST-NOT-A-LIVE-INCIDENT",
                      "rule":"delivery_test","entity":"test","assessment":"test","last_seen":datetime.now(timezone.utc).isoformat()}
            payload=alerts.envelope(incident,"test",time.time())
            if args.action=="preview":
                print(json.dumps(payload,indent=2));return 0
            if not route["enabled"]: raise ValueError("Enable the route before requesting a test delivery")
            secrets=alerts.secrets_for(route,root/"local.env")
            db=alerts.connect(path)
            try:
                state=db.execute("SELECT active,fingerprint FROM routes WHERE route_id=?",(route["id"],)).fetchone()
                if not state or not state["active"] or state["fingerprint"]!=alerts.digest([route,secrets]):
                    raise ValueError("Wait for the worker to activate this exact route configuration")
                with db: alerts.enqueue(db,route,incident["incident_id"],incident,"test",time.time())
                print("Test queued. Inspect Alert delivery for provider acceptance or failure.")
            finally: db.close()
            return 0
        path.parent.mkdir(parents=True,exist_ok=True)
        stop=threading.Event()
        for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,lambda *_:stop.set())
        with import_lock(path):
            db=alerts.connect(path)
            try:
                alerts.recover_inflight(db)
                while not stop.is_set() and not (args.stop_file and args.stop_file.exists()):
                    history=None
                    try:
                        history=sqlite3.connect((root/"artifacts/history/othryss.sqlite").as_uri()+"?mode=ro",uri=True,timeout=5)
                        history.row_factory=sqlite3.Row
                        history.execute("PRAGMA query_only=ON")
                        cycle(db,history,root)
                    except Exception:
                        # Configuration/provider data and exception strings may contain secrets.
                        heartbeat(db,"configuration_or_history_error")
                    finally:
                        if history: history.close()
                    if args.once: break
                    stop.wait(5)
            finally:
                heartbeat(db,"stopped");db.close()
        return 0
    except Exception as exc:
        # Fixed messages for configuration errors only; no HTTP/provider text.
        print("Alert command failed. Check local configuration and worker status ("+type(exc).__name__+").")
        return 1


if __name__=="__main__": raise SystemExit(main())
