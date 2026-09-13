"""Local Othryss service supervisor. Never manages trading processes."""
import argparse
import ctypes
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

from .fixture import ROOT
from .maintenance import BackupTimeout, backup, prune_backups, reclaim, restore
from .storage import encode, import_lock, utc_now

DEFAULTS={"account":"my-account","environment":"demo","port":8766,"reconcile_ticker":None,"backup_interval_seconds":21600,"backup_keep":7,"lip_supervisor_log":None}


def config(root):
    path=root/"ops.local.json"
    raw=json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(raw,dict) or set(raw)-DEFAULTS.keys(): raise ValueError("Unknown ops setting; credentials do not belong here")
    result=DEFAULTS|raw
    if not isinstance(result["account"],str) or not result["account"] or len(result["account"])>100: raise ValueError("Invalid account")
    if result["environment"] not in {"production","demo"}: raise ValueError("Invalid environment")
    for key,low,high in (("port",1024,65535),("backup_interval_seconds",300,604800),("backup_keep",1,100)):
        if type(result[key]) is not int or not low<=result[key]<=high: raise ValueError("Invalid ops bound")
    if result["reconcile_ticker"] is not None and (not isinstance(result["reconcile_ticker"],str) or not __import__("re").fullmatch(r"[A-Za-z0-9_.:-]{1,200}",result["reconcile_ticker"])): raise ValueError("Invalid reconciliation ticker")
    if result["lip_supervisor_log"] is not None and (not isinstance(result["lip_supervisor_log"], str) or not Path(result["lip_supervisor_log"]).is_absolute()):
        raise ValueError("Supervisor log must be an absolute local path")
    return result


def commands(root, settings):
    ops=root/"artifacts/ops"
    shared=["--account",settings["account"],"--environment",settings["environment"]]
    collector=["-m","othryss.collector_cli",*shared,"--telemetry-dir",str(root/"artifacts/telemetry/lip"),"--stop-file",str(ops/"collector.stop")]
    if settings.get("lip_supervisor_log"):
        collector += ["--lip-supervisor-log", settings["lip_supervisor_log"]]
    if settings["reconcile_ticker"]: collector += ["--reconcile-ticker",settings["reconcile_ticker"]]
    return {"collector":collector,"reference":["-m","othryss.reference_cli",*shared,"--stop-file",str(ops/"reference.stop")],"explorer":["-m","othryss.server","--port",str(settings["port"])],"alerts":["-m","othryss.alerts_cli","run","--stop-file",str(ops/"alerts.stop")]}


def atomic(path, data):
    temp=path.with_suffix(".tmp")
    temp.write_text(encode(data),encoding="utf-8");os.replace(temp,path)


class Job:
    """Windows kills owned readonly children if the supervisor disappears."""
    def __init__(self):
        self.handle=None
        if os.name!="nt": return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[("process_time",ctypes.c_int64),("job_time",ctypes.c_int64),("flags",w.DWORD),("min_ws",ctypes.c_size_t),("max_ws",ctypes.c_size_t),("active",w.DWORD),("affinity",ctypes.c_size_t),("priority",w.DWORD),("scheduling",w.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint64) for name in ("read_ops","write_ops","other_ops","read_bytes","write_bytes","other_bytes")]
        class Extended(ctypes.Structure):
            _fields_=[("basic",Basic),("io",IO),("process_mem",ctypes.c_size_t),("job_mem",ctypes.c_size_t),("peak_process",ctypes.c_size_t),("peak_job",ctypes.c_size_t)]
        self.kernel=ctypes.WinDLL("kernel32",use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];self.kernel.CreateJobObjectW.restype=w.HANDLE
        self.kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        self.kernel.CloseHandle.argtypes=[w.HANDLE]
        self.handle=self.kernel.CreateJobObjectW(None,None)
        if not self.handle: raise ctypes.WinError(ctypes.get_last_error())
        info=Extended();info.basic.flags=0x2000
        if not self.kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)):
            self.close();raise ctypes.WinError(ctypes.get_last_error())
    def add(self,process):
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle,int(process._handle)):
            process.terminate();process.wait();raise ctypes.WinError(ctypes.get_last_error())
    def close(self):
        if self.handle: self.kernel.CloseHandle(self.handle);self.handle=None


def log_output(pipe,path,max_bytes=5*1024*1024):
    try:
        with pipe:
            while chunk:=pipe.read1(4096):
                try:
                    if path.exists() and path.stat().st_size+len(chunk)>max_bytes:
                        for n in range(2,0,-1):
                            old=path.with_name(path.name+f".{n}");new=path.with_name(path.name+f".{n+1}")
                            if old.exists(): os.replace(old,new)
                        os.replace(path,path.with_name(path.name+".1"))
                    with path.open("ab") as handle: handle.write(chunk)
                except OSError: pass  # Keep draining if log storage is unavailable.
    except Exception:
        pass


def launch(root,name,args,job):
    process=subprocess.Popen([sys.executable,*args],cwd=root,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
    job.add(process)
    thread=threading.Thread(target=log_output,args=(process.stdout,root/"artifacts/ops"/(name+".log")),daemon=True);thread.start()
    return process


def status(root):
    path=root/"artifacts/ops/status.json"
    result=json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status":"not_started","services":{}}
    if "heartbeat_at" in result:
        from .replay import timestamp
        from datetime import datetime,timezone
        result["stale"]=(datetime.now(timezone.utc)-timestamp(result["heartbeat_at"])).total_seconds()>10
    result["stop_requested"]=(root/"artifacts/ops/STOP").exists()
    result["application_health"]={}
    for name,path,table in (("collector",root/"artifacts/history/othryss.sqlite","sync_state"),("reference",root/"artifacts/reference/quotes.sqlite","workers")):
        try:
            with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
                db.row_factory=sqlite3.Row
                rows=[dict(r) for r in db.execute(f"SELECT scope_id,status,heartbeat_at FROM {table}")]
                from datetime import datetime,timezone
                from .replay import timestamp
                for row in rows:
                    age=(datetime.now(timezone.utc)-timestamp(row["heartbeat_at"])).total_seconds() if row["heartbeat_at"] else None
                    row["stale"]=age is None or not -5<=age<=(180 if name=="collector" else 15)
                result["application_health"][name]=rows
        except sqlite3.Error: result["application_health"][name]="unavailable"
    try:
        with closing(sqlite3.connect((root/"artifacts/alerts/delivery.sqlite").resolve().as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
            row=db.execute("SELECT status,heartbeat_at FROM worker WHERE id=1").fetchone()
            result["application_health"]["alerts"]={"status":row[0],"stale":time.time()-row[1]>120} if row else {"status":"unavailable"}
    except sqlite3.Error: result["application_health"]["alerts"]={"status":"unavailable"}
    return result


def scheduled_start(root):
    if os.name!="nt": return False
    script="$ErrorActionPreference='Stop'; $t=Get-ScheduledTask -TaskName 'Othryss Local Services' -ErrorAction SilentlyContinue; if (-not $t) { exit 2 }; if (@($t.Actions | Where-Object { $_.Arguments -eq '-m othryss.ops run' -and $_.WorkingDirectory -eq $env:OTHRYSS_TASK_ROOT }).Count -ne 1) { exit 3 }; Start-ScheduledTask -TaskName 'Othryss Local Services'"
    result=subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",script],env=os.environ|{"OTHRYSS_TASK_ROOT":str(root)},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW,timeout=20)
    if result.returncode==2: return False
    if result.returncode: raise RuntimeError("Scheduled supervisor could not start; inspect task identity and permissions")
    return True


def backup_sources_ready(root):
    """Fresh startup must finish creating schemas before the first backup."""
    for relative, version in (("history/othryss.sqlite",7),("reference/quotes.sqlite",1),("alerts/delivery.sqlite",1)):
        try:
            path=(Path(root)/"artifacts"/relative).resolve()
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)) as db:
                if db.execute("PRAGMA user_version").fetchone()[0]!=version: return False
        except sqlite3.Error:
            return False
    return True


def backup_interval(state, normal_interval):
    return min(300,normal_interval) if state.get("status")=="error" else normal_interval


def last_successful_backup(state):
    if state.get("status")=="complete":
        return {key:state[key] for key in ("path","finished_at")}
    return state.get("last_success")


def backup_failure(exc):
    # Keep provider messages, paths and arbitrary exception text out of status.
    result={"error":type(exc).__name__}
    if isinstance(exc,sqlite3.Error):
        result["sqlite_error"]=getattr(exc,"sqlite_errorname",None)
    if isinstance(exc,BackupTimeout):
        result.update(database=exc.database,stage=exc.stage)
    return result


def backup_attempt(root, keep, previous):
    result={"started_at":utc_now(),"last_success":last_successful_backup(previous)}
    try:
        bundle=backup(root)
    except Exception as exc:
        return result|{"status":"error","failed_at":utc_now(),**backup_failure(exc)}
    result.update(status="complete",path=str(bundle),finished_at=utc_now(),error=None)
    result["last_success"]={key:result[key] for key in ("path","finished_at")}
    try:
        prune_backups(root,keep)
    except Exception as exc:
        # Retention failure does not invalidate the new, completed backup.
        result["retention_error"]=backup_failure(exc)
    return result


def run(root):
    root=Path(root).resolve();settings=config(root);directory=root/"artifacts/ops";directory.mkdir(parents=True,exist_ok=True)
    stopfile=directory/"STOP"
    if stopfile.exists(): return
    stop=threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM): signal.signal(sig,lambda *_:stop.set())
    with import_lock(directory/"supervisor"):
        job=Job();processes={};next_start={};failures={};started={}
        data={"pid":os.getpid(),"status":"running","heartbeat_at":utc_now(),"services":{},"backup":{},"spool":{}}
        old=directory/"status.json"
        if old.exists():
            try: data["backup"]=json.loads(old.read_text())["backup"]
            except (ValueError,KeyError): pass
        last_backup=time.monotonic()-settings["backup_interval_seconds"]
        previous_attempt=data["backup"].get("failed_at") or data["backup"].get("finished_at")
        if previous_attempt:
            from datetime import datetime,timezone
            from .replay import timestamp
            age=(datetime.now(timezone.utc)-timestamp(previous_attempt)).total_seconds()
            last_backup=time.monotonic()-max(0,age)
        maintenance_thread=None;maintenance_result={};last_cleanup=0
        def maintain():
            maintenance_result.update(backup_attempt(root,settings["backup_keep"],data["backup"]))
        try:
            while not stop.is_set() and not stopfile.exists():
                now=time.monotonic()
                for name,args in commands(root,settings).items():
                    process=processes.get(name)
                    if process is not None and process.poll() is not None:
                        lived=now-started[name];failures[name]=1 if lived>=300 else failures.get(name,0)+1
                        next_start[name]=now+min(60,2**min(failures[name],6))
                        data["services"][name].update(status="backoff",last_exit_code=process.returncode,next_retry_seconds=round(next_start[name]-now),restarts=failures[name])
                        processes[name]=None;process=None
                    if process is None and now>=next_start.get(name,0):
                        (directory/(name+".stop")).unlink(missing_ok=True)
                        try:
                            process=launch(root,name,args,job);processes[name]=process;started[name]=now
                            data["services"][name]={"pid":process.pid,"status":"running","restarts":failures.get(name,0)}
                        except Exception as exc:
                            next_start[name]=now+60;data["services"][name]={"status":"start_failed","error":type(exc).__name__}
                if maintenance_thread is not None and not maintenance_thread.is_alive():
                    data["backup"]=dict(maintenance_result);maintenance_thread=None;last_backup=now
                if maintenance_thread is None and now-last_backup>=backup_interval(data["backup"],settings["backup_interval_seconds"]):
                    if backup_sources_ready(root):
                        last_backup=now;maintenance_result={};data["backup"]={"status":"running","started_at":utc_now(),"last_success":last_successful_backup(data["backup"])}
                        maintenance_thread=threading.Thread(target=maintain,daemon=True);maintenance_thread.start()
                    else:
                        data["backup"]={"status":"waiting_for_databases"}
                if now-last_cleanup>=10 and maintenance_thread is None:
                    last_cleanup=now
                    try: data["spool"]={"checked_at":utc_now(),**reclaim(root/"artifacts/history/othryss.sqlite",root/"artifacts/telemetry/lip")}
                    except Exception as exc: data["spool"]={"error":type(exc).__name__}
                data.update(heartbeat_at=utc_now());atomic(directory/"status.json",data)
                stop.wait(2)
        finally:
            data["status"]="stopping";atomic(directory/"status.json",data)
            for name in processes: (directory/(name+".stop")).touch()
            server=processes.get("explorer")
            if server is not None and server.poll() is None: server.terminate()
            deadline=time.monotonic()+60
            while any(p is not None and p.poll() is None for p in processes.values()) and time.monotonic()<deadline:
                data["heartbeat_at"]=utc_now();atomic(directory/"status.json",data);time.sleep(1)
            for name,process in processes.items():
                if process is not None:
                    if process.poll() is None: process.terminate()
                    process.wait(timeout=10);data["services"][name].update(status="stopped",last_exit_code=process.returncode)
            if maintenance_thread is not None: maintenance_thread.join(timeout=5)
            job.close();data.update(status="stopped",heartbeat_at=utc_now());atomic(directory/"status.json",data)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["start","stop","status","run","backup","restore"])
    parser.add_argument("--backup",type=Path);parser.add_argument("--to",type=Path)
    args=parser.parse_args();root=ROOT.resolve();directory=root/"artifacts/ops";directory.mkdir(parents=True,exist_ok=True)
    try:
        if args.action=="status": print(json.dumps(status(root),indent=2));return 0
        if args.action=="stop":
            (directory/"STOP").touch();print("Stop requested. The supervisor stops only its Othryss children; check status for completion.");return 0
        if args.action=="run": run(root);return 0
        if args.action=="backup": print(backup(root));return 0
        if args.action=="restore":
            if args.backup is None or args.to is None: raise ValueError("restore requires --backup and --to")
            print(restore(args.backup,args.to));return 0
        with import_lock(directory/"control"):
            config(root)
            try:
                with import_lock(directory/"supervisor"): pass
            except ValueError:
                print("Othryss supervisor is already running. Use status to inspect it.");return 0
            (directory/"STOP").unlink(missing_ok=True)
            child=None;launch_time=utc_now()
            if not scheduled_start(root):
                with (directory/"supervisor.log").open("ab") as log:
                    child=subprocess.Popen([sys.executable,"-m","othryss.ops","run"],cwd=root,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0,start_new_session=os.name!="nt")
            for _ in range(50):
                if child is not None and child.poll() is not None: raise RuntimeError("Supervisor exited at startup; inspect its log")
                current=status(root)
                if current.get("heartbeat_at","")>=launch_time and current.get("status")=="running":
                    print(f"Othryss supervisor started (PID {current['pid']}). Use python -m othryss.ops status.");return 0
                time.sleep(.2)
            raise RuntimeError("Supervisor startup not confirmed; inspect status before retrying")
    except Exception as exc:
        print(f"Operation failed: {type(exc).__name__}: {exc}",file=sys.stderr);return 1


if __name__=="__main__": raise SystemExit(main())
