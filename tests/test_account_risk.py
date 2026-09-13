"""Account/risk correctness with isolated evidence and no external messages."""
import copy
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch

from othryss import account_risk as risk, alerts
from othryss.storage import Store
from othryss.history_reader import HistoryReader
from othryss.incidents import act,detail
from test_execution import fill,insert_fill
from test_alerts import route,DISCORD


class Client:
    def __init__(self,positions=None,balance=None):
        self.positions=positions if positions is not None else [{"ticker":"LONG","position_fp":"10.25"},{"ticker":"SHORT","position_fp":"-7.5"}]
        self.balance=balance or {"balance":12345,"portfolio_value":6789,"updated_ts":int(datetime.now(timezone.utc).timestamp())}
        self.calls=[];self.subaccount=None;self.scopes=["read"]
    def verify_read_only(self):return {"subaccount":self.subaccount,"scopes":self.scopes}
    def request(self,endpoint,params=None):
        self.calls.append((endpoint,params))
        if endpoint=="/portfolio/balance":
            if isinstance(self.balance,Exception):raise self.balance
            return self.balance
        if endpoint=="/portfolio/positions":return {"market_positions":self.positions,"cursor":""}
        raise AssertionError(endpoint)


def snapshot(scope,now,positions=None,identity="snapshot",complete=True):
    rows=positions if positions is not None else [("LONG","10.25"),("SHORT","-7.5")]
    return {"snapshot_id":identity,"scope_id":scope,"subaccount":0,"started_at":now.isoformat(),"received_at":now.isoformat(),
            "evidence":{"positions_complete":complete,"positions":[{"instrument_id":t,"quantity":q,"absolute_quantity":risk.fixed(abs(risk.number(q)))} for t,q in rows],
                        "balance":None,"balance_error":None,"positions_error":None}}


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/"history.sqlite"
        self.store=Store(self.path);self.scope=self.store.bind_account("local","kalshi","demo","test","key")
        self.other=self.store.bind_account("local","kalshi","demo","other","key")
        self.now=datetime.now(timezone.utc)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def read(self,**kwargs):
        with HistoryReader(self.path) as reader:return risk.catalog(reader,self.scope,**kwargs)
    def configure(self,**kwargs):
        settings=risk.settings(self.store.db,self.scope)
        return risk.save_settings(self.path,self.scope,settings['revision'],risk.DEFAULT|{"enabled":True,"per_market_limit":"10"}|kwargs)

    def test_exact_cents_and_non_netting_absolute_inventory(self):
        captured=risk.collect(self.store,Client(),self.scope)
        data=self.read()
        self.assertEqual(data['balance']['available_balance_usd'],'123.45')
        self.assertEqual(data['balance']['portfolio_value_usd'],'67.89')
        self.assertEqual(data['absolute_contracts'],'17.75')
        self.assertEqual(data['position_count'],2)
        self.assertTrue(captured['evidence']['positions_complete'])
        self.assertFalse(data['settings']['config']['enabled'])

    def test_empty_complete_snapshot_is_distinct_from_failed_capture(self):
        risk.collect(self.store,Client(positions=[]),self.scope)
        self.assertEqual(self.read()['absolute_contracts'],'0')
        client=Client(positions=[{'ticker':'BROKEN'}])
        risk.collect(self.store,client,self.scope)
        data=self.read();self.assertIsNone(data['absolute_contracts']);self.assertTrue(data['balance_fresh'])
        self.assertEqual(data['errors']['positions_error'],'position_capture_incomplete')

    def test_balance_failure_does_not_remove_position_evidence(self):
        risk.collect(self.store,Client(balance=OSError('PRIVATE')),self.scope)
        data=self.read();self.assertTrue(data['positions_fresh']);self.assertFalse(data['balance_fresh'])
        self.assertNotIn('PRIVATE',json.dumps(data))

    def test_wrong_read_key_scope_prevents_portfolio_reads(self):
        client=Client();client.subaccount=1
        captured=risk.collect(self.store,client,self.scope)
        self.assertEqual(client.calls,[])
        self.assertFalse(captured['evidence']['positions_complete'])

    def test_positions_fail_closed_for_duplicate_wrong_scope_and_bad_units(self):
        for rows in [[{'ticker':'X','position_fp':'1'},{'ticker':'X','position_fp':'2'}],[{'ticker':'X','position_fp':'1','subaccount_number':1}],
                     [{'ticker':'X','position_fp':'NaN'}],[{'ticker':'X','position_fp':'1','exchange_index':1}]]:
            with self.assertRaises(ValueError):risk.position_rows(Client(rows))
        class Loop(Client):
            def request(self,*_):return {'market_positions':[],'cursor':'repeat'}
        with self.assertRaises(ValueError):risk.position_rows(Loop())

    def test_stale_capture_keeps_evidence_but_not_current_totals(self):
        risk.collect(self.store,Client(),self.scope)
        data=self.read(now=self.now+timedelta(minutes=10))
        self.assertIsNone(data['absolute_contracts']);self.assertFalse(data['balance_fresh'])
        self.assertEqual(len(data['positions']),2)

    def test_configuration_validation_revision_and_zero_limits(self):
        result=self.configure(per_market_limit='0',total_limit='20.50',overrides=[{'ticker':'LONG','limit':'12'}])
        self.assertEqual(result['config']['total_limit'],'20.5')
        self.assertEqual(result['revision'],1)
        with self.assertRaises(ValueError):risk.save_settings(self.path,self.scope,0,result['config'])
        for values in [{'total_limit':'NaN'},{'total_limit':'-1'},{'enabled':'true'},{'grace_seconds':1},{'overrides':[{'ticker':'X','limit':'1'},{'ticker':'X','limit':'2'}]}]:
            with self.assertRaises(ValueError):risk.validate_config(risk.DEFAULT|values)
        with self.assertRaises(ValueError):risk.save_settings(self.path,self.other,1,result['config'])

    def test_limits_strict_greater_override_and_total(self):
        config=risk.validate_config(risk.DEFAULT|{'enabled':True,'per_market_limit':'10.25','total_limit':'17.75'})
        self.assertEqual(risk.assess(snapshot(self.scope,self.now),config,self.now)['findings'],[])
        config['overrides']=[{'ticker':'SHORT','limit':'7'}];config['total_limit']='17'
        findings=risk.assess(snapshot(self.scope,self.now),config,self.now)['findings']
        self.assertEqual({f['entity'] for f in findings},{'SHORT','primary:total'})

    def test_persistence_dedup_recovery_and_immutable_evidence(self):
        self.configure(grace_seconds=30)
        now=datetime.now(timezone.utc)+timedelta(seconds=1)
        first=snapshot(self.scope,now)
        risk.evaluate(self.store,self.scope,first,now);risk.evaluate(self.store,self.scope,first,now+timedelta(seconds=31))
        row=self.store.db.execute("SELECT * FROM incidents").fetchone();self.assertEqual(row['status'],'pending')
        now+=timedelta(seconds=31)
        risk.evaluate(self.store,self.scope,snapshot(self.scope,now,identity='second'),now)
        row=self.store.db.execute("SELECT * FROM incidents").fetchone();self.assertEqual(row['status'],'open')
        self.assertEqual(detail(self.store.db,self.scope,row['incident_id'])['first_evidence']['result']['config']['per_market_limit'],'10')
        for i in range(2):
            now+=timedelta(seconds=31)
            risk.evaluate(self.store,self.scope,snapshot(self.scope,now,positions=[],identity=f'clear-{i}'),now)
        self.assertEqual(self.store.db.execute("SELECT status FROM incidents").fetchone()[0],'resolved')

    def test_unknown_and_config_change_cannot_resolve_breach(self):
        self.configure(grace_seconds=30)
        now=datetime.now(timezone.utc)+timedelta(seconds=1)
        for i in range(2):
            risk.evaluate(self.store,self.scope,snapshot(self.scope,now,identity=str(i)),now);now+=timedelta(seconds=31)
        risk.evaluate(self.store,self.scope,snapshot(self.scope,now,identity='missing',complete=False),now)
        row=self.store.db.execute('SELECT * FROM incidents').fetchone()
        self.assertEqual((row['status'],row['assessment']),('open','unknown'))
        self.configure(enabled=False)
        self.assertEqual(self.store.db.execute('SELECT assessment FROM incidents').fetchone()[0],'unknown')
        with self.assertRaises(ValueError):act(self.path,self.scope,row['incident_id'],'resolve')

    def test_snapshot_from_before_configuration_is_not_reused(self):
        # Windows may return the same clock tick for setup and configuration.
        old=snapshot(self.scope,self.now-timedelta(seconds=1))
        self.configure();risk.evaluate(self.store,self.scope,old,self.now)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM bot_checks').fetchone()[0],0)

    def test_fill_window_scope_fees_and_unknown_exclusions(self):
        def add(identity,hours,sub,qty,fee):
            event=fill(event_id=identity,subaccount=sub)
            event['occurred_at']=(self.now-timedelta(hours=hours)).isoformat()
            event['payload'].update(quantity=qty,fee_usd=fee)
            insert_fill(self.store,self.scope,event)
        add('a',1,0,'2.25','0.013');add('b',2,0,'1','-0.001');add('old',48,0,'3','0.02');add('unknown',1,None,'100','1');add('other',1,1,'100','1')
        self.store.db.commit()
        result=self.read(now=self.now)['fills']
        self.assertEqual((result['fill_count'],result['volume_contracts'],result['fees_usd']),(2,'3.25','0.012'))
        self.assertEqual((result['unknown_subaccount_excluded'],result['other_subaccounts_excluded']),(1,1))
        self.assertEqual(self.read(window='all',now=self.now)['fills']['fill_count'],3)
        with patch.object(risk,'MAX_FILLS',1):self.assertTrue(self.read(window='all',now=self.now)['fills']['truncated'])

    def test_inventory_incident_routes_through_shared_alert_queue(self):
        self.configure(grace_seconds=30)
        queue=alerts.connect(Path(self.temp.name)/'alerts.sqlite');r=route(self.scope)
        now=datetime.now(timezone.utc)+timedelta(seconds=1)
        try:
            alerts.sync_routes(queue,self.store.db,[r],{'discord':DISCORD},now.timestamp())
            for i in range(2):
                risk.evaluate(self.store,self.scope,snapshot(self.scope,now,identity=str(i)),now);now+=timedelta(seconds=31)
            alerts.discover(queue,self.store.db,r,now.timestamp())
            delivery=queue.execute('SELECT payload_json FROM deliveries').fetchone()
            self.assertIn('10.25 absolute contracts exceeds limit 10',delivery[0])
            sent=[]
            alerts.deliver_one(queue,self.store.db,r,DISCORD,now.timestamp(),lambda *args:sent.append(args) or {'status':'accepted'})
            self.assertEqual(len(sent),1)
        finally:queue.close()


if __name__=='__main__':unittest.main()
