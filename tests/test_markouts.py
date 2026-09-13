"""Synthetic markouts: direction, weighting, clocks, coverage and API isolation."""
from contextlib import closing
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError

from othryss.markouts import evaluate, catalog, iso
from othryss.reference import ReferenceStore
from othryss.history_reader import HistoryReader
from othryss.storage import Store, encode
from othryss.server import Handler

AT=datetime(2026,9,10,12,tzinfo=timezone.utc)


def fill(scope="scope", key="fill", direction="increase_yes", quantity="2.5", at=AT):
    return {"event_id":key,"scope_id":scope,"type":"ORDER_FILL","instrument_id":"TEST","occurred_at":iso(at),
            "payload":{"fill_id":key,"order_id":"order-"+key,"subaccount":0,"price_usd":"0.40","quantity":quantity,"fee_usd":"0.02",
            "price_basis":"yes_outcome","currency":"USD","quantity_unit":"contracts","exposure_direction":direction}}


def quotes(scope="scope", at=AT):
    return [{"quote_id":i+2,"scope_id":scope,"instrument_id":"TEST","received_at":iso(at+timedelta(seconds=i)),
        "source_at":None,"received_monotonic_ns":str((i+100)*1_000_000_000),"connection_id":"connection","sid":1,"sequence":i+2,
        "schema_version":"reference-1","kind":"orderbook_snapshot","price_basis":"yes_outcome","currency":"USD","quantity_unit":"contracts",
        "bid":"0.40","ask":"0.60","midpoint":"0.50","bid_size":"2.5","ask_size":"3.5","quality":"valid","clock_basis":"local_receipt_only"} for i in range(-1,32)]


class MarkoutMathTests(unittest.TestCase):
    def run_case(self, f=None, q=None, gaps=None, now=None):
        return evaluate(f or fill(),quotes() if q is None else q,gaps or [],now or AT+timedelta(seconds=100))

    def test_all_horizons_signed_decimal_values_and_fee_exclusion(self):
        result=self.run_case()
        self.assertEqual([r["horizon_seconds"] for r in result],[1,5,30])
        for r in result:
            self.assertEqual(r["status"],"estimate")
            self.assertEqual(r["per_contract_usd"],"0.10")
            self.assertEqual(r["quantity_weighted_usd"],"0.250")
            self.assertEqual(r["quote_age_seconds"],"0")
        result=self.run_case(fill(direction="decrease_yes"))
        self.assertEqual(result[0]["per_contract_usd"],"-0.10")
        self.assertEqual(result[0]["quantity_weighted_usd"],"-0.250")

    def test_valid_zero_is_distinct_from_missing(self):
        f=fill();f["payload"]["price_usd"]="0.50"
        self.assertEqual(self.run_case(f)[0]["per_contract_usd"],"0")
        self.assertIsNone(self.run_case(q=[])[0]["per_contract_usd"])

    def test_pending_horizon_confirmation_and_deadline(self):
        self.assertEqual(self.run_case(now=AT)[0]["reason"],"horizon_not_elapsed")
        q=[r for r in quotes() if r["sequence"]<=3]
        self.assertEqual(self.run_case(q=q,now=AT+timedelta(seconds=2))[0]["reason"],"awaiting_confirmation")
        self.assertEqual(self.run_case(q=q)[0]["reason"],"confirmation_missing")

    def test_no_future_price_selection_and_no_invalid_quote_fallback(self):
        q=quotes();q[3]["midpoint"]="0.55";q[3]["ask"]="0.70"
        self.assertEqual(self.run_case(q=q)[0]["per_contract_usd"],"0.10")
        q=quotes();q[2]["quality"]="one_sided";q[2]["midpoint"]=None
        self.assertEqual(self.run_case(q=q)[0]["reason"],"invalid_reference")

    def test_age_boundary_old_capture_and_no_history_reconstruction(self):
        q=[r for r in quotes() if r["received_at"]!=iso(AT+timedelta(seconds=1))]
        self.assertEqual(self.run_case(q=q)[0]["status"],"estimate")
        q=[r for r in q if r["received_at"]!=iso(AT)]
        self.assertEqual(self.run_case(q=q)[0]["reason"],"quote_too_old")
        q=[r for r in quotes() if r["received_at"]>iso(AT)]
        self.assertEqual(self.run_case(q=q)[0]["reason"],"capture_missing_at_fill")

    def test_open_closed_and_startup_gap_boundaries(self):
        gap={"started_at":iso(AT+timedelta(seconds=.5)),"ended_at":None,"reason":"disconnect"}
        self.assertEqual(self.run_case(gaps=[gap])[0]["reason"],"capture_gap")
        gap["ended_at"]=iso(AT+timedelta(seconds=.8))
        self.assertEqual(self.run_case(gaps=[gap])[0]["reason"],"capture_gap")
        gap.update(started_at=iso(AT-timedelta(seconds=2)),ended_at=iso(AT))
        self.assertEqual(self.run_case(gaps=[gap])[0]["status"],"estimate")

    def test_invalid_intermediate_book_does_not_disappear_at_longer_horizon(self):
        q=quotes();q[4]["quality"]="crossed"
        result=self.run_case(q=q)
        self.assertEqual(result[0]["status"],"estimate")
        self.assertEqual(result[1]["reason"],"invalid_reference")
        self.assertEqual(result[2]["reason"],"invalid_reference")

    def test_connection_clock_scope_and_sequence_guards(self):
        for field,value,reason in [("connection_id","other","connection_changed"),("sid",2,"connection_changed"),
                ("received_monotonic_ns","100","clock_discontinuity"),("sequence",1,"sequence_reversal"),
                ("scope_id","other","reference_scope_mismatch"),("instrument_id","other","reference_scope_mismatch"),
                ("price_basis","no_outcome","invalid_reference"),("midpoint","NaN","invalid_reference")]:
            with self.subTest(field=field):
                q=quotes();q[2][field]=value
                self.assertEqual(self.run_case(q=q)[0]["reason"],reason)

    def test_supplied_source_time_must_also_be_near_target(self):
        for seconds in (-1,1.1):
            q=quotes();q[2]["source_at"]=iso(AT+timedelta(seconds=seconds))
            self.assertEqual(self.run_case(q=q)[0]["reason"],"timing_uncertain")
        q=quotes();q[2]["source_at"]=iso(AT+timedelta(seconds=.9))
        self.assertEqual(self.run_case(q=q)[0]["status"],"estimate")

    def test_sparse_interval_and_invalid_fill_fail_closed(self):
        q=[r for r in quotes() if r["received_at"] in {iso(AT),iso(AT+timedelta(seconds=30)),iso(AT+timedelta(seconds=31))}]
        self.assertEqual(self.run_case(q=q)[2]["reason"],"observation_gap")
        f=fill();f["payload"]["exposure_direction"]="unknown"
        self.assertEqual(self.run_case(f)[0]["reason"],"invalid_fill")


def seed(history_path, reference_path):
    with Store(history_path) as h:
        scope=h.bind_account("local","kalshi","demo","markout-test","key")
        other=h.bind_account("local","kalshi","demo","zzz-other","other-key")
        for f in [fill(scope,"buy",quantity="2"),fill(scope,"sell","decrease_yes",quantity="1"),fill(scope,"old",at=AT-timedelta(days=1)),fill(other,"other")]:
            h.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",(f["event_id"],f["scope_id"],f["type"],f["instrument_id"],f["payload"]["order_id"],f["occurred_at"],f["occurred_at"],encode(f)))
        h.db.commit()
    r=ReferenceStore(reference_path)
    try:
        r.start(scope,"w");r.watch(scope,{"TEST":{}})
        for q in quotes(scope): r.append(scope,"connection",q)
    finally: r.db.close()
    return scope,other


class MarkoutQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name);self.history=root/"history.sqlite";self.refs=root/"quotes.sqlite"
        self.scope,self.other=seed(self.history,self.refs)

    def get(self,**kwargs):
        with HistoryReader(self.history) as reader: return catalog(reader,self.refs,self.scope,now=AT+timedelta(seconds=100),**kwargs)

    def test_weighting_and_coverage_denominators_exclude_unavailable(self):
        data=self.get();summary=data["summary"][0]
        self.assertEqual(data["total"],3)
        self.assertEqual(summary["estimated_fills"],2)
        self.assertEqual(summary["unavailable_fills"],1)
        self.assertEqual(summary["eligible_quantity"],"3")
        self.assertEqual(summary["quantity_weighted_mean_usd"],"0.033333")
        self.assertEqual(summary["quantity_weighted_sum_usd"],"0.10")

    def test_filter_pagination_scope_and_query_bounds(self):
        self.assertEqual(self.get(order="order-buy")["total"],1)
        self.assertEqual(self.get(instrument="MISSING")["rows"],[])
        self.assertNotEqual(self.get(limit=1)["rows"][0]["fill"]["event_id"],self.get(limit=1,offset=1)["rows"][0]["fill"]["event_id"])
        with self.assertRaises(ValueError): self.get(limit=51)
        with HistoryReader(self.history) as reader:
            other=catalog(reader,self.refs,self.other)
            self.assertEqual(other["rows"][0]["markouts"][0]["reason"],"capture_missing_at_fill")
            with self.assertRaises(ValueError): catalog(reader,self.refs,"unknown")

    def test_missing_reference_store_is_unavailable_and_not_created(self):
        missing=self.refs.parent/"missing.sqlite"
        with HistoryReader(self.history) as reader: data=catalog(reader,missing,self.scope)
        self.assertFalse(missing.exists())
        self.assertTrue(all(m["per_contract_usd"] is None for r in data["rows"] for m in r["markouts"]))

    def test_evidence_limit_is_explicit_and_foreign_gaps_do_not_leak(self):
        with closing(sqlite3.connect(self.refs)) as db, db:
            db.execute("INSERT INTO gaps(scope_id,instrument_id,started_at,reason) VALUES (?,?,?,?)",(self.other,"TEST",iso(AT),"other_scope"))
        self.assertEqual(self.get(order="order-buy")["rows"][0]["markouts"][0]["status"],"estimate")
        with closing(sqlite3.connect(self.refs)) as db, db:
            records=[]
            for i in range(2001):
                q=quotes(self.scope)[0]|{"received_at":iso(AT+timedelta(milliseconds=i)),"sequence":i+100,"connection_id":"burst"}
                records.append((self.scope,"burst","TEST",1,i+100,q["received_at"],"valid",encode(q)))
            db.executemany("INSERT INTO quotes(scope_id,run_id,instrument_id,sid,sequence,received_at,quality,record_json) VALUES (?,?,?,?,?,?,?,?)",records)
        results=self.get(order="order-buy")["rows"][0]["markouts"]
        self.assertNotEqual(results[0]["reason"],"evidence_limit")
        self.assertEqual(results[2]["reason"],"evidence_limit")

    def test_http_read_only_endpoint_and_export_evidence(self):
        handler=type("TestHandler",(Handler,),{"history_db":self.history,"reference_db":self.refs,"log_message":lambda *_:None})
        server=ThreadingHTTPServer(("127.0.0.1",0),handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f"http://127.0.0.1:{server.server_port}/api/history/markouts"
            with urlopen(base+"?scope="+self.scope) as response: data=json.load(response)
            self.assertEqual(data["policy"]["version"],"receipt-markout-1")
            self.assertIn("evidence",data["rows"][0]["markouts"][0])
            for query in ("?scope=unknown","?scope="+self.scope+"&limit=100"):
                with self.assertRaises(HTTPError) as error: urlopen(base+query)
                self.assertEqual(error.exception.code,400)
            with self.assertRaises(HTTPError) as error: urlopen(base,data=b"{}")
            self.assertEqual(error.exception.code,404)
        finally: server.shutdown();server.server_close();thread.join()

    def test_busy_capture_is_bounded_per_horizon_without_sampling_away_bad_quotes(self):
        with closing(sqlite3.connect(self.refs)) as db, db:
            db.execute("DELETE FROM quotes WHERE scope_id=? AND instrument_id='TEST'", (self.scope,))
            records=[]
            for i in range(-2000, 5001):
                q=quotes(self.scope)[0]|{"received_at":iso(AT+timedelta(milliseconds=i*10)),
                    "received_monotonic_ns":str((i+3000)*10000000),"sequence":i+3001}
                records.append((self.scope,"busy","TEST",1,q["sequence"],q["received_at"],"valid",encode(q)))
            db.executemany("INSERT INTO quotes(scope_id,run_id,instrument_id,sid,sequence,received_at,quality,record_json) VALUES (?,?,?,?,?,?,?,?)",records)
        result=self.get(order="order-buy")["rows"][0]["markouts"]
        self.assertEqual([r["status"] for r in result],["estimate","estimate","unavailable"])
        self.assertEqual(result[2]["reason"],"evidence_limit")
        self.assertEqual([r["evidence"]["interval_records"] for r in result[:2]],[102,502])
        # A single invalid intermediate observation must still invalidate the horizon.
        with closing(sqlite3.connect(self.refs)) as db, db:
            db.execute("UPDATE quotes SET record_json=json_set(record_json,'$.quality','invalid') WHERE scope_id=? AND received_at=?",(self.scope,iso(AT+timedelta(milliseconds=500))))
        self.assertEqual(self.get(order="order-buy")["rows"][0]["markouts"][0]["reason"],"invalid_reference")


if __name__=="__main__":unittest.main()
