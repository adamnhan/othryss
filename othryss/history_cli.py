"""Historical import commands; no trading writes or live watcher."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .fixture import ROOT
from .credentials import credentials
from .history import import_saved_report, run_import
from .kalshi_client import ImportRequestError, KalshiClient
from .storage import Store, encode, import_lock

DEFAULT_DB = ROOT / "artifacts/history/othryss.sqlite"


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    commands = ap.add_subparsers(dest="command", required=True)
    live = commands.add_parser("import", help="Traverse current and historical orders/fills using a verified read-only key")
    live.add_argument("--account", required=True, help="Stable local account label; bound to this credential")
    live.add_argument("--workspace", default="local")
    live.add_argument("--environment", choices=["production", "demo"], default="production")
    live.add_argument("--key-id-env", default="OTHRYSS_KALSHI_KEY_ID", help="Environment variable name, not the key value")
    live.add_argument("--env-file", type=Path, default=ROOT / "local.env")
    live.add_argument("--key-file", type=Path)
    live.add_argument("--ticker", help="Optional scope; no date window silently truncates older history")
    live.add_argument("--page-size", type=int, default=100)
    live.add_argument("--max-pages", type=int, default=1000, help="Pause after this many pages; checkpoint is retained")
    live.add_argument("--resume", metavar="RUN_ID", help="Resume saved scope, filters, page size and cursors")
    saved = commands.add_parser("import-saved", help="Import fills from one historical LIP report without API access")
    saved.add_argument("source", type=Path)
    saved.add_argument("--account", default="saved-incentives")
    saved.add_argument("--workspace", default="local")
    status = commands.add_parser("status", help="Inspect the latest or selected import and scope totals")
    status.add_argument("--run-id")
    export = commands.add_parser("export", help="Export normalized events for the selected import's entire account scope")
    export.add_argument("--run-id")
    export.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-known", help="Compare this scope against saved incentives fill IDs and economics")
    verify.add_argument("source", type=Path)
    verify.add_argument("--run-id")
    return ap


def latest(store, requested):
    if requested:
        return requested
    row = store.db.execute("SELECT run_id FROM imports ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError("No import runs exist in this database")
    return row[0]


def verify_known(store, run_id, source):
    from .kalshi import normalize
    info = store.import_info(run_id)
    config = json.loads(info["config_json"])
    report = json.loads(Path(source).read_text(encoding="utf-8"))
    if report.get("guardrails", {}).get("live") is not True:
        raise ValueError("Known-fill comparison requires a report marked live")
    # Indexed identity lookups avoid loading an entire account history into memory.
    matched = missing = conflicts = 0
    for row in report["fills"]:
        expected = normalize(row, "fills", info["scope_id"], config["account"], config["environment"])
        stored = store.db.execute("SELECT canonical_json FROM events WHERE event_id=? AND scope_id=?",
                                 (expected["event_id"], info["scope_id"])).fetchone()
        if stored is None:
            missing += 1
        else:
            actual = json.loads(stored[0])
            if actual["payload"] != expected["payload"] or actual["occurred_at"] != expected["occurred_at"]:
                conflicts += 1
            else:
                matched += 1
    return {"known_fills": len(report["fills"]), "matched": matched, "missing": missing, "conflicts": conflicts,
            "independent_api_import": config["environment"] in {"production", "demo"}}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command in {"status", "export", "verify-known"} and not args.db.is_file():
            raise ValueError("Database does not exist; import data first")
        if args.command == "import":
            key_id, key_file = credentials(args.env_file, args.key_id_env, args.key_file)
            client = KalshiClient(key_id, key_file, args.environment)
            access = client.verify_read_only()
            client.credential_subaccount = access["subaccount"]
            with import_lock(args.db), Store(args.db) as store:
                scope = store.bind_account(args.workspace, "kalshi", args.environment, args.account, client.fingerprint)
                result = run_import(store, client, scope, args.account, ticker=args.ticker,
                                    page_size=args.page_size, max_pages=args.max_pages, resume=args.resume,
                                    progress=lambda value: print(encode(value), file=sys.stderr, flush=True))
        elif args.command == "import-saved":
            with import_lock(args.db), Store(args.db) as store:
                scope = store.bind_account(args.workspace, "kalshi", "saved-report", args.account, "saved-lip-reports")
                result = import_saved_report(store, args.source, scope, args.account)
        else:
            with Store(args.db) as store:
                run_id = latest(store, args.run_id)
                result = store.report(run_id)
                if args.command == "export":
                    if args.output.resolve() == args.db.resolve():
                        raise ValueError("Export destination cannot be the database")
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    with args.output.open("x", encoding="utf-8") as output:
                        count = 0
                        for event in store.events(result["scope_id"]):
                            output.write(encode(event) + "\n")
                            count += 1
                    result = {"output": str(args.output), "events": count, "scope_id": result["scope_id"]}
                elif args.command == "verify-known":
                    result = verify_known(store, run_id, args.source)
        print(json.dumps(result, indent=2))
        if args.command == "verify-known" and (result["missing"] or result["conflicts"]):
            return 2
        return 0 if result.get("status") not in {"paused", "needs_rescan", "failed"} else 2
    except (ValueError, KeyError, OSError, sqlite3.Error, ImportRequestError) as exc:
        print(f"Import error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Import interrupted; committed pages are retained.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
