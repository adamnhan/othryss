"""Isolated account overview; never contacts an exchange or delivery provider."""
import json
import sys
from datetime import datetime,timezone,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"tests")]
from othryss.storage import Store
from othryss.account_risk import collect
from test_account_risk import Client
from test_execution import fill,insert_fill
path=Path(sys.argv[1])
if path.exists() or path.resolve().parent!=(ROOT/"artifacts/browser").resolve():raise ValueError("Expected new browser artifact")
with Store(path) as store:
    scope=store.bind_account('local','kalshi','demo','risk-test','fake')
    other=store.bind_account('local','kalshi','demo','empty','fake')
    collect(store,Client(),scope)
    event=fill(event_id='risk-fill')
    event['occurred_at']=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    event['payload'].update(quantity='2.25',fee_usd='0.013')
    insert_fill(store,scope,event);store.db.commit()
print(json.dumps({'scope':scope,'other':other}))
