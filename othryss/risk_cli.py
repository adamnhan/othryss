"""Read and apply primary-subaccount monitoring limits from a JSON file."""
import argparse
import json
import sqlite3
from pathlib import Path

from .account_risk import save_settings, settings, validate_config
from .fixture import ROOT
from .history_reader import HistoryReader


def read_config(path):
    with Path(path).open("r", encoding="utf-8-sig") as source:
        text = source.read(32769)
    if len(text) > 32768:
        raise ValueError("Configuration exceeds 32 KiB")
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"scope_id", "subaccount", "revision", "config"}:
        raise ValueError("Expected scope_id, subaccount, revision and config")
    if not isinstance(data["scope_id"], str) or not data["scope_id"]:
        raise ValueError("An explicit account scope_id is required")
    if type(data["subaccount"]) is not int or data["subaccount"] != 0:
        raise ValueError("Only primary subaccount 0 is supported")
    if type(data["revision"]) is not int or data["revision"] < 0:
        raise ValueError("Invalid settings revision")
    data["config"] = validate_config(data["config"])
    return data


def apply_config(path, config_path, *, check=False):
    data = read_config(config_path)
    with HistoryReader(path) as reader:
        reader.account(data["scope_id"])
        current = settings(reader.db, data["scope_id"])
        if current["revision"] != data["revision"]:
            raise ValueError("Settings changed; export the current configuration before applying")
    if check:
        return {"valid": True, "scope_id": data["scope_id"], "revision": data["revision"]}
    # save_settings rechecks the revision while holding the write transaction.
    result = save_settings(path, data["scope_id"], data["revision"], data["config"])
    return {"scope_id": data["scope_id"], "subaccount": 0, **result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "artifacts/history/othryss.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("accounts", help="List local account scope IDs")
    show = commands.add_parser("show", help="Export editable JSON for one account")
    show.add_argument("--scope", required=True)
    for name in ("check", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in ("check", "apply"):
            result = apply_config(args.db, args.file, check=args.command == "check")
        else:
            with HistoryReader(args.db) as reader:
                if args.command == "accounts":
                    result = [dict(row) for row in reader.db.execute("SELECT scope_id,account,venue,environment FROM accounts ORDER BY account,scope_id")]
                else:
                    reader.account(args.scope)
                    current = settings(reader.db, args.scope)
                    result = {"scope_id": args.scope, "subaccount": 0, "revision": current["revision"], "config": current["config"]}
        print(json.dumps(result, indent=2))
    except (ValueError, OSError) as error:
        parser.exit(2, f"{error}\n")
    except sqlite3.Error:
        parser.exit(2, "Local history is unavailable or needs a collector schema upgrade.\n")


if __name__ == "__main__":
    main()
