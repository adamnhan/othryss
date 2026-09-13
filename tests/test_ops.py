"""Operational failures use isolated databases and disposable child processes."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import sqlite3
from unittest.mock import patch
from contextlib import closing
from pathlib import Path

from integrations.lip.othryss_telemetry import Publisher
from othryss.maintenance import BackupTimeout, backup, restore, verify, reclaim, prune_backups, checksum, sqlite_copy
from othryss.ops import Job, config, commands, backup_sources_ready, backup_attempt, backup_interval
from othryss.storage import Store, import_lock
from othryss.telemetry import import_directory
from test_markouts import seed


def until(predicate,seconds=10):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            if predicate(): return
        except (PermissionError,FileNotFoundError,json.JSONDecodeError):
            # Windows can briefly deny a read while the supervisor replaces status.
            pass
        time.sleep(.05)
    raise AssertionError("Timed out waiting for isolated test process")


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.spool=self.root/"spool"
        self.store=Store(self.root/"history.sqlite");self.scope=self.store.bind_account("local","kalshi","demo","test","key")
        self.publisher=Publisher(self.spool,account="test",environment="demo",ticker="TEST",run_id="test",segment_bytes=1200,max_bytes=3600)
    def tearDown(self):
        self.publisher.close();self.publisher.thread.join(timeout=10);self.store.close();self.temp.cleanup()
    def test_rotation_preserves_session_sequence_and_recovers_after_import_ack(self):
        for _ in range(50): self.publisher.emit("PRODUCER_START",{})
        until(lambda:self.publisher.capped and self.publisher.queue.empty())
        self.assertLessEqual(sum(p.stat().st_size for p in self.spool.glob("*.jsonl")),3600)
        result=import_directory(self.store,self.scope,self.spool)
        self.assertEqual(result["errors"],0)
        self.assertGreater(len(list(self.spool.glob("*.closed"))),1)
        old_sequence=self.publisher.seq
        removed=reclaim(self.root/"history.sqlite",self.spool)
        self.assertGreater(removed["segments"],1)
        self.publisher.emit("PRODUCER_START",{})
        until(lambda:not self.publisher.capped)
        self.publisher.close();self.publisher.thread.join(5)
        result=import_directory(self.store,self.scope,self.spool)
        self.assertEqual(result["errors"],0)
        maximum=self.store.db.execute("SELECT MAX(sequence) FROM telemetry_records").fetchone()[0]
        self.assertGreater(maximum,old_sequence)
        self.assertEqual(self.store.db.execute("SELECT COUNT(DISTINCT session_id) FROM telemetry_records").fetchone()[0],1)
        self.assertEqual(import_directory(self.store,self.scope,self.spool)["inserted"],0)
    def test_unacknowledged_and_changed_segments_are_never_reclaimed(self):
        self.publisher.close();self.publisher.thread.join(5)
        self.assertEqual(reclaim(self.root/"history.sqlite",self.spool)["segments"],0)
        import_directory(self.store,self.scope,self.spool)
        path=next(self.spool.glob("*.jsonl"))
        with path.open("ab") as out: out.write(b"changed")
        self.assertEqual(reclaim(self.root/"history.sqlite",self.spool)["segments"],0)
        self.assertTrue(path.exists())


class BackupTests(unittest.TestCase):
    def test_first_backup_waits_for_all_committed_schemas_without_creating_files(self):
        fresh=self.root/"fresh"
        self.assertFalse(backup_sources_ready(fresh))
        self.assertFalse(fresh.exists())
        for relative,version in (("history/othryss.sqlite",7),("reference/quotes.sqlite",1),("alerts/delivery.sqlite",1)):
            path=fresh/"artifacts"/relative;path.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(path)) as db:
                self.assertFalse(backup_sources_ready(fresh))
                db.execute(f"PRAGMA user_version={version}")
        self.assertTrue(backup_sources_ready(fresh))
        with closing(sqlite3.connect(path)) as db: db.execute("PRAGMA user_version=999")
        self.assertFalse(backup_sources_ready(fresh))

    def test_backup_pins_one_snapshot_while_another_connection_keeps_writing(self):
        source=self.root/"busy.sqlite";target=self.root/"snapshot.sqlite"
        with closing(sqlite3.connect(source)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE records(id INTEGER PRIMARY KEY,payload BLOB)")
            writer.execute("CREATE TABLE counter(n INTEGER)");writer.execute("INSERT INTO counter VALUES (0)")
            writer.executemany("INSERT INTO records(payload) VALUES (?)", [(b"a"*4096,)]*2000);writer.commit()
            remaining=[]
            def mutate(status,left,total):
                remaining.append(left)
                writer.execute("UPDATE counter SET n=n+1");writer.commit()
            sqlite_copy(source,target,timeout_seconds=10,progress_hook=mutate)
            self.assertGreater(len(remaining),2)
            self.assertEqual(remaining,sorted(remaining,reverse=True))
            self.assertGreater(writer.execute("SELECT n FROM counter").fetchone()[0],0)
        with closing(sqlite3.connect(target)) as copy:
            self.assertEqual(copy.execute("SELECT n FROM counter").fetchone()[0],0)
            self.assertEqual(copy.execute("SELECT COUNT(*) FROM records").fetchone()[0],2000)
            self.assertEqual(copy.execute("PRAGMA integrity_check").fetchone()[0],"ok")

    def test_backup_deadline_remains_bounded_and_closes_connections(self):
        target=self.root/"deadline.sqlite"
        with self.assertRaises(TimeoutError):sqlite_copy(self.history,target,timeout_seconds=-1)
        # Connection cleanup permits the isolated test artifact to be removed on Windows.
        target.unlink()

    def test_integrity_deadline_is_reported_as_timeout_and_never_completed(self):
        with closing(sqlite3.connect(self.history)) as db, db:
            db.execute("CREATE TABLE deadline_records(value INTEGER)")
            db.executemany("INSERT INTO deadline_records VALUES (?)",((n,) for n in range(10000)))
        real_copy=sqlite_copy
        calls=[]
        def copy_with_expiring_clock(source,target,**kwargs):
            copied=False;checks=0
            def progress(status,left,total):
                nonlocal copied
                if left==0: copied=True
            def clock():
                nonlocal checks
                if copied: checks+=1
                # Enter integrity_check, then expire at its SQLite progress callback.
                return 121 if checks>1 else 0
            with patch("othryss.maintenance.time.monotonic",side_effect=clock):
                try: real_copy(source,target,progress_hook=progress)
                except BackupTimeout as exc:
                    calls.append(exc)
                    raise
        with patch("othryss.maintenance.sqlite_copy",side_effect=copy_with_expiring_clock):
            with self.assertRaises(BackupTimeout): backup(self.root)
        self.assertEqual(calls[0].stage,"integrity_check")
        self.assertEqual(calls[0].database,"history.sqlite")
        self.assertIsInstance(calls[0].__cause__,sqlite3.OperationalError)
        self.assertEqual(calls[0].__cause__.sqlite_errorcode,sqlite3.SQLITE_INTERRUPT)
        bundle=next(p for p in (self.root/"artifacts/backups").iterdir() if p.is_dir())
        self.assertFalse((bundle/"COMPLETE").exists())
        (bundle/"history.sqlite").unlink()  # No leaked connection after interrupt.

    def test_failure_retains_last_success_and_recovery_survives_retention_error(self):
        previous={"status":"complete","path":"previous-bundle","finished_at":"2026-09-13T09:47:03+00:00"}
        with patch("othryss.ops.backup",side_effect=BackupTimeout("history.sqlite","integrity_check")):
            failed=backup_attempt(self.root,7,previous)
        self.assertEqual(failed["last_success"]["path"],"previous-bundle")
        self.assertEqual(failed["stage"],"integrity_check")
        self.assertEqual(backup_interval(failed,21600),300)
        with patch("othryss.ops.prune_backups",side_effect=OSError("SECRET-CANARY")):
            recovered=backup_attempt(self.root,7,failed)
        self.assertEqual(recovered["status"],"complete")
        self.assertEqual(recovered["last_success"]["path"],recovered["path"])
        self.assertEqual(recovered["retention_error"],{"error":"OSError"})
        self.assertNotIn("SECRET-CANARY",json.dumps(recovered))
        self.assertEqual(backup_interval(recovered,21600),21600)
        verify(recovered["path"])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.addCleanup(self.temp.cleanup)
        self.history=self.root/"artifacts/history/othryss.sqlite";self.refs=self.root/"artifacts/reference/quotes.sqlite"
        self.scope,_=seed(self.history,self.refs)
        self.spool=self.root/"artifacts/telemetry/lip";self.spool.mkdir(parents=True)
        (self.root/"local.env").write_text("SECRET-CANARY")
    def test_backup_restore_integrity_spool_replay_and_secret_exclusion(self):
        (self.root/"ops.local.json").write_text(json.dumps({"lip_supervisor_log":str(self.root/"supervisor.log"),"unexpected_secret":"SECRET-CANARY"}))
        p=Publisher(self.spool,account="markout-test",environment="demo",ticker="TEST",run_id="test")
        p.close();p.thread.join(5)
        with Store(self.history) as db: import_directory(db,self.scope,self.spool)
        file=next(self.spool.glob("*.jsonl"))
        with file.open("ab") as out: out.write(b'{"partial":')
        bundle=backup(self.root);manifest=verify(bundle)
        self.assertNotIn("local.env",manifest["files"])
        self.assertNotIn("SECRET-CANARY",(bundle/"manifest.json").read_text())
        saved=json.loads((bundle/"ops.local.json").read_text())
        self.assertEqual(saved,{"lip_supervisor_log":str(self.root/"supervisor.log")})
        target=restore(bundle,self.root/"restored")
        self.assertTrue((target/"spool"/file.name).read_bytes().endswith(b"\n"))
        with Store(target/"history.sqlite") as db:
            self.assertEqual(db.db.execute("SELECT COUNT(*) FROM telemetry_files").fetchone()[0],0)
            result=import_directory(db,self.scope,target/"spool")
            self.assertEqual(result["errors"],0);self.assertEqual(result["inserted"],0)
            self.assertEqual(db.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],4)
        with self.assertRaises(ValueError): restore(bundle,target)
    def test_alert_queue_backup_restores_disarmed(self):
        from othryss import alerts
        from test_alerts import route,seed_incident
        db=alerts.connect(self.root/"artifacts/alerts/delivery.sqlite")
        try:
            with Store(self.history) as history:
                incident=seed_incident(history,self.scope,time.time(),"backup-incident")
            r=route(self.scope)
            with db:
                db.execute("INSERT INTO routes VALUES (?,?,?,?,?,?,?,?,?)",(r["id"],self.scope,"discord","fingerprint",0,1,None,None,time.time()))
                alerts.enqueue(db,r,"test",incident,"test",time.time())
            bundle=backup(self.root)
            self.assertIn("alerts.sqlite",verify(bundle)["files"])
            restored=restore(bundle,self.root/"alerts-restore")
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(restored/"alerts.sqlite")) as saved:
                self.assertEqual(saved.execute("SELECT status FROM deliveries").fetchone()[0],"canceled")
                self.assertEqual(saved.execute("SELECT active FROM routes").fetchone()[0],0)
        finally: db.close()
    def test_tampered_backup_and_path_traversal_are_rejected_before_restore(self):
        bundle=backup(self.root)
        with (bundle/"references.sqlite").open("ab") as out: out.write(b"bad")
        with self.assertRaises(ValueError): restore(bundle,self.root/"tampered")
        self.assertFalse((self.root/"tampered").exists())
        bundle=backup(self.root)
        manifest=json.loads((bundle/"manifest.json").read_text());manifest["files"]["../escape"]="bad"
        (bundle/"manifest.json").write_text(json.dumps(manifest));(bundle/"COMPLETE").write_text(checksum(bundle/"manifest.json"))
        with self.assertRaises(ValueError): verify(bundle)
    def test_retention_preserves_newest_verified_bundles(self):
        one=backup(self.root,self.root/"artifacts/backups/001")
        two=backup(self.root,self.root/"artifacts/backups/002")
        prune_backups(self.root,keep=1)
        self.assertFalse(one.exists());self.assertTrue(two.exists());verify(two)


class SupervisorTests(unittest.TestCase):
    def test_failed_automatic_backup_retries_without_restarting_supervisor(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);directory=root/"artifacts/ops";directory.mkdir(parents=True)
            previous={"status":"complete","path":"previous-bundle","finished_at":"2020-01-01T00:00:00+00:00"}
            statefile=directory/"status.json"
            statefile.write_text(json.dumps({"backup":previous}))
            script="""
from pathlib import Path
import sys,sqlite3
from othryss import ops
root=Path(sys.argv[1])
clock=ops.time.monotonic
ops.time.monotonic=lambda:clock()*100
ops.commands=lambda *_:{}
ops.backup_sources_ready=lambda *_:True
ops.reclaim=lambda *_:{}
ops.prune_backups=lambda *_:None
attempts=0
def backup(root):
    global attempts
    attempts+=1
    (root/'attempts').write_text(str(attempts))
    if attempts==1:
        error=sqlite3.OperationalError('SECRET-CANARY')
        error.sqlite_errorname='SQLITE_BUSY'
        raise error
    return root/'completed-bundle'
ops.backup=backup
ops.run(root)
"""
            parent=subprocess.Popen([sys.executable,"-c",script,str(root)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            try:
                until(lambda:json.loads(statefile.read_text()).get("backup",{}).get("status")=="error",10)
                failed=json.loads(statefile.read_text())["backup"]
                self.assertEqual(failed["sqlite_error"],"SQLITE_BUSY")
                self.assertEqual(failed["last_success"]["path"],"previous-bundle")
                self.assertNotIn("SECRET-CANARY",json.dumps(failed))
                until(lambda:json.loads(statefile.read_text()).get("backup",{}).get("path")==str(root/"completed-bundle"),12)
                self.assertEqual((root/"attempts").read_text(),"2")
                self.assertEqual(json.loads(statefile.read_text())["backup"]["status"],"complete")
                (directory/"STOP").touch();parent.wait(timeout=10)
                self.assertEqual(parent.returncode,0,parent.stderr.read().decode())
            finally:
                if parent.poll() is None:parent.terminate();parent.wait()
                parent.stderr.close()

    def test_config_cannot_inject_arbitrary_commands_or_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            self.assertEqual(set(commands(root,config(root))),{"collector","reference","explorer","alerts"})
            (root/"ops.local.json").write_text('{"command":"trade"}')
            with self.assertRaises(ValueError):config(root)
            (root/"ops.local.json").write_text('{"port":true}')
            with self.assertRaises(ValueError):config(root)
    @unittest.skipUnless(os.name=="nt","Windows job-object ownership")
    def test_job_close_kills_only_its_owned_disposable_child(self):
        job=Job();child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            job.add(child);job.close();child.wait(timeout=5)
        finally:
            job.close()
            if child.poll() is None:child.terminate();child.wait()
    def test_crashed_worker_restarts_and_intentional_stop_persists(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/"artifacts/ops").mkdir(parents=True)
            worker="from pathlib import Path;import time; p=Path('count');n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n));time.sleep(0.1 if n==1 else 60);raise SystemExit(7)"
            script="from pathlib import Path;import sys;from othryss import ops;ops.commands=lambda *_:{'collector':['-c',sys.argv[2]]};ops.run(Path(sys.argv[1]))"
            parent=subprocess.Popen([sys.executable,"-c",script,str(root),worker],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            try:
                until(lambda:(root/"count").exists() and (root/"count").read_text()=="2",15)
                with self.assertRaises(ValueError):
                    with import_lock(root/"artifacts/ops/supervisor"):pass
                # Simulated worker obeys no stop sentinel; terminate this owned test child
                # to keep the graceful-stop test fast, then request permanent shutdown.
                state=json.loads((root/"artifacts/ops/status.json").read_text())
                self.assertGreaterEqual(state["services"]["collector"]["restarts"],1)
                (root/"artifacts/ops/STOP").touch()
                os.kill(state["services"]["collector"]["pid"],signal_number())
                parent.wait(timeout=10)
                self.assertEqual(parent.returncode,0,parent.stderr.read().decode())
                self.assertEqual(json.loads((root/"artifacts/ops/status.json").read_text())["status"],"stopped")
            finally:
                if parent.poll() is None:parent.terminate();parent.wait()
                parent.stderr.close()


def signal_number():
    import signal
    return signal.SIGTERM


if __name__=="__main__":unittest.main()
