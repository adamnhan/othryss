"""Reference economics, sequencing, evidence isolation and failure recovery."""
import copy
import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from othryss.reference import Book, Feed, ReferenceStore, read, targets
from othryss.server import Handler
from othryss.storage import Store, encode, utc_now
from othryss.reference_cli import run


def snapshot(ticker="TEST", seq=1, **changes):
    return {"type":"orderbook_snapshot","sid":7,"seq":seq,"msg":{
        "market_ticker":ticker,"yes_dollars_fp":[["0.40","10.25"],["0.42","3.50"]],
        "no_dollars_fp":[["0.55","12.75"]], **changes}}


def feed(tickers=("TEST",)):
    result=Feed(tickers)
    result.accept({"type":"subscribed","msg":{"channel":"orderbook_delta","sid":7}})
    return result


def delta(seq=2, ticker="TEST", **changes):
    return {"type":"orderbook_delta","sid":7,"seq":seq,"msg":{
        "market_ticker":ticker,"side":"yes","price_dollars":"0.42","delta_fp":"-3.50",**changes}}


class ReferenceBookTests(unittest.TestCase):
    def test_yes_complement_decimal_depth_and_level_removal(self):
        f=feed(); quote=f.accept(snapshot())
        self.assertEqual((quote["bid"],quote["ask"],quote["midpoint"]),("0.42","0.45","0.435"))
        self.assertEqual((quote["bid_size"],quote["ask_size"]),("3.50","12.75"))
        quote=f.accept(delta())
        self.assertEqual(quote["bid"],"0.40")
        quote=f.accept(delta(3,side="no",price_dollars="0.56",delta_fp="0.25"))
        self.assertEqual((quote["ask"],quote["ask_size"]),("0.44","0.25"))
        f=feed(); f.accept(snapshot(yes_dollars_fp=[["0.42","123456789012345678.123456789012345678"]]))
        quote=f.accept(delta(delta_fp="0.000000000000000001"))
        self.assertEqual(quote["bid_size"],"123456789012345678.123456789012345679")

    def test_one_sided_empty_locked_and_crossed_have_no_midpoint(self):
        for yes,no,quality in [([],[],"empty"),([["0.4","1"]],[],"one_sided"),
                ([["0.4","1"]],[["0.6","1"]],"locked"),([["0.5","1"]],[["0.6","1"]],"crossed")]:
            with self.subTest(quality=quality):
                quote=feed().accept(snapshot(yes_dollars_fp=yes,no_dollars_fp=no))
                self.assertEqual(quote["quality"],quality); self.assertIsNone(quote["midpoint"])

    def test_invalid_depth_fails_closed(self):
        for rows in [None, [["0.4","1"],["0.40","2"]],[["NaN","1"]],[["1.01","1"]],
                     [["0.4","-1"]],[[0.4,"1"]],[["0.4","0"]],[["0.4","1e2"]]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                feed().accept(snapshot(yes_dollars_fp=rows))
        with self.assertRaises(ValueError): feed().accept(delta(1))
        for change in ({"delta_fp":"-4"},{"side":"maybe"}):
            f=feed(); f.accept(snapshot())
            with self.assertRaises(ValueError): f.accept(delta(**change))

    def test_subscription_sequence_is_global_across_markets_and_control_ack(self):
        f=feed(("A","B")); f.accept(snapshot("A",10)); f.accept(snapshot("B",11))
        f.accept({"type":"ok","sid":7,"seq":12,"msg":{}})
        self.assertEqual(f.accept(delta(13,"A"))["sequence"],13)
        for message in [snapshot("B",15),snapshot("B",13),snapshot("C",14),snapshot("B",14)|{"sid":8}]:
            with self.subTest(message=message), self.assertRaises(ValueError):
                copy.deepcopy(f).accept(message)

    def test_receipt_and_source_clocks_are_distinct_and_delay_is_ineligible(self):
        now=datetime.now(timezone.utc)
        f=feed(); quote=f.accept(snapshot(),now.isoformat())
        self.assertIsNone(quote["source_at"])
        self.assertEqual(quote["clock_basis"],"local_receipt_only")
        for seconds,quality in [(0,"valid"),(-3,"timing_uncertain"),(2,"timing_uncertain")]:
            source=now+timedelta(seconds=seconds)
            quote=copy.deepcopy(f).accept(delta(ts_ms=int(source.timestamp()*1000)),now.isoformat())
            self.assertEqual(quote["quality"],quality)
            self.assertEqual(quote["clock_basis"],"exchange_event")


class ReferenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"reference.sqlite"
        self.store=ReferenceStore(self.path); self.addCleanup(self.store.db.close)
        self.store.start("scope","worker")
        self.store.watch("scope",{"TEST":{"status":"active"}})
        self.f=feed()

    def append(self, record=None, scope="scope", run="connection"):
        self.store.append(scope,run,record or self.f.accept(snapshot()))
        self.store.health(scope,"streaming")

    def test_immutable_dedup_scope_and_connection_identity(self):
        quote=self.f.accept(snapshot()); self.append(quote); self.append(quote)
        self.assertEqual(len(read(self.path,"scope")["quotes"]),1)
        with self.assertRaises(ValueError): self.append(quote|{"bid":"0.41"})
        self.append(quote,scope="other"); self.append(quote,run="new-connection")
        data=read(self.path,"scope")
        self.assertEqual(len(data["quotes"]),2)
        self.assertEqual(data["quotes"][0]["connection_id"],"new-connection")
        self.assertEqual(data["quotes"][0]["scope_id"],"scope")
        self.assertEqual(read(self.path,"missing")["quotes"],[])

    def test_gap_requires_snapshot_then_restart_invalidates_again(self):
        self.append(); self.assertTrue(read(self.path,"scope")["markets"][0]["eligible"])
        self.store.gap("scope","TEST","sequence_gap")
        self.append(self.f.accept(delta()))
        self.assertFalse(read(self.path,"scope")["markets"][0]["eligible"])
        self.append(self.f.accept(snapshot(seq=3)))
        data=read(self.path,"scope")
        self.assertTrue(data["markets"][0]["eligible"]); self.assertIsNotNone(data["gaps"][0]["ended_at"])
        self.store.start("scope","restart")
        self.assertFalse(read(self.path,"scope")["markets"][0]["eligible"])

    def test_stale_worker_quote_and_unwatched_market_are_ineligible(self):
        self.append(); old=(datetime.now(timezone.utc)-timedelta(seconds=40)).isoformat()
        with self.store.db: self.store.db.execute("UPDATE workers SET heartbeat_at=?",(old,))
        self.assertTrue(read(self.path,"scope")["worker"]["stale"])
        self.append(self.f.accept(snapshot(seq=2),old))
        self.assertFalse(read(self.path,"scope")["markets"][0]["eligible"])
        self.append(self.f.accept(snapshot(seq=3)))
        self.store.watch("scope",{})
        self.assertFalse(read(self.path,"scope")["markets"][0]["eligible"])
        self.store.watch("scope",{"TEST":{"status":"active"}})
        self.assertEqual(read(self.path,"scope")["markets"][0]["status"],"waiting_snapshot")

    def test_watch_refresh_keeps_book_and_retention_is_scoped_and_bounded(self):
        for i in range(1,6): self.append(self.f.accept(snapshot(seq=i)))
        self.store.watch("scope",{"TEST":{"status":"active"}})
        self.assertTrue(read(self.path,"scope")["markets"][0]["eligible"])
        self.append(feed().accept(snapshot()),scope="other")
        self.store.prune("scope",max_rows=2)
        self.assertEqual(len(read(self.path,"scope")["quotes"]),2)
        self.assertEqual(len(read(self.path,"other")["quotes"]),1)
        self.assertIsNotNone(read(self.path,"scope")["worker"]["retention_cutoff"])
        self.assertEqual(len(read(self.path,"scope",limit=1)["quotes"]),1)
        with self.assertRaises(ValueError): read(self.path,"scope",limit=501)

    def test_fresh_bot_selection_scope_stop_and_capacity(self):
        history=Path(self.temp.name)/"history.sqlite"
        with Store(history) as h:
            scope=h.bind_account("local","kalshi","demo","test","key")
            other=h.bind_account("local","kalshi","demo","other","other-key")
            now=datetime.now(timezone.utc)
            for session,ticker,age,stopped,account in [("1","A",0,False,scope),("2","B",181,False,scope),("3","C",0,True,scope),("4","D",0,False,other)]:
                data={"instrument_id":ticker,"heartbeat_at":(now-timedelta(seconds=age)).isoformat(),"stopped":stopped}
                h.db.execute("INSERT INTO telemetry_health VALUES (?,?,?,?)",(account,session,utc_now(),encode(data)))
            h.db.commit()
            self.assertEqual(targets(history,scope,now),{"A"})
            for i in range(11):
                h.db.execute("INSERT INTO telemetry_health VALUES (?,?,?,?)",(scope,str(i+10),utc_now(),encode({"instrument_id":f"X{i}","heartbeat_at":now.isoformat(),"stopped":False})))
            h.db.commit()
            with self.assertRaises(ValueError): targets(history,scope,now)

    def test_http_scoped_read_export_bounds_and_no_write_route(self):
        history=Path(self.temp.name)/"history.sqlite"
        with Store(history) as h: scope=h.bind_account("local","kalshi","demo","test","key")
        self.store.start(scope,"w"); self.store.watch(scope,{"TEST":{}})
        self.append(scope=scope)
        handler=type("TestHandler",(Handler,),{"history_db":history,"reference_db":self.path,"log_message":lambda *_:None})
        server=ThreadingHTTPServer(("127.0.0.1",0),handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            base=f"http://127.0.0.1:{server.server_port}/api/history/references"
            with urlopen(base+f"?scope={scope}&limit=1") as response:
                self.assertEqual(len(json.load(response)["quotes"]),1)
            for query in ("?scope=other",f"?scope={scope}&limit=1000"):
                with self.assertRaises(HTTPError) as caught: urlopen(base+query)
                self.assertEqual(caught.exception.code,400)
            with self.assertRaises(HTTPError) as caught: urlopen(base,data=b"{}")
            self.assertEqual(caught.exception.code,404)
        finally:
            server.shutdown(); server.server_close(); thread.join()


class ReferenceWorkerTests(unittest.TestCase):
    def test_worker_reconnects_after_sequence_gap_and_only_sends_market_data_commands(self):
        try:
            import websockets.asyncio.client
        except ImportError:
            self.skipTest("Optional reference worker dependency unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); history=root/"history.sqlite"
            with Store(history) as h:
                scope=h.bind_account("local","kalshi","demo","test","fingerprint")
                h.db.execute("INSERT INTO telemetry_health VALUES (?,?,?,?)",(scope,"session",utc_now(),encode({"instrument_id":"TEST","heartbeat_at":utc_now(),"stopped":False}))); h.db.commit()
            args=SimpleNamespace(env_file=root/"unused",account="test",environment="demo",history_db=history,db=root/"quotes.sqlite",stop_file=root/"stop")
            sent=[]; sessions=[]
            class Client:
                fingerprint="fingerprint"
                def verify_read_only(self): pass
                def request(self,endpoint):
                    self_endpoint=endpoint
                    assert self_endpoint=="/markets/TEST"
                    return {"market":{"ticker":"TEST","market_type":"binary","status":"active"}}
            class Connection:
                def __init__(self,uri,**kwargs):
                    assert uri=="wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
                    assert kwargs["proxy"] is None
                    exc=ValueError("redirect")
                    assert self.process_redirect(exc) is exc
                    self.number=len(sessions); sessions.append(self)
                    self.messages=[{"type":"subscribed","msg":{"channel":"orderbook_delta","sid":7}},snapshot(),delta(3 if self.number==0 else 2)]
                async def __aenter__(self): return self
                async def __aexit__(self,*_): pass
                async def send(self,payload): sent.append(json.loads(payload))
                async def recv(self):
                    await asyncio.sleep(0)
                    result=self.messages.pop(0)
                    if self.number==1 and not self.messages: args.stop_file.touch()
                    return json.dumps(result)
            with patch("othryss.reference_cli.credentials",return_value=("fake","fake")), patch("othryss.reference_cli.KalshiClient",return_value=Client()), patch("othryss.reference_cli.auth_headers",return_value={}), patch("websockets.asyncio.client.connect",Connection):
                asyncio.run(run(args))
            data=read(args.db,scope)
            self.assertEqual(len(sessions),2)
            self.assertTrue(all(c["cmd"]=="subscribe" and c["params"]["channels"]==["orderbook_delta"] for c in sent))
            self.assertEqual(len({q["connection_id"] for q in data["quotes"]}),2)
            self.assertEqual(len(data["quotes"]),3)
            self.assertEqual(data["worker"]["status"],"stopped")
            gaps=[g for g in data["gaps"] if g["reason"]=="sequence_or_message_invalid"]
            self.assertEqual(len(gaps),1); self.assertIsNotNone(gaps[0]["ended_at"])
            self.assertFalse(data["markets"][0]["eligible"])


if __name__=="__main__": unittest.main()
