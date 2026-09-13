"""Durable normalized events, source evidence, and atomic import checkpoints."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6, 7):
            self.close()
            raise ValueError("Unsupported database schema version")
        if version == 0:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE accounts (
                    scope_id TEXT PRIMARY KEY, workspace TEXT NOT NULL, venue TEXT NOT NULL,
                    environment TEXT NOT NULL, account TEXT NOT NULL,
                    credential_fingerprint TEXT NOT NULL,
                    UNIQUE(workspace, venue, environment, account)
                );
                CREATE TABLE imports (
                    run_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES accounts,
                    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
                    config_json TEXT NOT NULL, cutoff_before_json TEXT NOT NULL,
                    cutoff_after_json TEXT, error TEXT
                );
                CREATE TABLE streams (
                    run_id TEXT NOT NULL REFERENCES imports, name TEXT NOT NULL,
                    cursor TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0,
                    pages INTEGER NOT NULL DEFAULT 0, rows_seen INTEGER NOT NULL DEFAULT 0,
                    inserted INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(run_id, name)
                );
                CREATE TABLE events (
                    event_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES accounts,
                    type TEXT NOT NULL, instrument_id TEXT NOT NULL, order_id TEXT,
                    occurred_at TEXT, received_at TEXT NOT NULL, canonical_json TEXT NOT NULL
                );
                CREATE INDEX events_scope_time ON events(scope_id, occurred_at, event_id);
                CREATE INDEX events_order ON events(scope_id, order_id);
                CREATE TABLE pages (
                    page_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES imports,
                    stream TEXT NOT NULL, page_number INTEGER NOT NULL,
                    request_cursor TEXT NOT NULL, next_cursor TEXT NOT NULL,
                    received_at TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    UNIQUE(run_id, stream, page_number)
                );
                CREATE TABLE event_evidence (
                    event_id TEXT NOT NULL REFERENCES events, page_id INTEGER NOT NULL REFERENCES pages,
                    row_index INTEGER NOT NULL, PRIMARY KEY(page_id, row_index)
                );
                PRAGMA user_version=1;
                COMMIT;
            """)
        if version < 2:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE sync_state (
                    scope_id TEXT PRIMARY KEY REFERENCES accounts,
                    active_run TEXT REFERENCES imports,
                    fill_watermark INTEGER, last_success_at TEXT, last_full_at INTEGER,
                    cutoff_json TEXT, status TEXT NOT NULL DEFAULT 'idle',
                    last_attempt_at TEXT, heartbeat_at TEXT, next_attempt_at TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                    interval_seconds INTEGER NOT NULL, overlap_seconds INTEGER NOT NULL,
                    full_rescan_seconds INTEGER NOT NULL
                );
                CREATE INDEX imports_scope_started ON imports(scope_id,started_at DESC);
                PRAGMA user_version=2;
                COMMIT;
            """)
        if version < 3:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE reconciliation_targets (
                    scope_id TEXT PRIMARY KEY REFERENCES accounts, instrument_id TEXT NOT NULL,
                    subaccount INTEGER NOT NULL, grace_seconds INTEGER NOT NULL,
                    baseline_id TEXT, last_attempt_at TEXT, last_success_at TEXT, last_error TEXT
                );
                CREATE TABLE position_observations (
                    snapshot_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES accounts,
                    instrument_id TEXT NOT NULL, subaccount INTEGER NOT NULL,
                    request_started_at TEXT NOT NULL, received_at TEXT NOT NULL,
                    quantity TEXT, evidence_json TEXT NOT NULL
                );
                CREATE INDEX positions_scope_time ON position_observations(scope_id,received_at);
                CREATE TABLE reconciliation_checks (
                    check_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES accounts,
                    checked_at TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL
                );
                CREATE INDEX checks_scope_time ON reconciliation_checks(scope_id,checked_at DESC);
                CREATE INDEX events_instrument_time ON events(scope_id,instrument_id,occurred_at);
                PRAGMA user_version=3;
                COMMIT;
            """)
        if version < 4:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE telemetry_records (
                    scope_id TEXT NOT NULL REFERENCES accounts, event_id TEXT NOT NULL,
                    session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    request_id TEXT, instrument_id TEXT NOT NULL, subaccount INTEGER NOT NULL,
                    occurred_at TEXT NOT NULL, received_at TEXT NOT NULL, type TEXT NOT NULL,
                    canonical_json TEXT NOT NULL, PRIMARY KEY(scope_id,event_id), UNIQUE(scope_id,session_id,sequence)
                );
                CREATE INDEX telemetry_request ON telemetry_records(scope_id,session_id,request_id);
                CREATE INDEX telemetry_market ON telemetry_records(scope_id,instrument_id,occurred_at);
                CREATE TABLE telemetry_files (
                    scope_id TEXT NOT NULL REFERENCES accounts, path TEXT NOT NULL,
                    byte_offset INTEGER NOT NULL DEFAULT 0, prefix_hash TEXT, prefix_length INTEGER,
                    last_error TEXT, PRIMARY KEY(scope_id,path)
                );
                CREATE TABLE telemetry_health (
                    scope_id TEXT NOT NULL REFERENCES accounts, session_id TEXT NOT NULL,
                    received_at TEXT NOT NULL, health_json TEXT NOT NULL, PRIMARY KEY(scope_id,session_id)
                );
                PRAGMA user_version=4;
                COMMIT;
            """)

        if version < 5:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE bot_exchange (
                    snapshot_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES accounts,
                    instrument_id TEXT NOT NULL, subaccount INTEGER NOT NULL,
                    started_at TEXT NOT NULL, received_at TEXT NOT NULL, evidence_json TEXT NOT NULL
                );
                CREATE INDEX bot_exchange_scope ON bot_exchange(scope_id,instrument_id,received_at);
                CREATE TABLE bot_checks (
                    check_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES accounts, session_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, checked_at TEXT NOT NULL, status TEXT NOT NULL, result_json TEXT NOT NULL,
                    UNIQUE(scope_id,session_id,snapshot_id)
                );
                CREATE TABLE bot_monitors (
                    scope_id TEXT NOT NULL REFERENCES accounts, session_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
                    subaccount INTEGER NOT NULL, grace_seconds INTEGER NOT NULL DEFAULT 120,
                    last_attempt_at TEXT, last_error TEXT, PRIMARY KEY(scope_id,session_id)
                );
                CREATE TABLE incidents (
                    incident_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES accounts, session_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL, rule TEXT NOT NULL, entity TEXT NOT NULL,
                    status TEXT NOT NULL, assessment TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    opened_at TEXT, acknowledged_at TEXT, resolved_at TEXT,
                    first_check TEXT NOT NULL, last_check TEXT NOT NULL, hits INTEGER NOT NULL, clears INTEGER NOT NULL,
                    last_capture TEXT NOT NULL
                );
                CREATE UNIQUE INDEX incident_active ON incidents(scope_id,session_id,rule,entity) WHERE status!='resolved';
                CREATE INDEX incident_list ON incidents(scope_id,last_seen);
                CREATE TABLE incident_actions (
                    action_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL REFERENCES incidents,
                    occurred_at TEXT NOT NULL, action TEXT NOT NULL, note TEXT NOT NULL
                );
                PRAGMA user_version=5;
                COMMIT;
            """)

        if version < 6:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE account_snapshots (
                    snapshot_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES accounts,
                    subaccount INTEGER NOT NULL, started_at TEXT NOT NULL, received_at TEXT NOT NULL,
                    evidence_json TEXT NOT NULL);
                CREATE INDEX account_snapshot_latest ON account_snapshots(scope_id,subaccount,received_at);
                CREATE TABLE risk_settings (
                    scope_id TEXT PRIMARY KEY REFERENCES accounts, revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL, config_json TEXT NOT NULL);
                PRAGMA user_version=6;
                COMMIT;
            """)

        if version < 7:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE source_health (
                    scope_id TEXT NOT NULL REFERENCES accounts, session_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, last_heartbeat TEXT NOT NULL,
                    counters_json TEXT NOT NULL, quiet_since TEXT NOT NULL,
                    baseline_sequence INTEGER NOT NULL, retirement_json TEXT,
                    PRIMARY KEY(scope_id,session_id));
                PRAGMA user_version=7;
                COMMIT;
            """)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def bind_account(self, workspace, venue, environment, account, fingerprint):
        scope_id = digest([workspace, venue, environment, account])
        existing = self.db.execute("SELECT * FROM accounts WHERE scope_id=?", (scope_id,)).fetchone()
        if existing and existing["credential_fingerprint"] != fingerprint:
            raise ValueError("Account label is already bound to a different credential/source; use a separate label")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO accounts VALUES (?,?,?,?,?,?)",
                            (scope_id, workspace, venue, environment, account, fingerprint))
        return scope_id

    def create_import(self, scope_id, config, cutoff, streams):
        run_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO imports (run_id,scope_id,status,started_at,config_json,cutoff_before_json) VALUES (?,?,?,?,?,?)",
                            (run_id, scope_id, "running", utc_now(), encode(config), encode(cutoff)))
            self.db.executemany("INSERT INTO streams(run_id,name) VALUES (?,?)", [(run_id, name) for name in streams])
            if config.get("collector"):
                self.db.execute("UPDATE sync_state SET active_run=? WHERE scope_id=?", (run_id, scope_id))
        return run_id

    def import_info(self, run_id):
        row = self.db.execute("SELECT * FROM imports WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown import run")
        return dict(row)

    def checkpoint(self, run_id, stream):
        return dict(self.db.execute("SELECT * FROM streams WHERE run_id=? AND name=?", (run_id, stream)).fetchone())

    def status(self, run_id, status, error=None, cutoff_after=None):
        with self.db:
            self.db.execute("UPDATE imports SET status=?, error=?, finished_at=?, cutoff_after_json=COALESCE(?,cutoff_after_json) WHERE run_id=?",
                            (status, error, utc_now() if status != "running" else None,
                             encode(cutoff_after) if cutoff_after is not None else None, run_id))

    def commit_page(self, run_id, stream, checkpoint, records, evidence, next_cursor, received_at, *, retain_duplicates=True):
        inserted = duplicates = 0
        scope_id = self.import_info(run_id)["scope_id"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.checkpoint(run_id, stream)
            if current != checkpoint or current["done"]:
                raise ValueError("Import checkpoint changed; only one importer may resume a run")
            page_id = self.db.execute("INSERT INTO pages(run_id,stream,page_number,request_cursor,next_cursor,received_at,evidence_json) VALUES (?,?,?,?,?,?,?)",
                                     (run_id, stream, current["pages"] + 1, current["cursor"], next_cursor, received_at, encode(evidence) if retain_duplicates else "{}")).lastrowid
            retained = []
            for index, record in enumerate(records):
                canonical = encode(record)
                prior = self.db.execute("SELECT canonical_json FROM events WHERE event_id=?", (record["event_id"],)).fetchone()
                if prior:
                    if prior[0] != canonical:
                        raise ValueError("Conflicting immutable event identity; page was not committed")
                    duplicates += 1
                    if not retain_duplicates:
                        continue
                else:
                    self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                                    (record["event_id"], scope_id, record["type"], record["instrument_id"],
                                     record["payload"].get("order_id"), record["occurred_at"], received_at, canonical))
                    inserted += 1
                self.db.execute("INSERT INTO event_evidence VALUES (?,?,?)", (record["event_id"], page_id, index))
                if not retain_duplicates:
                    retained.append({"row_index": index, "record": evidence[index]})
            if not retain_duplicates:
                self.db.execute("UPDATE pages SET evidence_json=? WHERE page_id=?", (encode({"retention": "new_events_only", "rows": retained}), page_id))
            self.db.execute("UPDATE streams SET cursor=?,done=?,pages=pages+1,rows_seen=rows_seen+?,inserted=inserted+?,duplicates=duplicates+? WHERE run_id=? AND name=?",
                            (next_cursor, int(not next_cursor), len(records), inserted, duplicates, run_id, stream))
        return inserted, duplicates

    def used_cursor(self, run_id, stream, cursor):
        return self.db.execute("SELECT 1 FROM pages WHERE run_id=? AND stream=? AND request_cursor=? LIMIT 1",
                               (run_id, stream, cursor)).fetchone() is not None

    def events(self, scope_id):
        for row in self.db.execute("SELECT canonical_json, received_at FROM events WHERE scope_id=? ORDER BY COALESCE(occurred_at,received_at), event_id", (scope_id,)):
            record = json.loads(row[0])
            record["received_at"] = row[1]
            yield record

    def report(self, run_id):
        info = self.import_info(run_id)
        return {
            "run_id": run_id, "status": info["status"], "scope_id": info["scope_id"],
            "started_at": info["started_at"], "finished_at": info["finished_at"],
            "config": json.loads(info["config_json"]), "error": info["error"],
            "cutoff_before": json.loads(info["cutoff_before_json"]),
            "cutoff_after": json.loads(info["cutoff_after_json"]) if info["cutoff_after_json"] else None,
            "streams": [dict(row) for row in self.db.execute("SELECT * FROM streams WHERE run_id=? ORDER BY name", (run_id,))],
            "stored_events_in_scope": self.db.execute("SELECT COUNT(*) FROM events WHERE scope_id=?", (info["scope_id"],)).fetchone()[0],
            "coverage": "Completed traversal is not an atomic account snapshot or a live freshness guarantee.",
        }


@contextmanager
def import_lock(db_path):
    """OS-held lock releases on crash, including on Windows; the lock file may remain."""
    path = Path(str(db_path) + ".import.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another importer is using this database") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
