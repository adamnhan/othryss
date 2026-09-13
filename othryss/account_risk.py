"""Primary-subaccount overview and configurable, evidence-backed inventory limits."""
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .kalshi import fixed
from .replay import number, timestamp
from .storage import encode, utc_now
from .incidents import record_check

DEFAULT = {"enabled": False, "per_market_limit": None, "total_limit": None, "overrides": [], "grace_seconds": 120}
FRESH_SECONDS = 180
MAX_POSITIONS = 10000
MAX_FILLS = 100000
TICKER = r"[A-Za-z0-9_.:-]{1,200}"


def decimal(value, *, signed=False):
    if not isinstance(value,(str,int)) or isinstance(value,bool) or len(str(value)) > 40:
        raise ValueError("Invalid decimal value")
    if not re.fullmatch(r"-?[0-9]{1,13}(?:\.[0-9]{1,12})?",str(value)):
        raise ValueError("Use fixed-point decimals with at most 12 fractional digits")
    result = number(value)
    if abs(result) > Decimal("1000000000000") or (not signed and result < 0):
        raise ValueError("Value outside supported bounds")
    return result


def validate_config(config):
    if not isinstance(config,dict) or set(config) != set(DEFAULT) or type(config["enabled"]) is not bool:
        raise ValueError("Invalid inventory configuration")
    result = dict(config)
    for key in ("per_market_limit","total_limit"):
        if result[key] is not None: result[key] = fixed(decimal(result[key]))
    if type(result["grace_seconds"]) is not int or not 30 <= result["grace_seconds"] <= 3600:
        raise ValueError("Grace must be 30–3600 seconds")
    if not isinstance(result["overrides"],list) or len(result["overrides"]) > 100:
        raise ValueError("At most 100 market limits are supported")
    seen = set(); overrides = []
    for rule in result["overrides"]:
        if not isinstance(rule,dict) or set(rule) != {"ticker","limit"} or not isinstance(rule["ticker"],str) or not re.fullmatch(TICKER,rule["ticker"]) or rule["ticker"] in seen:
            raise ValueError("Invalid or duplicate market override")
        seen.add(rule["ticker"]); overrides.append({"ticker":rule["ticker"],"limit":fixed(decimal(rule["limit"]))})
    result["overrides"] = sorted(overrides,key=lambda r:r["ticker"])
    if result["enabled"] and result["per_market_limit"] is None and result["total_limit"] is None and not overrides:
        raise ValueError("Configure at least one limit before enabling alerts")
    return result


def settings(db, scope):
    row = db.execute("SELECT * FROM risk_settings WHERE scope_id=?",(scope,)).fetchone()
    return {"revision":row["revision"],"updated_at":row["updated_at"],"config":json.loads(row["config_json"])} if row else {"revision":0,"updated_at":None,"config":dict(DEFAULT)}


def save_settings(path, scope, revision, config):
    config = validate_config(config)
    if type(revision) is not int or revision < 0: raise ValueError("Invalid settings revision")
    db = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=rw",uri=True,timeout=10);db.row_factory=sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM accounts WHERE scope_id=?",(scope,)).fetchone(): raise ValueError("Unknown account scope")
        old = settings(db,scope)
        if revision != old["revision"]: raise ValueError("Settings changed; reload them before saving")
        if old["config"] == config:
            db.rollback();return old
        now=utc_now()
        db.execute("INSERT OR REPLACE INTO risk_settings VALUES (?,?,?,?)",(scope,revision+1,now,encode(config)))
        # A settings change is not evidence of recovery. Retain old incidents and
        # suspend their assessment; revised limits start an independent lifecycle.
        db.execute("UPDATE incidents SET assessment='unknown',hits=0,clears=0 WHERE scope_id=? AND rule='inventory_limit' AND status!='resolved'",(scope,))
        db.commit();return settings(db,scope)
    finally:db.close()


def normalize_balance(raw):
    for key in ("balance","portfolio_value","updated_ts"):
        if type(raw.get(key)) is not int or raw[key] < 0: raise ValueError("Balance response incomplete")
    if raw.get("subaccount_number",0) != 0: raise ValueError("Balance subaccount mismatch")
    return {"available_balance_usd":fixed(Decimal(raw["balance"])/100),
            "portfolio_value_usd":fixed(Decimal(raw["portfolio_value"])/100),
            "source_updated_at":datetime.fromtimestamp(raw["updated_ts"],timezone.utc).isoformat(),
            "source_units":"cents", "currency":"USD"}


def position_rows(client, progress=None):
    result=[];cursor="";cursors=set();tickers=set()
    for _ in range(20):
        params={"subaccount":0,"limit":500,"count_filter":"position"}
        if cursor:params["cursor"]=cursor
        raw=client.request("/portfolio/positions",params)
        if not isinstance(raw.get("market_positions"),list) or not isinstance(raw.get("cursor"),str): raise ValueError("Position pages incomplete")
        for row in raw["market_positions"]:
            if not isinstance(row,dict) or row.get("subaccount_number",0) != 0 or row.get("exchange_index",0) != 0:
                raise ValueError("Unsupported position scope")
            ticker=row.get("ticker")
            if not isinstance(ticker,str) or not re.fullmatch(TICKER,ticker) or ticker in tickers: raise ValueError("Duplicate or invalid market position")
            tickers.add(ticker)
            qty=decimal(row.get("position_fp"),signed=True)
            result.append({"instrument_id":ticker,"quantity":fixed(qty),"absolute_quantity":fixed(abs(qty)),
                           "source_updated_at":row.get("last_updated_ts"),"position_basis":"signed_yes_contracts"})
            if len(result)>MAX_POSITIONS:raise ValueError("Position count exceeds capture budget")
        if progress:progress({"phase":"account_overview","endpoint":"/portfolio/positions"})
        cursor=raw["cursor"]
        if not cursor:return sorted(result,key=lambda p:p["instrument_id"])
        if cursor in cursors:raise ValueError("Position cursor repeated")
        cursors.add(cursor)
    raise ValueError("Position page budget exceeded")


def collect(store, client, scope, *, progress=None):
    started=utc_now()
    evidence={"subaccount":0,"balance":None,"positions":None,"balance_error":None,"positions_error":None,
              "position_filter":"position", "positions_complete":False,
              "interpretation":"Primary subaccount only. Sequential REST reads are not an atomic account snapshot; resting-order potential exposure is excluded."}
    try:
        permission=client.verify_read_only()
        if permission.get("scopes")!=["read"] or permission.get("subaccount") not in (None,0): raise ValueError("Read key does not cover primary subaccount")
    except Exception:
        evidence.update(balance_error="read_scope_unavailable",positions_error="read_scope_unavailable")
    else:
        try:
            evidence["balance"]=normalize_balance(client.request("/portfolio/balance",{"subaccount":0}))
        except Exception:evidence["balance_error"]="balance_capture_incomplete"
        if progress:progress({"phase":"account_overview","endpoint":"/portfolio/balance"})
        try:
            evidence["positions"]=position_rows(client,progress)
            evidence["positions_complete"]=True
        except Exception:evidence["positions_error"]="position_capture_incomplete"
    received=utc_now();snapshot={"snapshot_id":uuid.uuid4().hex,"scope_id":scope,"subaccount":0,"started_at":started,"received_at":received,"evidence":evidence}
    with store.db:
        store.db.execute("INSERT INTO account_snapshots VALUES (?,?,?,?,?,?)",(snapshot["snapshot_id"],scope,0,started,received,encode(evidence)))
        store.db.execute("DELETE FROM account_snapshots WHERE scope_id=? AND subaccount=0 AND snapshot_id NOT IN (SELECT snapshot_id FROM account_snapshots WHERE scope_id=? AND subaccount=0 ORDER BY rowid DESC LIMIT 100)",(scope,scope))
    evaluate(store,scope,snapshot)
    return snapshot


def valid_positions(snapshot, now):
    return bool(snapshot and snapshot["subaccount"]==0 and snapshot["evidence"]["positions_complete"] and
                snapshot["evidence"]["positions"] is not None and
                -5 <= (now-timestamp(snapshot["started_at"])).total_seconds() <= FRESH_SECONDS and
                -5 <= (now-timestamp(snapshot["received_at"])).total_seconds() <= FRESH_SECONDS)


def assess(snapshot, config, now):
    result={"status":"unavailable","reason":"Position capture is incomplete or stale", "rule_version":"inventory-limit-1",
            "evaluated_rules":[],"findings":[],"config":config,"snapshot":snapshot}
    if not valid_positions(snapshot,now):return result
    positions=snapshot["evidence"]["positions"]
    by_market={p["instrument_id"]:decimal(p["quantity"],signed=True) for p in positions}
    overrides={r["ticker"]:decimal(r["limit"]) for r in config["overrides"]}
    total=sum((abs(q) for q in by_market.values()),Decimal(0))
    result.update(status="clear",reason="Complete position capture is within configured limits",evaluated_rules=["inventory_limit"],absolute_contracts=fixed(total))
    def breach(entity,actual,limit):
        result["findings"].append({"rule":"inventory_limit","entity":entity,"message":"Observed absolute inventory exceeds configured limit",
                                  "details":{"observed_absolute_contracts":fixed(actual),"limit_contracts":fixed(limit),"subaccount":0}})
    if config["total_limit"] is not None and total>decimal(config["total_limit"]):breach("primary:total",total,decimal(config["total_limit"]))
    for ticker,qty in by_market.items():
        limit=overrides.get(ticker,decimal(config["per_market_limit"]) if config["per_market_limit"] is not None else None)
        if limit is not None and abs(qty)>limit:breach(ticker,abs(qty),limit)
    if result["findings"]:result.update(status="difference",reason="Inventory limit exceeded; persistence checks determine incident opening")
    return result


def evaluate(store, scope, snapshot, now=None):
    now=now or datetime.now(timezone.utc)
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        configured=settings(store.db,scope);config=configured["config"]
        if not config["enabled"]:return
        if snapshot["scope_id"]!=scope:raise ValueError("Risk snapshot scope mismatch")
        if configured["updated_at"] and timestamp(snapshot["started_at"])<timestamp(configured["updated_at"]):return
        session=f"risk:primary:{configured['revision']}"
        monitor={"scope_id":scope,"session_id":session,"instrument_id":"PRIMARY-ACCOUNT","grace_seconds":config["grace_seconds"]}
        result=assess(snapshot,config,now)
        result["settings_revision"]=configured["revision"]
        record_check(store,monitor,snapshot["snapshot_id"],result,now)


def fill_summary(db, scope, window, now):
    if window not in {"24h","7d","all"}:raise ValueError("Invalid fill window")
    since=None if window=="all" else now-timedelta(hours=24 if window=="24h" else 168)
    where="scope_id=? AND type='ORDER_FILL' AND occurred_at<=?";args=[scope,now.isoformat(timespec="microseconds")]
    if since:where+=" AND occurred_at>=?";args.append(since.isoformat(timespec="microseconds"))
    rows=db.execute(f"SELECT canonical_json FROM events WHERE {where} ORDER BY occurred_at DESC,event_id LIMIT ?",[*args,MAX_FILLS+1])
    count=0;volume=Decimal(0);fees=Decimal(0);excluded=0;other=0;invalid=0;markets={};liquidity={k:0 for k in ("maker","taker","unknown")};scanned=0
    for row in rows:
        scanned+=1
        if scanned>MAX_FILLS:break
        event=json.loads(row[0]);p=event["payload"]
        if p.get("subaccount") is None:excluded+=1;continue
        if p["subaccount"]!=0:other+=1;continue
        try:
            if p["quantity_unit"]!="contracts" or p["currency"]!="USD":raise ValueError()
            qty=decimal(p["quantity"]);fee=decimal(p["fee_usd"],signed=True)
            if qty<=0:raise ValueError()
        except (KeyError,TypeError,ValueError):invalid+=1;continue
        count+=1;volume+=qty;fees+=fee
        liquidity[p.get("liquidity") if p.get("liquidity") in liquidity else "unknown"]+=1
        m=markets.setdefault(event["instrument_id"],{"fills":0,"volume":Decimal(0),"fees":Decimal(0)})
        m["fills"]+=1;m["volume"]+=qty;m["fees"]+=fee
    return {"window":window,"since":since.isoformat() if since else None,"through":now.isoformat(),"fill_count":count,"volume_contracts":fixed(volume),
            "fees_usd":fixed(fees),"liquidity_counts":liquidity,"unknown_subaccount_excluded":excluded,"other_subaccounts_excluded":other,
            "invalid_fills_excluded":invalid,"truncated":scanned>MAX_FILLS,
            "market_count":len(markets),"markets_truncated":len(markets)>100,
            "markets":[{"instrument_id":k,"fill_count":v["fills"],"volume_contracts":fixed(v["volume"]),"fees_usd":fixed(v["fees"])} for k,v in sorted(markets.items(),key=lambda item:(-item[1]["volume"],item[0]))[:100]],
            "coverage":"Imported fills in this window, primary subaccount only. Fees exclude incentives and settlements; this is not P&L or a fill-rate denominator."}


def catalog(reader, scope, window="24h", offset=0, now=None):
    reader.account(scope)
    if not 0<=offset<=MAX_POSITIONS:raise ValueError("Invalid position offset")
    now=now or datetime.now(timezone.utc);db=reader.db
    if db.execute("PRAGMA user_version").fetchone()[0]<6:
        return {"available":False,"reason":"Account overview needs collector schema upgrade"}
    row=db.execute("SELECT * FROM account_snapshots WHERE scope_id=? AND subaccount=0 ORDER BY rowid DESC LIMIT 1",(scope,)).fetchone()
    snapshot=dict(row) if row else None
    if snapshot:snapshot["evidence"]=json.loads(snapshot.pop("evidence_json"))
    configured=settings(db,scope)
    fresh=valid_positions(snapshot,now)
    positions=snapshot["evidence"]["positions"] if snapshot and snapshot["evidence"]["positions"] is not None else []
    nonzero=[p for p in positions if decimal(p["quantity"],signed=True)!=0]
    total=sum((decimal(p["absolute_quantity"]) for p in nonzero),Decimal(0))
    balance=snapshot["evidence"]["balance"] if snapshot else None
    balance_fresh=bool(balance and -5<=(now-timestamp(snapshot["started_at"])).total_seconds()<=FRESH_SECONDS and
                       timestamp(balance["source_updated_at"])<=now+timedelta(seconds=5))
    from .collector import freshness
    checks=db.execute("SELECT checked_at,status,result_json FROM bot_checks WHERE scope_id=? AND session_id=? ORDER BY checked_at DESC LIMIT 1",(scope,f"risk:primary:{configured['revision']}")).fetchone()
    return {"available":True,"scope_id":scope,"subaccount":0,"snapshot_id":snapshot["snapshot_id"] if snapshot else None,
            "observed_at":snapshot["received_at"] if snapshot else None,"started_at":snapshot["started_at"] if snapshot else None,
            "balance":balance,"balance_fresh":balance_fresh,"positions_fresh":fresh,
            "errors":{k:snapshot["evidence"][k] if snapshot else "not_captured" for k in ("balance_error","positions_error")},
            "positions":nonzero[offset:offset+25],"position_count":len(nonzero),"position_offset":offset,
            "absolute_contracts":fixed(total) if fresh else None,"settings":configured,
            "risk_check":{"checked_at":checks[0],"status":checks[1],"reason":json.loads(checks[2])["reason"]} if checks else None,
            "fills":fill_summary(db,scope,window,now),"sync":freshness(db,scope),
            "coverage":"Primary subaccount (0), all returned markets. Missing or stale snapshots cannot establish zero inventory. Absolute contracts are an activity measure, not dollar risk; resting orders and other subaccounts are excluded."}
