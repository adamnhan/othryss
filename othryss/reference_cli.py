"""Independent read-only orderbook worker; never imports or restarts trading bots."""
import argparse
import asyncio
import base64
import json
import signal
import sqlite3
import time
import uuid
from pathlib import Path

from .credentials import credentials
from .fixture import ROOT
from .history_cli import DEFAULT_DB as HISTORY_DB
from .kalshi_client import KalshiClient
from .reference import DEFAULT_DB, Feed, ReferenceStore, targets
from .storage import digest, import_lock, utc_now

HOSTS={"production":"wss://external-api-ws.kalshi.com/trade-api/ws/v2","demo":"wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"}


class SelectionChanged(Exception):
    pass


class SnapshotTimeout(Exception):
    pass


def auth_headers(client):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    stamp=str(time.time_ns()//1_000_000)
    signature=client.key.sign((stamp+"GET/trade-api/ws/v2").encode(),padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=hashes.SHA256.digest_size),hashes.SHA256())
    return {"KALSHI-ACCESS-KEY":client.key_id,"KALSHI-ACCESS-TIMESTAMP":stamp,"KALSHI-ACCESS-SIGNATURE":base64.b64encode(signature).decode()}


async def run(args):
    from websockets.asyncio.client import connect
    # Prevent authenticated handshakes from following redirects to any other host.
    class DirectConnection(connect):
        def process_redirect(self, exc):
            return exc
    key_id,key_file=credentials(args.env_file,"OTHRYSS_KALSHI_KEY_ID",None)
    client=KalshiClient(key_id,key_file,args.environment)
    scope=digest(["local","kalshi",args.environment,args.account])
    db=sqlite3.connect(args.history_db.resolve().as_uri()+"?mode=ro",uri=True)
    row=db.execute("SELECT credential_fingerprint FROM accounts WHERE scope_id=?",(scope,)).fetchone(); db.close()
    if not row or row[0]!=client.fingerprint:
        raise ValueError("Reference worker account is not bound to this read-only key")
    await asyncio.to_thread(client.verify_read_only)
    stop=asyncio.Event()
    previous={sig:signal.signal(sig,lambda *_:stop.set()) for sig in (signal.SIGINT,signal.SIGTERM)}
    def stopping():
        return stop.is_set() or args.stop_file.exists()
    async def pause(seconds):
        until=time.monotonic()+seconds
        while not stopping() and time.monotonic()<until:
            await asyncio.sleep(min(1,until-time.monotonic()))
    # The OS-held lock is distinct from the historical ingestion lock.
    with import_lock(args.db):
        store=ReferenceStore(args.db)
        store.start(scope,uuid.uuid4().hex)
        selection={"markets":{},"updated":0,"error":None}
        last_seen={}
        async def maintenance():
            while not stopping():
                try:
                    current=targets(args.history_db,scope)
                    now=time.monotonic()
                    for ticker in current: last_seen[ticker]=now
                    # Keep watching 60 seconds after a bot departs for future 30s markouts.
                    selected={t for t,seen in last_seen.items() if now-seen<=60}
                    for ticker in set(last_seen)-selected: del last_seen[ticker]
                    if len(selected)>10: raise ValueError("Market capacity exceeded")
                    metadata={}
                    for ticker in sorted(selected):
                        response=await asyncio.to_thread(client.request,"/markets/"+ticker)
                        m=response.get("market",{})
                        if m.get("ticker")!=ticker or m.get("market_type")!="binary":
                            raise ValueError("Unsupported market identity or economics")
                        if m.get("status")=="active":
                            metadata[ticker]={"status":"active","checked_at":utc_now(),"watch_reason":"active_bot" if ticker in current else "post_bot_tail"}
                    store.watch(scope,metadata)
                    selection.update(markets=metadata,updated=time.monotonic(),error=None)
                    store.prune(scope)
                except Exception:
                    selection.update(markets={},updated=time.monotonic(),error="Market selection or lifecycle check unavailable")
                await pause(30)
        task=asyncio.create_task(maintenance())
        backoff=1
        connected=set()
        try:
            while not stopping():
                if task.done(): await task
                wanted=set(selection["markets"])
                if not wanted:
                    store.health(scope,"waiting_markets",selection["error"])
                    await pause(1); continue
                feed=Feed(wanted); connection=uuid.uuid4().hex
                connected=wanted
                for ticker in wanted: store.gap(scope,ticker,"awaiting_snapshot")
                try:
                    store.health(scope,"connecting")
                    async with DirectConnection(HOSTS[args.environment],additional_headers=auth_headers(client),proxy=None,
                            open_timeout=10,close_timeout=2,ping_interval=5,ping_timeout=5,max_size=2*1024*1024,max_queue=16) as ws:
                        await ws.send(json.dumps({"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers":sorted(wanted)}}))
                        next_snapshot=time.monotonic()+15; next_ping=time.monotonic()+5; command=2
                        snapshots={t:time.monotonic() for t in wanted}
                        while not stopping():
                            if set(selection["markets"])!=wanted or time.monotonic()-selection["updated"]>60:
                                raise SelectionChanged()
                            if any(time.monotonic()-last>30 for last in snapshots.values()):
                                raise SnapshotTimeout()
                            if time.monotonic()>=next_snapshot:
                                if feed.sid is None or not all(b.ready for b in feed.books.values()):
                                    raise SnapshotTimeout()
                                await ws.send(json.dumps({"id":command,"cmd":"update_subscription","params":{"sids":[feed.sid],"market_tickers":sorted(wanted),"action":"get_snapshot"}}))
                                command+=1; next_snapshot=time.monotonic()+15
                            if time.monotonic()>=next_ping:
                                pong=await ws.ping()
                                await asyncio.wait_for(pong,timeout=5)
                                store.verified(scope,[t for t,b in feed.books.items() if b.ready])
                                next_ping=time.monotonic()+5
                            try:
                                raw=await asyncio.wait_for(ws.recv(),timeout=0.5)
                            except asyncio.TimeoutError:
                                store.health(scope,"streaming"); continue
                            record=feed.accept(json.loads(raw),utc_now())
                            if record:
                                store.append(scope,connection,record)
                                if record["kind"]=="orderbook_snapshot": snapshots[record["instrument_id"]]=time.monotonic()
                                backoff=1
                            store.health(scope,"streaming")
                except Exception as exc:
                    reason="market_selection_changed" if isinstance(exc,SelectionChanged) else "snapshot_timeout" if isinstance(exc,SnapshotTimeout) else "sequence_or_message_invalid" if isinstance(exc,(ValueError,KeyError,TypeError)) else "connection_lost"
                    for ticker in wanted: store.gap(scope,ticker,reason)
                    store.health(scope,"recovering",reason)
                    print(json.dumps({"reference_status":"recovering","reason":reason}),flush=True)
                    await pause(backoff); backoff=min(30,backoff*2)
        finally:
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            for ticker in connected: store.gap(scope,ticker,"worker_stopped")
            store.health(scope,"stopped")
            store.db.close()
            for sig,handler in previous.items(): signal.signal(sig,handler)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account",required=True)
    parser.add_argument("--environment",choices=list(HOSTS),default="production")
    parser.add_argument("--db",type=Path,default=DEFAULT_DB)
    parser.add_argument("--history-db",type=Path,default=HISTORY_DB)
    parser.add_argument("--env-file",type=Path,default=ROOT/"local.env")
    parser.add_argument("--stop-file",type=Path,default=ROOT/"artifacts/reference/worker.stop")
    args=parser.parse_args()
    if args.db.resolve()==args.history_db.resolve():
        parser.error("Reference data needs a separate database")
    if args.stop_file.exists():
        print("Reference stop file exists; worker not started."); return 0
    try:
        asyncio.run(run(args)); return 0
    except KeyboardInterrupt: return 130
    except Exception as exc:
        print(f"Reference worker unavailable ({type(exc).__name__}); inspect configuration and worker lock. No exchange writes are supported.")
        return 1


if __name__=="__main__": raise SystemExit(main())
