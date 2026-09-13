"""Current source recovery and explicitly observed LIP retirement evidence.

No process control or exchange writes. Historical telemetry is never rewritten.
"""
import json
import re
import os
from datetime import datetime, timezone
from pathlib import Path

from .replay import timestamp
from .storage import encode

FRESH_SECONDS = 180
QUIET_SECONDS = 120
MAX_LOG_BYTES = 4 * 1024 * 1024


def retirements(path):
    """Bounded log tail; only an explicit eviction following an exact launch counts."""
    if path is None:
        return []
    result, launches = [], {}
    path = Path(path)
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - MAX_LOG_BYTES))
        if size > MAX_LOG_BYTES:
            handle.readline()  # Never parse a partial leading record.
        content = handle.read(MAX_LOG_BYTES)
    for line in content.decode("utf-8", errors="replace").splitlines():
        if len(line) > 16384:
            continue
        if line.startswith("{"):
            try:
                item = json.loads(line)
                if item.get("event") == "child_launch":
                    ticker = item.get("ticker", "")
                    # The filename supplies launch time; run IDs contain finer precision.
                    name = str(item.get("stdout", "")).replace("\\", "/").rsplit("/", 1)[-1]
                    match = re.fullmatch(r"lip_portfolio_(.+)_(\d{8}T\d{6}Z)\.out\.log", name)
                    if match and match[1] == ticker and re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", ticker) and type(item.get("pid")) is int and 0 < item["pid"] < 2**32:
                        launches[ticker] = {"ticker": ticker, "pid": item["pid"], "launch_at": datetime.strptime(match[2], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat(), "launch_file": name}
                    else:
                        launches.pop(ticker, None)
            except (ValueError, TypeError):
                continue
        match = re.fullmatch(r"\[evict\] ([A-Za-z0-9_.:-]+) expected \$[0-9.]+ < \$[0-9.]+ sustained over \d+ scans; cancelled \d+ resting order\(s\)", line)
        if match and match[1] in launches:
            result.append(launches.pop(match[1]) | {"reason": "supervisor_eviction", "log_record": line, "log_path": str(path.resolve())})
    return result


def process_absent(pid):
    """Fail closed on permission errors or PID reuse; never send a signal."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID absent.
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259)
    finally:
        kernel.CloseHandle(handle)


def observe(store, scope, *, supervisor_log=None, now=None, absent=process_absent):
    """Persist recovery baselines. Re-reading one old heartbeat cannot show recovery."""
    now = now or datetime.now(timezone.utc)
    retired = []
    if supervisor_log is not None:
        try:
            retired = retirements(supervisor_log)
        except OSError:
            pass  # Missing evidence never establishes an expected shutdown.
    rows = store.db.execute("SELECT session_id,health_json FROM telemetry_health WHERE scope_id=?", (scope,)).fetchall()
    starts = {}
    for row in store.db.execute("SELECT session_id,instrument_id,occurred_at,canonical_json FROM telemetry_records WHERE scope_id=? AND type='PRODUCER_START'", (scope,)):
        r = json.loads(row["canonical_json"])
        if r.get("producer") == "lip-requote-probe" and r.get("subaccount") == 0:
            starts.setdefault(row["instrument_id"], []).append((row["session_id"], timestamp(row["occurred_at"])))
    matched = {}
    for event in retired:
        candidates = [(session, at) for session, at in starts.get(event["ticker"], []) if 0 <= (at - timestamp(event["launch_at"])).total_seconds() < 10]
        if len(candidates) == 1 and absent(event["pid"]):
            matched[candidates[0][0]] = event
    with store.db:
        for row in rows:
            h = json.loads(row["health_json"])
            session = row["session_id"]
            beat = h.get("heartbeat_at")
            if not beat:
                continue
            old = store.db.execute("SELECT * FROM source_health WHERE scope_id=? AND session_id=?", (scope, session)).fetchone()
            counters = encode({k: h.get(k, 0) for k in ("dropped", "write_failures")})
            fresh = -5 <= (now - timestamp(beat)).total_seconds() <= FRESH_SECONDS
            reset = not old or old["counters_json"] != counters or not fresh or h.get("capped") or (old and (
                not -5 <= (now - timestamp(old["observed_at"])).total_seconds() <= FRESH_SECONDS or
                not 0 <= (timestamp(beat) - timestamp(old["last_heartbeat"])).total_seconds() <= FRESH_SECONDS))
            quiet = now.isoformat() if reset else old["quiet_since"]
            baseline = h.get("last_sequence", 0) if reset else old["baseline_sequence"]
            retirement = None
            prior_retirement = json.loads(old["retirement_json"]) if old and old["retirement_json"] else None
            if prior_retirement:
                # Retained explicit evidence survives log rotation. A revived session
                # permanently invalidates that old eviction, even if it goes stale again.
                if prior_retirement.get("heartbeat_at") != beat:
                    prior_retirement["invalidated_by_heartbeat"] = beat
                elif not prior_retirement.get("invalidated_by_heartbeat") and not prior_retirement.get("process_exit_verified"):
                    # Repair older observations that rechecked a historical PID and
                    # forgot its confirmed exit when Windows reused that number.
                    proof = store.db.execute("""SELECT check_id FROM bot_checks WHERE scope_id=? AND session_id=?
                        AND json_extract(result_json,'$.health.retirement.process_exit_verified')=1
                        AND json_extract(result_json,'$.health.retirement.heartbeat_at')=?
                        AND json_extract(result_json,'$.health.retirement.launch_at')=?
                        AND json_extract(result_json,'$.health.retirement.pid')=? LIMIT 1""",
                        (scope,session,beat,prior_retirement["launch_at"],prior_retirement["pid"])).fetchone()
                    if proof:
                        prior_retirement["process_exit_verified"] = True
                        prior_retirement["restored_from_check"] = proof[0]
                # A confirmed exit is a historical fact. Never query that PID again:
                # a later process using it is not evidence this session came back.
                retirement = encode(prior_retirement)
            elif session in matched and not fresh and timestamp(beat) <= now:
                # Bind the stop inference to this exact last heartbeat. A new heartbeat
                # invalidates it until independently re-observed as stale.
                candidate = matched[session] | {"heartbeat_at": beat, "observed_at": now.isoformat(), "process_exit_verified": True,
                    "interpretation": "Expected retirement from the configured supervisor log; not proof of exchange position or order cleanup."}
                retirement = encode(candidate)
            store.db.execute("INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?,?,?)",
                             (scope, session, now.isoformat(), beat, counters, quiet, baseline, retirement))


def context(db, scope, session, health):
    if db.execute("PRAGMA user_version").fetchone()[0] < 7:
        return None
    row = db.execute("SELECT * FROM source_health WHERE scope_id=? AND session_id=?", (scope, session)).fetchone()
    if not row or not health or row["last_heartbeat"] != health.get("heartbeat_at"):
        return None
    return dict(row)


def recovered(row, health, now):
    return bool(row and -5 <= (now - timestamp(row["observed_at"])).total_seconds() <= FRESH_SECONDS
                and (now - timestamp(row["quiet_since"])).total_seconds() >= QUIET_SECONDS
                and health.get("last_sequence", 0) > row["baseline_sequence"])
