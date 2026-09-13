"""Run a read-only Kalshi collector separately from the explorer."""

import argparse
import json
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .collector import configure, cycle, freshness, retry_delay, state, update
from .credentials import credentials
from .fixture import ROOT
from .history_cli import DEFAULT_DB
from .kalshi_client import KalshiClient
from .storage import Store, encode, import_lock, utc_now
from .reconciliation import configure_target, collect as collect_reconciliation
from .telemetry import import_directory
from . import bot_state
from . import account_risk


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--account", required=True)
    parser.add_argument("--workspace", default="local")
    parser.add_argument("--environment", choices=["production", "demo"], default="production")
    parser.add_argument("--env-file", type=Path, default=ROOT / "local.env")
    parser.add_argument("--key-id-env", default="OTHRYSS_KALSHI_KEY_ID")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--interval", type=int, default=60, help="Seconds between cycles after completion")
    parser.add_argument("--overlap", type=int, default=300, help="Revisit this many seconds of fills")
    parser.add_argument("--full-rescan", type=int, default=86400, help="Maximum seconds between full traversals")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=1000, help="Per-cycle budget; the next cycle resumes")
    parser.add_argument("--once", action="store_true", help="Run one bounded cycle and exit")
    parser.add_argument("--stop-file", type=Path, help="Stop gracefully when this local file appears; remove it before restarting")
    parser.add_argument("--reconcile-ticker", help="Configure the account's one-market position reconciliation pilot")
    parser.add_argument("--telemetry-dir", type=Path, help="Import the optional local bot spool for this account")
    parser.add_argument("--lip-supervisor-log", type=Path, help="Optional trusted LIP supervisor log for expected-retirement evidence")
    parser.add_argument("--bot-grace", type=int, default=120, help="Persistence grace for newly discovered bot monitors (30–3600 seconds)")
    parser.add_argument("--reconcile-subaccount", type=int, default=0, help="Explicit position subaccount, default primary (0)")
    parser.add_argument("--reconcile-grace", type=int, default=120, help="Seconds to wait before comparing a snapshot")
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {}
    def stopping():
        return stop.is_set() or (args.stop_file is not None and args.stop_file.exists())
    try:
        if stopping():
            print("Collector stop file exists; remove it before restarting.", file=sys.stderr)
            return 0
        if not 1 <= args.page_size <= 1000 or args.max_pages < 1:
            raise ValueError("Page size must be 1–1000 and max-pages positive")
        if not 30 <= args.bot_grace <= 3600:
            raise ValueError("Bot grace must be 30–3600 seconds")
        key_id, key_file = credentials(args.env_file, args.key_id_env, args.key_file)
        client = KalshiClient(key_id, key_file, args.environment)
        # A single OS-held lock prevents importer/collector and duplicate-worker races.
        with import_lock(args.db), Store(args.db) as store:
            scope = store.bind_account(args.workspace, "kalshi", args.environment, args.account, client.fingerprint)
            configure(store, scope, interval=args.interval, overlap=args.overlap, full_rescan=args.full_rescan)
            if args.reconcile_ticker:
                configure_target(store, scope, args.reconcile_ticker, args.reconcile_subaccount, args.reconcile_grace)
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())

            def progress(value):
                update(store, scope, heartbeat_at=utc_now())
                print(encode(value), file=sys.stderr, flush=True)
                if stopping():
                    raise KeyboardInterrupt

            def telemetry():
                if args.telemetry_dir:
                    try:
                        result = import_directory(store, scope, args.telemetry_dir)
                        from .source_health import observe
                        observe(store, scope, supervisor_log=args.lip_supervisor_log)
                        bot_state.evaluate(store, scope, grace=args.bot_grace)
                        if result["inserted"] or result["errors"]:
                            print(encode({"telemetry": result}), flush=True)
                    except Exception:
                        print(encode({"telemetry": {"status": "unavailable"}}), file=sys.stderr, flush=True)

            try:
                while not stopping():
                    telemetry()
                    try:
                        result = cycle(store, client, scope, args.account, page_size=args.page_size, max_pages=args.max_pages, progress=progress)
                        if result["status"] == "traversed":
                            try:
                                account_risk.collect(store,client,scope,progress=progress)
                            except KeyboardInterrupt:
                                raise
                            except Exception:
                                print(encode({"account_overview":"unavailable"}),file=sys.stderr,flush=True)
                            if args.telemetry_dir:
                                telemetry()
                                bot_state.collect(store, client, scope, args.account, grace=args.bot_grace, progress=progress)
                            try:
                                check = collect_reconciliation(store, client, scope, args.account, progress=progress)
                                if check:
                                    print(encode({"reconciliation_status": check["status"], "reason": check["reason"]}), flush=True)
                            except KeyboardInterrupt:
                                raise
                            except Exception:
                                print(encode({"reconciliation_status": "error", "reason": "Position check incomplete; order/fill collection continues"}), file=sys.stderr, flush=True)
                        print(encode({"run_id": result["run_id"], "status": result["status"], "mode": result["config"]["mode"],
                                      "inserted": sum(s["inserted"] for s in result["streams"]),
                                      "duplicates": sum(s["duplicates"] for s in result["streams"]), "sync": freshness(store.db, scope)}), flush=True)
                    except KeyboardInterrupt:
                        break
                    except Exception:
                        print(encode({"sync": freshness(store.db, scope)}), file=sys.stderr, flush=True)
                    current = state(store, scope)
                    if args.once:
                        return 0 if current["status"] == "idle" else 2
                    delay = retry_delay(current)
                    due = datetime.now(timezone.utc) + timedelta(seconds=delay)
                    update(store, scope, next_attempt_at=due.isoformat())
                    while not stopping():
                        remaining = (due - datetime.now(timezone.utc)).total_seconds()
                        if remaining <= 0:
                            break
                        stop.wait(min(5 if args.telemetry_dir else 30, remaining))
                        telemetry()
                        update(store, scope, heartbeat_at=utc_now())
            finally:
                update(store, scope, status="stopped" if state(store, scope)["status"] != "error" else "error", next_attempt_at=None, heartbeat_at=utc_now())
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Collector could not start ({type(exc).__name__}). Check configuration, credentials and whether another importer or collector holds the database lock.", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
