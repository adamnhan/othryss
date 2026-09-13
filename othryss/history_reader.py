"""Read-only, bounded HTTP views over locally imported evidence. No credentials."""

import json
import sqlite3
from decimal import Decimal
from pathlib import Path

from .replay import display, number
from .collector import freshness


class HistoryReader:
    def __init__(self, path):
        # mode=ro never creates a missing database or runs importer migrations.
        self.db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        if self.db.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, 3, 4, 5, 6, 7):
            self.db.close()
            raise ValueError("Unsupported history database schema")
        self.db.execute("BEGIN")  # Each response sees a consistent committed snapshot.

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def account(self, scope):
        row = self.db.execute("SELECT scope_id, workspace, venue, environment, account FROM accounts WHERE scope_id=?", (scope,)).fetchone()
        if row is None:
            raise ValueError("Unknown account scope")
        return dict(row)

    def accounts(self):
        result = []
        for row in self.db.execute("SELECT scope_id FROM accounts ORDER BY CASE environment WHEN 'production' THEN 0 WHEN 'demo' THEN 1 ELSE 2 END, account"):
            scope = row[0]
            account = self.account(scope)
            account["sync"] = freshness(self.db, scope)
            account["event_counts"] = dict(self.db.execute("SELECT type, COUNT(*) FROM events WHERE scope_id=? GROUP BY type", (scope,)))
            account["order_count"] = self.db.execute("SELECT COUNT(*) FROM (SELECT instrument_id, order_id FROM events WHERE scope_id=? AND order_id IS NOT NULL GROUP BY instrument_id,order_id)", (scope,)).fetchone()[0]
            account["unlinked_events"] = self.db.execute("SELECT COUNT(*) FROM events WHERE scope_id=? AND order_id IS NULL", (scope,)).fetchone()[0]
            runs = self.db.execute("SELECT run_id,status,started_at,finished_at,config_json FROM imports WHERE scope_id=? ORDER BY started_at DESC,run_id DESC", (scope,))
            account["latest_import"] = None
            account["latest_full_traversal"] = None
            for run in runs:
                metadata = {key: run[key] for key in ("run_id", "status", "started_at", "finished_at")}
                metadata["ticker"] = json.loads(run["config_json"]).get("ticker")
                if account["latest_import"] is None:
                    account["latest_import"] = metadata
                if run["status"] == "traversed" and not metadata["ticker"] and json.loads(run["config_json"]).get("mode") != "incremental":
                    account["latest_full_traversal"] = metadata
                    break
            result.append(account)
        return {"accounts": result}

    @staticmethod
    def pagination(limit, offset):
        if not 1 <= limit <= 100 or not 0 <= offset <= 10_000_000:
            raise ValueError("Limit must be 1–100 and offset 0–10000000")

    def orders(self, scope, query="", filled=False, limit=25, offset=0):
        self.pagination(limit, offset)
        self.account(scope)
        if len(query) > 200:
            raise ValueError("Search must be at most 200 characters")
        # instr treats %, _ and quotes literally; all user values stay bound.
        where = "scope_id=? AND order_id IS NOT NULL AND (?='' OR instr(lower(instrument_id),lower(?))>0 OR instr(lower(order_id),lower(?))>0)"
        params = (scope, query, query, query)
        grouped = f"""SELECT instrument_id,order_id,COUNT(*) event_count,
            SUM(CASE WHEN type='ORDER_FILL' THEN 1 ELSE 0 END) fill_count,
            MAX(COALESCE(occurred_at,received_at)) last_evidence_at
            FROM events WHERE {where} GROUP BY instrument_id,order_id"""
        if filled:
            grouped += " HAVING SUM(CASE WHEN type='ORDER_FILL' THEN 1 ELSE 0 END)>0"
        total = self.db.execute(f"SELECT COUNT(*) FROM ({grouped})", params).fetchone()[0]
        rows = self.db.execute(grouped + " ORDER BY last_evidence_at DESC,instrument_id,order_id LIMIT ? OFFSET ?", (*params, limit, offset))
        orders = []
        for row in rows:
            item = dict(row)
            observation = self.db.execute("""SELECT canonical_json FROM events WHERE scope_id=? AND order_id=? AND instrument_id=?
                AND type='ORDER_OBSERVATION' ORDER BY COALESCE(occurred_at,received_at) DESC,event_id DESC LIMIT 1""", (scope, row["order_id"], row["instrument_id"])).fetchone()
            item["status"] = json.loads(observation[0])["payload"].get("status") if observation else None
            orders.append(item)
        return {"orders": orders, "total": total, "limit": limit, "offset": offset}

    def order_groups(self, scope, query="", filled=False, group="", limit=25, offset=0, through=None):
        self.account(scope)
        self.pagination(limit, offset)
        if len(query) > 200 or group not in ("", "active", "inactive", "unknown"):
            raise ValueError("Invalid order filter")
        if through is None:
            through = self.db.execute("SELECT COALESCE(MAX(rowid),0) FROM events WHERE scope_id=?", (scope,)).fetchone()[0]
        if type(through) is not int or through < 0:
            raise ValueError("Invalid order snapshot")
        # Pin subsequent pages to the initial event boundary: new imports cannot
        # move rows between sections midway through expanding a list.
        sql = """WITH relevant AS (
            SELECT instrument_id,order_id,type,occurred_at,received_at,event_id,
            CASE WHEN type='ORDER_OBSERVATION' THEN json_extract(canonical_json,'$.payload.status') END status
            FROM events WHERE scope_id=? AND rowid<=? AND order_id IS NOT NULL
            AND (?='' OR instr(lower(instrument_id),lower(?))>0 OR instr(lower(order_id),lower(?))>0)
        ), observed AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY instrument_id,order_id
                ORDER BY (type='ORDER_OBSERVATION') DESC,COALESCE(occurred_at,received_at) DESC,event_id DESC) latest
            FROM relevant
        ), totals AS (
            SELECT instrument_id,order_id,COUNT(*) event_count,
            SUM(type='ORDER_FILL') fill_count,MAX(COALESCE(occurred_at,received_at)) last_evidence_at,
            MAX(CASE WHEN latest=1 THEN status END) status
            FROM observed GROUP BY instrument_id,order_id HAVING (?=0 OR SUM(type='ORDER_FILL')>0)
        ), classified AS (
            SELECT *,CASE WHEN status='resting' THEN 'active'
            WHEN status IN ('canceled','executed') THEN 'inactive' ELSE 'unknown' END bucket
            FROM totals
        ), ranked AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY last_evidence_at DESC,instrument_id,order_id) ordinal
            FROM classified WHERE (?='' OR bucket=?)
        ) SELECT bucket,COUNT(*) total,
            json_group_array(json_object('instrument_id',instrument_id,'order_id',order_id,
                'status',status,'fill_count',fill_count,'event_count',event_count,
                'last_evidence_at',last_evidence_at,'ordinal',ordinal))
            FILTER (WHERE ordinal>? AND ordinal<=?+CASE WHEN ?='' AND bucket!='active' THEN 5 ELSE ? END) rows_json
            FROM ranked GROUP BY bucket"""
        rows = self.db.execute(sql, (scope,through,query,query,query,int(filled),group,group,offset,offset,group,limit))
        groups = {name: {"orders": [], "total": 0, "offset": offset} for name in ([group] if group else ["active","inactive","unknown"])}
        for row in rows:
            orders = sorted(json.loads(row["rows_json"]), key=lambda item:item["ordinal"])
            for item in orders:
                item.pop("ordinal")
            groups[row["bucket"]] = {"orders":orders,"total":row["total"],"offset":offset}
        return {"groups":groups,"through":through}

    def order(self, scope, instrument, order_id, limit=50, offset=0):
        self.pagination(limit, offset)
        account = self.account(scope)
        params = (scope, instrument, order_id)
        base = "FROM events WHERE scope_id=? AND instrument_id=? AND order_id=?"
        total = self.db.execute("SELECT COUNT(*) " + base, params).fetchone()[0]
        if not total:
            raise ValueError("Order not found in this account and instrument")
        totals = {key: Decimal(0) for key in ("volume", "net_quantity", "fees", "gross_cash_flow")}
        fill_count = 0
        # Stream the selected order only: totals are independent of the visible page.
        for row in self.db.execute("SELECT canonical_json " + base + " AND type='ORDER_FILL'", params):
            p = json.loads(row[0])["payload"]
            qty, price, fee = (number(p[k]) for k in ("quantity", "price_usd", "fee_usd"))
            sign = 1 if p["exposure_direction"] == "increase_yes" else -1
            totals["volume"] += qty
            totals["net_quantity"] += qty * sign
            totals["fees"] += fee
            totals["gross_cash_flow"] -= qty * price * sign
            fill_count += 1
        totals["net_cash_flow"] = totals["gross_cash_flow"] - totals["fees"]
        observation = self.db.execute("SELECT canonical_json,received_at " + base + " AND type='ORDER_OBSERVATION' ORDER BY COALESCE(occurred_at,received_at) DESC,event_id DESC LIMIT 1", params).fetchone()
        latest = self.record(observation) if observation else None
        records = []
        rows = self.db.execute("SELECT canonical_json,received_at " + base + " ORDER BY COALESCE(occurred_at,received_at),event_id LIMIT ? OFFSET ?", (*params, limit, offset))
        for row in rows:
            record = self.record(row)
            # Evidence includes ingestion provenance, never the source page's raw payload.
            record["evidence"] = [dict(e) for e in self.db.execute("""SELECT p.run_id,p.stream,p.page_number,p.received_at,ee.row_index
                FROM event_evidence ee JOIN pages p ON p.page_id=ee.page_id WHERE ee.event_id=?
                ORDER BY p.page_id,ee.row_index LIMIT 20""", (record["event_id"],))]
            record["evidence_count"] = self.db.execute("SELECT COUNT(*) FROM event_evidence WHERE event_id=?", (record["event_id"],)).fetchone()[0]
            records.append(record)
        return {"account": account, "instrument_id": instrument, "order_id": order_id,
                "totals": {k: display(v) for k, v in totals.items()}, "fill_count": fill_count,
                "latest_observation": latest, "events": records, "total": total, "limit": limit, "offset": offset,
                "coverage": "Stored historical evidence. Order snapshots do not establish submit/ACK timing. Fill cash flow is not strategy P&L; settlements and incentives are excluded. Position reconciliation is shown separately for the configured pilot market."}

    @staticmethod
    def record(row):
        record = json.loads(row["canonical_json"])
        record["received_at"] = row["received_at"]
        return record
