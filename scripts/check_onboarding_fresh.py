"""Run real onboarding/service CLIs in a clean copy with synthetic GET responses."""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_history import FakeClient


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package",type=Path,help="Test the extracted pilot ZIP instead of copying source")
    args=parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    root = ROOT / "artifacts/onboarding" / ("fresh-" + stamp)
    root.mkdir(parents=True)
    if args.package:
        from build_pilot import verify,PREFIX
        verify(args.package)
        with zipfile.ZipFile(args.package) as archive: archive.extractall(root)
        root=root/PREFIX
    else:
        for folder in ("othryss", "web", "fixtures"):
            shutil.copytree(ROOT / folder, root / folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("requirements-import.txt", "requirements-reference.txt"):
            shutil.copyfile(ROOT / name, root / name)
    (root / "SANDBOX").touch()
    shim = root / "test-transport"; shim.mkdir()
    shutil.copyfile(ROOT / "scripts/onboarding_sandbox/sitecustomize.py", shim / "sitecustomize.py")
    source = FakeClient()
    (root / "synthetic-responses.json").write_text(json.dumps({"cutoff":source.cutoff,"pages":{path+"|"+cursor:payload for (path,cursor),payload in source.pages.items()}}), encoding="utf-8")
    # A new interpreter environment; reuse installed dependency distributions
    # to make this regression offline and repeatable. This is not a pip test.
    subprocess.run([sys.executable,"-m","venv","--system-site-packages",str(root / ".venv")], check=True, capture_output=True, timeout=60)
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    env = {k:v for k,v in os.environ.items() if not k.upper().startswith(("OTHRYSS", "KALSHI", "PYTHON"))}
    env.update(PYTHONPATH=str(shim), OTHRYSS_ONBOARDING_SANDBOX=str(root), PYTHONUTF8="1")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    records = []

    def command(module, *args, expected=0, save=None):
        p = subprocess.run([str(python),"-m",module,*args], cwd=root, env=env, capture_output=True, text=True, timeout=45)
        records.append({"command":[module,*args],"exit_code":p.returncode})
        if save: (root / save).write_text(p.stdout, encoding="utf-8")
        if p.returncode != expected:
            raise AssertionError(f"{module} {args}: expected {expected}, got {p.returncode}: {p.stdout} {p.stderr}")
        return p.stdout

    def status():
        path = root / "artifacts/ops/status.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def until(predicate, seconds=45):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate(): return
            time.sleep(1)
        raise AssertionError("Isolated service did not reach the expected state")

    success = False
    try:
        init = ["--account","sandbox-account","--environment","demo","--port",str(port)]
        command("othryss.onboarding","check",expected=1,save="missing-setup.json")
        command("othryss.onboarding","init",*init)
        command("othryss.onboarding","check",expected=1,save="missing-credentials.json")
        # Generate a new RSA key valid only for the synthetic transport.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        (root / "sandbox-key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        (root / "local.env").write_text("OTHRYSS_KALSHI_KEY_ID=sandbox-only-key\nOTHRYSS_KALSHI_PRIVATE_KEY_PATH=sandbox-key.pem\n",encoding="utf-8")
        config = {name:(root / name).read_bytes() for name in ("ops.local.json","local.env","alerts.local.json")}
        command("othryss.onboarding","init",*init)
        command("othryss.onboarding","init","--account","wrong-account","--environment","demo",expected=1)
        assert all((root / name).read_bytes() == value for name,value in config.items())
        command("othryss.onboarding","check",save="preflight.json")
        command("othryss.onboarding","check","--live",expected=1,save="not-running.json")
        command("othryss.ops","start")
        first_pid = status()["pid"]
        command("othryss.ops","start")
        assert status()["pid"] == first_pid
        print("Fresh setup and idempotent startup passed; waiting for initial collection.",flush=True)
        deadline = time.monotonic() + 50
        while True:
            p = subprocess.run([str(python),"-m","othryss.onboarding","check","--live"],cwd=root,env=env,capture_output=True,text=True,timeout=30)
            live = json.loads(p.stdout)
            if p.returncode == 0: break
            if time.monotonic() >= deadline: raise AssertionError(json.dumps(live))
            time.sleep(2)
        (root / "live-ready.json").write_text(json.dumps(live,indent=2),encoding="utf-8")
        command("othryss.onboarding","check","--live","--require-telemetry",expected=1,save="telemetry-pending.json")
        command("othryss.onboarding","check","--live","--require-alerts",expected=1,save="alerts-pending.json")
        with urlopen(f"http://127.0.0.1:{port}/api/history/accounts",timeout=10) as response:
            account = json.load(response)["accounts"][0]
        assert account["account"] == "sandbox-account" and account["environment"] == "demo"
        assert account["event_counts"]["ORDER_FILL"] == 2 and account["order_count"] == 2
        with urlopen(f"http://127.0.0.1:{port}/",timeout=10) as response:
            assert b"Othryss" in response.read()
        until(lambda:status().get("backup",{}).get("status") == "complete")
        automatic = Path(status()["backup"]["path"])
        assert (automatic / "COMPLETE").is_file()
        assert all((p / "COMPLETE").exists() for p in (root / "artifacts/backups").iterdir() if p.is_dir())
        command("othryss.onboarding","check","--live",save="live-ready.json")
        bundle = command("othryss.ops","backup").strip()
        command("othryss.ops","restore","--backup",bundle,"--to",str(root / "restored"))
        print("Real collection, explorer, optional-integration gates, first automatic backup and restore passed.",flush=True)
        command("othryss.ops","stop")
        until(lambda:status().get("status") == "stopped",75)
        command("othryss.onboarding","check","--live",expected=1,save="stopped.json")
        command("othryss.ops","start")
        assert status()["pid"] != first_pid
        until(lambda:all(s.get("status") == "running" for s in status().get("services",{}).values()))
        with urlopen(f"http://127.0.0.1:{port}/api/history/accounts",timeout=10) as response:
            after = json.load(response)["accounts"][0]
        assert after["scope_id"] == account["scope_id"] and after["event_counts"]["ORDER_FILL"] == 2
        success = True
    finally:
        if status().get("status") in {"running","stopping"}:
            command("othryss.ops","stop")
            until(lambda:status().get("status") == "stopped",75)
        (root / "sandbox-key.pem").unlink(missing_ok=True)
        requests = [json.loads(line) for p in (root / "sandbox-requests").glob("*.jsonl") for line in p.read_text().splitlines()]
        report = {"passed":success,"checked_at":datetime.now(timezone.utc).isoformat(),"root":str(root),"package":str(args.package.resolve()) if args.package else None,"commands":records,
                  "synthetic_requests":len(requests),"request_methods":sorted({r["method"] for r in requests}),"final_status":status(),
                  "limits":["Existing installed dependency distributions reused by a new virtual environment","Kalshi GET responses synthetic; non-loopback socket connections blocked","Task Scheduler absence simulated; actual CLI, processes, signing, SQLite, explorer, backup and restore exercised","Bot and external alert integrations intentionally unconfigured"]}
        (root / "acceptance.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
        print(json.dumps({"passed":success,"report":str(root / "acceptance.json")}),flush=True)


if __name__ == "__main__":
    main()
