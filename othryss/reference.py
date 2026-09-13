"""Decimal reference books and a separate bounded store for market-data evidence."""
import json
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

from .replay import timestamp
from .storage import encode, utc_now

DEFAULT_DB = Path(__file__).resolve().parents[1] / "artifacts/reference/quotes.sqlite"


def decimal(value):
    if not isinstance(value, str) or not re.fullmatch(r"-?\d{1,18}(?:\.\d{1,18})?", value):
        raise ValueError("Invalid fixed-point reference value")
    return Decimal(value)


def price(value):
    result = decimal(value)
    if not 0 <= result <= 1:
        raise ValueError("Reference price outside binary payout")
    return result


class Book:
    def __init__(self):
        self.yes, self.no = {}, {}
        self.ready = False

    def snapshot(self, msg):
        books = []
        for key in ("yes_dollars_fp", "no_dollars_fp"):
            rows = msg.get(key)
            if not isinstance(rows, list) or len(rows) > 20000:
                raise ValueError("Missing or oversized orderbook snapshot")
            book = {}
            for row in rows:
                if not isinstance(row, list) or len(row) != 2:
                    raise ValueError("Invalid price level")
                p, q = price(row[0]), decimal(row[1])
                if q <= 0 or p in book:
                    raise ValueError("Invalid or duplicate snapshot quantity")
                book[p] = q
            books.append(book)
        self.yes, self.no = books
        self.ready = True

    def delta(self, msg):
        if not self.ready or msg.get("side") not in {"yes", "no"}:
            raise ValueError("Delta without a scoped snapshot")
        p, change = price(msg["price_dollars"]), decimal(msg["delta_fp"])
        book = self.yes if msg["side"] == "yes" else self.no
        with localcontext() as context:
            context.prec = 80
            quantity = book.get(p, Decimal(0)) + change
        if not 0 <= quantity < Decimal("1e18"):
            raise ValueError("Delta would create invalid depth")
        if quantity:
            book[p] = quantity
        else:
            book.pop(p, None)
        if len(book) > 20000:
            raise ValueError("Book capacity exceeded")

    def top(self):
        bid = max(self.yes) if self.yes else None
        no_bid = max(self.no) if self.no else None
        ask = Decimal(1) - no_bid if no_bid is not None else None
        quality = "empty" if bid is None and ask is None else "one_sided" if bid is None or ask is None else "crossed" if bid > ask else "locked" if bid == ask else "valid"
        return {"bid": str(bid) if bid is not None else None, "ask": str(ask) if ask is not None else None,
                "bid_size": str(self.yes[bid]) if bid is not None else None,
                "ask_size": str(self.no[no_bid]) if no_bid is not None else None,
                "midpoint": str((bid+ask)/2) if quality == "valid" else None, "quality": quality}


class Feed:
    """Sequence belongs to the subscription, never independently to each ticker."""
    def __init__(self, tickers):
        self.books = {ticker: Book() for ticker in tickers}
        self.sid = None
        self.sequence = None

    def accept(self, message, received_at=None):
        received_at = received_at or utc_now()
        kind = message.get("type")
        if kind == "error":
            raise ValueError("Exchange subscription error")
        if kind == "subscribed":
            msg = message.get("msg", {})
            if msg.get("channel") != "orderbook_delta" or type(msg.get("sid")) is not int or self.sid is not None:
                raise ValueError("Unexpected subscription acknowledgement")
            self.sid = msg["sid"]
            return None
        if kind not in {"orderbook_snapshot", "orderbook_delta", "ok"}:
            raise ValueError("Unexpected reference message type")
        sid, seq = message.get("sid"), message.get("seq")
        if kind == "ok" and seq is None:
            return None
        if type(sid) is not int or sid != self.sid or type(seq) is not int or seq < 1:
            raise ValueError("Missing or conflicting reference sequence identity")
        if self.sequence is not None and seq != self.sequence + 1:
            raise ValueError("Reference sequence gap or replay")
        self.sequence = seq
        if kind == "ok":
            return None
        msg = message.get("msg", {})
        ticker = msg.get("market_ticker")
        if ticker not in self.books:
            raise ValueError("Unsubscribed market in reference stream")
        book = self.books[ticker]
        if kind == "orderbook_snapshot":
            book.snapshot(msg)
        else:
            book.delta(msg)
        source_at = None
        if msg.get("ts_ms") is not None:
            if type(msg["ts_ms"]) is not int:
                raise ValueError("Invalid exchange timestamp")
            source_at = datetime.fromtimestamp(msg["ts_ms"] / 1000, timezone.utc).isoformat(timespec="microseconds")
        elif msg.get("ts") is not None:
            source_at = timestamp(msg["ts"]).isoformat(timespec="microseconds")
        top = book.top()
        if source_at and not -1 <= (timestamp(received_at)-timestamp(source_at)).total_seconds() <= 2:
            top.update(quality="timing_uncertain", midpoint=None)
        return {"schema_version": "reference-1", "instrument_id": ticker, "sid": sid, "sequence": seq,
                "kind": kind, "source_at": source_at, "received_at": received_at,
                "received_monotonic_ns": str(time.monotonic_ns()), "price_basis": "yes_outcome", "currency": "USD",
                "quantity_unit": "contracts", "clock_basis": "exchange_event" if source_at else "local_receipt_only", **top}


class ReferenceStore:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.db.close(); raise ValueError("Unsupported reference schema")
        if version == 0:
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE workers(scope_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, status TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL, last_error TEXT, retention_cutoff TEXT);
                CREATE TABLE markets(scope_id TEXT NOT NULL, instrument_id TEXT NOT NULL, selected INTEGER NOT NULL,
                    status TEXT NOT NULL, verified_at TEXT, last_quote_id INTEGER, metadata_json TEXT NOT NULL,
                    PRIMARY KEY(scope_id,instrument_id));
                CREATE TABLE quotes(quote_id INTEGER PRIMARY KEY, scope_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL, sid INTEGER NOT NULL, sequence INTEGER NOT NULL,
                    received_at TEXT NOT NULL, source_at TEXT, quality TEXT NOT NULL, record_json TEXT NOT NULL,
                    UNIQUE(scope_id,run_id,sid,sequence));
                CREATE INDEX quote_market_time ON quotes(scope_id,instrument_id,received_at);
                CREATE INDEX quote_scope_id ON quotes(scope_id,quote_id);
                CREATE INDEX quote_scope_time ON quotes(scope_id,received_at);
                CREATE TABLE gaps(gap_id INTEGER PRIMARY KEY, scope_id TEXT NOT NULL, instrument_id TEXT NOT NULL,
                    started_at TEXT NOT NULL, ended_at TEXT, reason TEXT NOT NULL);
                CREATE UNIQUE INDEX open_gap ON gaps(scope_id,instrument_id) WHERE ended_at IS NULL;
                PRAGMA user_version=1;
                COMMIT;
            """)
        # Idempotent indexes also cover stores created by the initial local pilot.
        with self.db:
            self.db.execute("CREATE INDEX IF NOT EXISTS quote_scope_id ON quotes(scope_id,quote_id)")
            self.db.execute("CREATE INDEX IF NOT EXISTS quote_scope_time ON quotes(scope_id,received_at)")

    def start(self, scope, run):
        with self.db:
            for row in self.db.execute("SELECT instrument_id FROM markets WHERE scope_id=? AND selected=1", (scope,)).fetchall():
                self.gap(scope, row[0], "worker_restart")
            self.db.execute("INSERT INTO workers(scope_id,run_id,status,heartbeat_at) VALUES (?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET run_id=excluded.run_id,status=excluded.status,heartbeat_at=excluded.heartbeat_at,last_error=NULL", (scope, run, "starting", utc_now()))

    def health(self, scope, status, error=None):
        with self.db:
            self.db.execute("UPDATE workers SET status=?,heartbeat_at=?,last_error=? WHERE scope_id=?", (status, utc_now(), error, scope))

    def watch(self, scope, targets):
        previous = {r[0] for r in self.db.execute("SELECT instrument_id FROM markets WHERE scope_id=? AND selected=1", (scope,))}
        for ticker in previous - targets.keys():
            self.gap(scope, ticker, "market_unwatched")
        with self.db:
            for ticker in previous - targets.keys():
                self.db.execute("UPDATE markets SET selected=0,status='unwatched' WHERE scope_id=? AND instrument_id=?", (scope,ticker))
            for ticker, metadata in targets.items():
                self.db.execute("INSERT INTO markets VALUES (?,?,1,'waiting_snapshot',NULL,NULL,?) ON CONFLICT(scope_id,instrument_id) DO UPDATE SET selected=1,metadata_json=excluded.metadata_json,status=CASE WHEN markets.status='unwatched' THEN 'waiting_snapshot' ELSE markets.status END", (scope, ticker, encode(metadata)))

    def gap(self, scope, ticker, reason):
        with self.db:
            row = self.db.execute("SELECT verified_at FROM markets WHERE scope_id=? AND instrument_id=?", (scope, ticker)).fetchone()
            self.db.execute("INSERT OR IGNORE INTO gaps(scope_id,instrument_id,started_at,reason) VALUES (?,?,?,?)", (scope, ticker, row[0] if row and row[0] else utc_now(), reason))
            self.db.execute("UPDATE markets SET status='gap',verified_at=NULL WHERE scope_id=? AND instrument_id=?", (scope, ticker))

    def append(self, scope, run, record):
        with self.db:
            prior = self.db.execute("SELECT record_json FROM quotes WHERE scope_id=? AND run_id=? AND sid=? AND sequence=?", (scope, run, record["sid"], record["sequence"])).fetchone()
            encoded = encode(record | {"scope_id": scope, "connection_id": run})
            if prior:
                if prior[0] != encoded:
                    raise ValueError("Conflicting immutable reference record")
                return
            cursor = self.db.execute("INSERT INTO quotes(scope_id,run_id,instrument_id,sid,sequence,received_at,source_at,quality,record_json) VALUES (?,?,?,?,?,?,?,?,?)", (scope,run,record["instrument_id"],record["sid"],record["sequence"],record["received_at"],record["source_at"],record["quality"],encoded))
            self.db.execute("UPDATE markets SET last_quote_id=?,status=?,verified_at=? WHERE scope_id=? AND instrument_id=?", (cursor.lastrowid,record["quality"],record["received_at"],scope,record["instrument_id"]))
            if record["kind"] == "orderbook_snapshot":
                self.db.execute("UPDATE gaps SET ended_at=? WHERE scope_id=? AND instrument_id=? AND ended_at IS NULL", (record["received_at"],scope,record["instrument_id"]))

    def verified(self, scope, tickers):
        with self.db:
            self.db.executemany("UPDATE markets SET verified_at=? WHERE scope_id=? AND instrument_id=? AND status!='gap'", [(utc_now(),scope,t) for t in tickers])

    def prune(self, scope, days=3, max_rows=1_000_000):
        cutoff = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat(timespec="microseconds")
        row = self.db.execute("SELECT quote_id,received_at FROM quotes WHERE scope_id=? ORDER BY quote_id DESC LIMIT 1 OFFSET ?", (scope,max_rows)).fetchone()
        with self.db:
            self.db.execute("DELETE FROM quotes WHERE scope_id=? AND received_at<=?", (scope,cutoff))
            if row:
                self.db.execute("DELETE FROM quotes WHERE scope_id=? AND quote_id<=?", (scope,row[0]))
                cutoff = max(cutoff,row[1])
            self.db.execute("DELETE FROM gaps WHERE scope_id=? AND ended_at IS NOT NULL AND ended_at<?", (scope,cutoff))
            # Old unwatched markets and their open-ended gaps aren't an unbounded archive.
            self.db.execute("DELETE FROM gaps WHERE scope_id=? AND started_at<? AND instrument_id IN (SELECT instrument_id FROM markets WHERE scope_id=? AND selected=0) AND instrument_id NOT IN (SELECT instrument_id FROM quotes WHERE scope_id=?)", (scope,cutoff,scope,scope))
            self.db.execute("DELETE FROM markets WHERE scope_id=? AND selected=0 AND instrument_id NOT IN (SELECT instrument_id FROM quotes WHERE scope_id=?) AND instrument_id NOT IN (SELECT instrument_id FROM gaps WHERE scope_id=?)", (scope,scope,scope))
            self.db.execute("UPDATE workers SET retention_cutoff=? WHERE scope_id=?", (cutoff,scope))


def targets(history_db, scope, now=None):
    now = now or datetime.now(timezone.utc)
    db = sqlite3.connect(Path(history_db).resolve().as_uri()+"?mode=ro",uri=True)
    try:
        selected = set()
        for row in db.execute("SELECT health_json FROM telemetry_health WHERE scope_id=?", (scope,)):
            h = json.loads(row[0])
            if not h["stopped"] and -5 <= (now-timestamp(h["heartbeat_at"])).total_seconds() <= 180:
                ticker = h["instrument_id"]
                if re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", ticker): selected.add(ticker)
        if len(selected) > 10:
            raise ValueError("Reference market capacity exceeded")
        return selected
    finally:
        db.close()


def read(path, scope, ticker=None, limit=200):
    if not 1 <= limit <= 500:
        raise ValueError("Reference limit must be 1–500")
    if not Path(path).exists():
        return {"worker": None,"markets": [],"quotes": [],"gaps": []}
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro",uri=True); db.row_factory=sqlite3.Row
    try:
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Unsupported reference schema")
        worker = db.execute("SELECT * FROM workers WHERE scope_id=?",(scope,)).fetchone()
        worker = dict(worker) if worker else None
        now = datetime.now(timezone.utc)
        if worker: worker["stale"] = not -5 <= (now-timestamp(worker["heartbeat_at"])).total_seconds() <= 15
        markets = []
        for row in db.execute("SELECT * FROM markets WHERE scope_id=? ORDER BY selected DESC,instrument_id LIMIT 100",(scope,)):
            m=dict(row); m["metadata"]=json.loads(m.pop("metadata_json"))
            quote=db.execute("SELECT record_json FROM quotes WHERE quote_id=? AND scope_id=?",(m["last_quote_id"],scope)).fetchone()
            m["quote"]=json.loads(quote[0]) if quote else None
            age=(now-timestamp(m["quote"]["received_at"])).total_seconds() if m["quote"] else None
            m["stale"]=age is None or not -1<=age<=30 or not worker or worker["stale"] or worker["status"]!="streaming"
            m["open_gap"]=bool(db.execute("SELECT 1 FROM gaps WHERE scope_id=? AND instrument_id=? AND ended_at IS NULL",(scope,m["instrument_id"])).fetchone())
            m["eligible"]=bool(m["selected"] and not m["stale"] and not m["open_gap"] and m["status"]=="valid")
            markets.append(m)
        params = (scope,ticker) if ticker else (scope,)
        condition = "scope_id=? AND instrument_id=?" if ticker else "scope_id=?"
        quotes=[json.loads(r[0]) for r in db.execute(f"SELECT record_json FROM quotes WHERE {condition} ORDER BY quote_id DESC LIMIT ?",params+(limit,))]
        gaps=[dict(r) for r in db.execute(f"SELECT * FROM gaps WHERE {condition} ORDER BY gap_id DESC LIMIT 50",params)]
        return {"worker":worker,"markets":markets,"quotes":quotes,"gaps":gaps,"limit":limit,
                "coverage":"YES-basis orderbook references. Snapshot times are local receipt times; exchange event times are retained when supplied. Fill markouts apply additional timing and interval checks. History is bounded by three days or one million records per scope."}
    finally:
        db.close()
