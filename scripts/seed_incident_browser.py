"""Synthetic incident database exclusively for the browser acceptance test."""
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"tests")]
from test_bot_state import bot, exchange, SESSION
from othryss.storage import Store, encode
from othryss.bot_state import compare
from othryss.incidents import record_check

parser=argparse.ArgumentParser(); parser.add_argument("--db",type=Path,required=True); parser.add_argument("--clear",action="store_true"); args=parser.parse_args()
assert args.db.resolve().parent == (ROOT/"artifacts/browser").resolve(), "Synthetic database must stay in browser artifacts"
with Store(args.db) as store:
    scope=store.bind_account("local","kalshi","demo","test","key")
    store.bind_account("local","kalshi","demo","zzz-other","other")
    monitor={"scope_id":scope,"session_id":SESSION,"instrument_id":"TEST-MARKET","subaccount":0,"grace_seconds":120}
    store.db.execute("INSERT OR IGNORE INTO bot_monitors(scope_id,session_id,instrument_id,subaccount,grace_seconds) VALUES (?,?,?,?,?)",tuple(monitor.values()))
    now=datetime.now(timezone.utc)
    for r in (bot(),bot(2,10)):
        store.db.execute("INSERT OR IGNORE INTO telemetry_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",(scope,r["event_id"],SESSION,r["sequence"],None,r["instrument_id"],0,r["occurred_at"],r["occurred_at"],"BOT_STATE",encode(r)))
    health={"heartbeat_at":now.isoformat(),"instrument_id":"TEST-MARKET","stopped":False,"dropped":0,"write_failures":0,"capped":False,"queue_depth":0}
    store.db.execute("INSERT OR REPLACE INTO telemetry_health VALUES (?,?,?,?)",(scope,SESSION,now.isoformat(),encode(health)))
    store.db.commit()
    for age in ([0] if args.clear else [130,70,10]):
        when=now-timedelta(seconds=age)
        snapshot=exchange(scope,position="1")
        snapshot.update(snapshot_id=uuid.uuid4().hex,started_at=(when-timedelta(seconds=4)).isoformat(),received_at=(when-timedelta(seconds=3)).isoformat())
        changes={"position":"1","inventory":"1"} if args.clear else {}
        before=bot(**changes); before["occurred_at"]=(when-timedelta(seconds=10)).isoformat()
        after=bot(2,10,**changes); after["occurred_at"]=when.isoformat()
        result=compare(before,after,snapshot)
        store.db.execute("INSERT INTO bot_exchange VALUES (?,?,?,?,?,?,?)",(snapshot["snapshot_id"],scope,"TEST-MARKET",0,snapshot["started_at"],snapshot["received_at"],encode(snapshot["evidence"]))); store.db.commit()
        record_check(store,monitor,snapshot["snapshot_id"],result,when)
    print(json.dumps({"scope":scope}))
