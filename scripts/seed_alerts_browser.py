"""Isolated delivery UI fixtures; all sends use an injected local fake."""
import json
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"tests")]
from othryss import alerts
from othryss.storage import Store
from othryss.alerts_cli import heartbeat
from test_alerts import route,seed_incident,DISCORD,WEBHOOK

history,queue=map(Path,sys.argv[1:])
for path in (history,queue):
    if path.exists() or path.resolve().parent!=(ROOT/"artifacts/browser").resolve():raise ValueError("Expected new browser artifacts")
with Store(history) as store:
    scope=store.bind_account("local","kalshi","demo","alerts-test","fake")
    other=store.bind_account("local","kalshi","demo","empty","fake")
    incident=seed_incident(store,scope,time.time())
    routes=[route(scope),route(scope,"webhook","hook")]
    db=alerts.connect(queue)
    try:
        alerts.sync_routes(db,store.db,routes,{"discord":DISCORD,"hook":WEBHOOK},time.time())
        for i,(r,result) in enumerate([(routes[0],{"status":"accepted","provider_id":"123"}),(routes[1],{"status":"retry","http_status":429,"retry_after":120,"error":"rate_limited"})]):
            with db:alerts.enqueue(db,r,f"test-{i}",incident,"test",time.time())
            alerts.deliver_one(db,store.db,r,DISCORD if i==0 else WEBHOOK,time.time(),lambda *_,r=result:r)
        with db:
            alerts.enqueue(db,routes[0],"unknown",incident,"test",time.time())
            db.execute("UPDATE deliveries SET status='unknown',error='worker_interrupted' WHERE event_key='unknown'")
        heartbeat(db,"running")
    finally:db.close()
print(json.dumps({"scope":scope,"other":other}))
