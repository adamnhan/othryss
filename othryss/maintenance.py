"""Verified spool reclamation and consistent SQLite backup/isolated restore."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .storage import encode, import_lock, utc_now


def checksum(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle,"sha256").hexdigest()


def reclaim(history, spool):
    """Delete only sealed, hash-verified segments fully committed in this history DB."""
    spool=Path(spool).resolve(); removed=0; freed=0
    if not spool.is_dir() or not Path(history).is_file(): return {"segments":0,"bytes":0}
    with import_lock(spool/"maintenance"), closing(sqlite3.connect(Path(history).resolve().as_uri()+"?mode=ro",uri=True)) as db:
        for ack in sorted(spool.glob("*.jsonl.acked"))[:1000]:
            path=ack.with_name(ack.name[:-6]); closed=path.with_name(path.name+".closed")
            if not re.fullmatch(r"[a-f0-9]{32}(?:\.\d{6,})?\.jsonl",path.name): continue
            if any(p.is_symlink() or p.resolve().parent!=spool for p in (path,closed,ack)): continue
            if not path.is_file() or not closed.is_file() or max(ack.stat().st_size,closed.stat().st_size)>1024: continue
            metadata=json.loads(ack.read_text(encoding="utf-8"))
            if metadata!=json.loads(closed.read_text(encoding="utf-8")): continue
            size=path.stat().st_size
            row=db.execute("SELECT 1 FROM telemetry_files WHERE path=? AND byte_offset=? AND last_error IS NULL",(str(path),size)).fetchone()
            if not row or metadata.get("size")!=size or checksum(path)!=metadata.get("sha256"): continue
            path.unlink(); closed.unlink(); ack.unlink()
            removed+=1; freed+=size
    return {"segments":removed,"bytes":freed}


class BackupTimeout(TimeoutError):
    def __init__(self, database, stage):
        self.database=database
        self.stage=stage
        super().__init__(f"{database}: {stage} exceeded the database backup time budget")


def sqlite_copy(source, target, *, timeout_seconds=120, progress_hook=None):
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+"?mode=ro",uri=True,timeout=10)) as src, closing(sqlite3.connect(target)) as dst:
        started=time.monotonic()
        def progress(status, remaining, total):
            if time.monotonic()-started>timeout_seconds: raise BackupTimeout(Path(target).name,"copy")
            if progress_hook: progress_hook(status, remaining, total)
        # Pin a committed source snapshot before incremental copying. Without this,
        # writes from other connections can repeatedly restart the SQLite backup.
        # WAL writers keep running; do not acquire an IMMEDIATE/write transaction.
        src.execute("PRAGMA query_only=ON")
        src.execute("BEGIN")
        src.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        src.backup(dst,pages=256,progress=progress,sleep=.05)
        src.rollback()  # Release the source snapshot before checking the copy.
        expired=False
        def check_deadline():
            nonlocal expired
            expired=time.monotonic()-started>timeout_seconds
            return int(expired)
        dst.set_progress_handler(check_deadline,10000)
        try:
            if check_deadline(): raise BackupTimeout(Path(target).name,"integrity_check")
            if dst.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise ValueError("Backup integrity failed")
        except sqlite3.OperationalError as exc:
            if expired and getattr(exc,"sqlite_errorcode",None)==sqlite3.SQLITE_INTERRUPT:
                raise BackupTimeout(Path(target).name,"integrity_check") from exc
            raise
        finally:
            dst.set_progress_handler(None,0)


def backup(root, destination=None):
    root=Path(root).resolve(); backups=root/"artifacts/backups"
    backups.mkdir(parents=True,exist_ok=True)
    destination=Path(destination).resolve() if destination else backups/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"-"+uuid.uuid4().hex[:8])
    if destination.parent!=backups or destination.exists(): raise ValueError("Backup must be a new direct child of artifacts/backups")
    spool=root/"artifacts/telemetry/lip"
    sources=[root/"artifacts/history/othryss.sqlite",root/"artifacts/reference/quotes.sqlite",root/"artifacts/alerts/delivery.sqlite"]
    required=sum(p.stat().st_size for p in sources if p.exists())+sum(p.stat().st_size for p in spool.glob("*.jsonl"))
    if shutil.disk_usage(root).free < required*2+64*1024*1024: raise OSError("Insufficient backup disk reserve")
    with import_lock(backups/"backup"), import_lock(spool/"maintenance"):
        destination.mkdir(); (destination/"spool").mkdir()
        files={}; started=utc_now()
        # Copy spool first, then history. Imported/deleted segments are already in DB.
        # A live partial trailing record is excluded; replay deduplicates complete lines.
        for path in sorted(spool.iterdir()):
            if path.is_symlink() or not path.is_file(): continue
            if not re.fullmatch(r"[a-f0-9]{32}(?:\.\d{6,})?\.jsonl|[a-f0-9]{32}\.health\.json",path.name): continue
            target=destination/"spool"/path.name
            if path.suffix==".jsonl":
                with path.open("rb") as source, target.open("xb") as out:
                    # Fix an upper bound even if a producer continues appending.
                    remaining=path.stat().st_size
                    while remaining>0:
                        line=source.readline(min(16385,remaining)); remaining-=len(line)
                        if not line: break
                        if not line.endswith(b"\n"): break
                        if len(line)>16384: raise ValueError("Spool record exceeds contract")
                        out.write(line)
            else:
                if path.stat().st_size>8192: raise ValueError("Oversized heartbeat")
                shutil.copyfile(path,target)
            files[str(target.relative_to(destination)).replace("\\","/")]=checksum(target)
        for name,source in (("history.sqlite",root/"artifacts/history/othryss.sqlite"),("references.sqlite",root/"artifacts/reference/quotes.sqlite")):
            if not source.is_file(): raise ValueError("Both evidence databases must exist before backup")
            sqlite_copy(source,destination/name); files[name]=checksum(destination/name)
        # Queue first would be needed for automatic replay; restored queues are
        # deliberately disarmed instead, so recovery cannot re-send notifications.
        alert_db=root/"artifacts/alerts/delivery.sqlite"
        if alert_db.is_file():
            sqlite_copy(alert_db,destination/"alerts.sqlite");files["alerts.sqlite"]=checksum(destination/"alerts.sqlite")
        config=root/"ops.local.json"
        if config.exists():
            # Only the known nonsecret operational settings are copied.
            raw=json.loads(config.read_text(encoding="utf-8-sig"))
            permitted={k:raw[k] for k in ("account","environment","port","reconcile_ticker","backup_interval_seconds","backup_keep","lip_supervisor_log") if k in raw}
            (destination/"ops.local.json").write_text(encode(permitted),encoding="utf-8");files["ops.local.json"]=checksum(destination/"ops.local.json")
        manifest={"version":1,"started_at":started,"finished_at":utc_now(),"files":files,"source_root":str(root),"consistency":"Individually consistent SQLite snapshots; spool copied before history. No atomic cross-database snapshot. Credentials excluded."}
        (destination/"manifest.json").write_text(encode(manifest),encoding="utf-8")
        (destination/"COMPLETE").write_text(checksum(destination/"manifest.json"),encoding="ascii")
    return destination


def verify(bundle):
    bundle=Path(bundle).resolve()
    if (bundle/"COMPLETE").read_text(encoding="ascii")!=checksum(bundle/"manifest.json"): raise ValueError("Incomplete or changed backup")
    manifest=json.loads((bundle/"manifest.json").read_text(encoding="utf-8"))
    if manifest.get("version")!=1 or not {"history.sqlite","references.sqlite"}<=manifest.get("files",{}).keys(): raise ValueError("Unsupported backup manifest")
    for name,expected in manifest["files"].items():
        if not (name in {"history.sqlite","references.sqlite","alerts.sqlite","ops.local.json"} or re.fullmatch(r"spool/[a-f0-9]{32}(?:\.\d{6,})?\.jsonl|spool/[a-f0-9]{32}\.health\.json",name)): raise ValueError("Unexpected backup path")
        path=bundle/name
        if path.is_symlink() or not path.resolve().is_relative_to(bundle) or checksum(path)!=expected: raise ValueError("Backup file verification failed")
    for name in ("history.sqlite","references.sqlite",*(["alerts.sqlite"] if "alerts.sqlite" in manifest["files"] else [])):
        with closing(sqlite3.connect((bundle/name).as_uri()+"?mode=ro",uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0]!="ok": raise ValueError("Backup database integrity failed")
    return manifest


def restore(bundle, target):
    bundle=Path(bundle).resolve(); target=Path(target).resolve()
    manifest=verify(bundle)
    if target.exists(): raise ValueError("Restore requires a new directory; live data is never overwritten")
    if target.is_relative_to(bundle): raise ValueError("Restore target cannot be inside its backup")
    target.mkdir(parents=True)
    for name in manifest["files"]:
        dest=target/name; dest.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(bundle/name,dest)
    with closing(sqlite3.connect(target/"history.sqlite")) as db, db:
        # Restored files can be shorter than later DB checkpoints. Replay their complete
        # lines from zero against immutable IDs, using the new absolute spool location.
        db.execute("DELETE FROM telemetry_files")
    if (target/"alerts.sqlite").exists():
        with closing(sqlite3.connect(target/"alerts.sqlite")) as db, db:
            db.execute("UPDATE deliveries SET status='unknown',error='restored_inflight' WHERE status='sending'")
            db.execute("UPDATE attempts SET status='unknown',error='restored_inflight' WHERE status='sending'")
            db.execute("UPDATE deliveries SET status='canceled',error='restored_disarmed' WHERE status IN ('pending','retry')")
            db.execute("UPDATE routes SET active=0,fingerprint='restored_disarmed',error='restored_disarmed'")
            db.execute("UPDATE worker SET status='stopped'")
    (target/"RESTORED.json").write_text(encode({"restored_at":utc_now(),"backup":str(bundle),"checkpoint_policy":"Replay restored spool from zero; immutable event IDs deduplicate"}),encoding="utf-8")
    return target


def prune_backups(root, keep=7):
    if not 1<=keep<=100: raise ValueError("Invalid backup retention")
    base=(Path(root)/"artifacts/backups").resolve()
    candidates=sorted((p for p in base.iterdir() if p.is_dir() and not p.is_symlink() and (p/"COMPLETE").is_file()),key=lambda p:p.name,reverse=True)
    for path in candidates[keep:]:
        if path.resolve().parent!=base: raise ValueError("Backup path escaped root")
        verify(path)
        shutil.rmtree(path)  # Resolved, verified, direct child of the backup workspace.
