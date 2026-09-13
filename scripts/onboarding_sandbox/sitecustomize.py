"""Test-process-only transport and scheduler isolation, loaded through PYTHONPATH."""
import io
import json
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import OpenerDirector

ROOT = Path(os.environ["OTHRYSS_ONBOARDING_SANDBOX"]).resolve()
if Path.cwd().resolve() != ROOT or not (ROOT / "SANDBOX").is_file():
    os._exit(90)
original_open = OpenerDirector.open


def opened(self, request, *args, **kwargs):
    url = request.full_url if hasattr(request, "full_url") else request
    parsed = urlparse(url)
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return original_open(self, request, *args, **kwargs)
    if parsed.hostname != "external-api.demo.kalshi.co" or request.get_method() != "GET":
        raise RuntimeError("Sandbox rejected external request")
    assert request.get_header("Kalshi-access-signature")
    endpoint = parsed.path.removeprefix("/trade-api/v2")
    fixtures = json.loads((ROOT / "synthetic-responses.json").read_text(encoding="utf-8"))
    if endpoint == "/api_keys":
        payload = {"api_keys":[{"api_key_id":"sandbox-only-key", "scopes":["read"]}]}
    elif endpoint == "/historical/cutoff":
        payload = fixtures["cutoff"]
    elif endpoint == "/portfolio/balance":
        payload = {"balance":0,"portfolio_value":0}
    elif endpoint == "/portfolio/positions":
        payload = {"market_positions":[],"cursor":""}
    elif endpoint == "/portfolio/settlements":
        payload = {"settlements":[],"cursor":""}
    else:
        cursor = parse_qs(parsed.query).get("cursor", [""])[0]
        payload = fixtures["pages"][endpoint + "|" + cursor]
    log = ROOT / "sandbox-requests"
    log.mkdir(exist_ok=True)
    with (log / f"{os.getpid()}.jsonl").open("a", encoding="utf-8") as out:
        out.write(json.dumps({"method":"GET","endpoint":endpoint}) + "\n")
    return io.BytesIO(json.dumps(payload).encode())


OpenerDirector.open = opened
original_connect = socket.socket.connect


def connected(self, address):
    if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
        raise RuntimeError("Sandbox forbids non-loopback network connections")
    return original_connect(self, address)


socket.socket.connect = connected
original_run = subprocess.run


def run(args, *pos, **kwargs):
    # Simulate a fresh machine with no Othryss task. Never inspect or change the
    # real user's installed task, which belongs to their production checkout.
    if isinstance(args, list) and any("Get-ScheduledTask" in str(a) for a in args):
        return subprocess.CompletedProcess(args, 2)
    return original_run(args, *pos, **kwargs)


subprocess.run = run
