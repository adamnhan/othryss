"""Build and verify an allowlisted pilot ZIP; never copy local configuration or data."""
import argparse
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT=Path(__file__).resolve().parents[1]
VERSION="0.1.0-pilot.1"
PREFIX=f"othryss-{VERSION}"
MODULES="__init__ account_risk alert_channels alerts alerts_cli bot_state collector collector_cli credentials execution fixture history history_cli history_reader incidents kalshi kalshi_client maintenance markouts onboarding ops order_detail reconciliation reference reference_cli replay risk_cli server source_health storage telemetry".split()
WEB="account-risk.js alerts.js app.js execution.js favicon.svg history.js incidents.js index.html markouts.js order-detail.js reference.js styles.css telemetry.js".split()
DOCS="account-risk-overview alert-delivery bot-state-incidents collector event-contract-draft execution-analysis history-import markouts onboarding operations order-investigation pilot-bot-integration reconciliation reference-prices source-health-recovery".split()


def sources():
    result={f"othryss/{name}.py":f"othryss/{name}.py" for name in MODULES}
    result.update({f"web/{name}":f"web/{name}" for name in WEB})
    result.update({f"docs/{name}.md":f"docs/{name}.md" for name in DOCS})
    result.update({name:name for name in (
        "LICENSE","local.env.example","alerts.env.example","ops.local.example.json","alerts.local.example.json",
        "requirements-import.txt","requirements-reference.txt","scripts/install_ops_task.ps1",
        "integrations/lip/prepare_patch.py","integrations/lip/othryss_telemetry.py")})
    result.update({"README.md":"pilot/START-HERE.md","FEEDBACK.md":"pilot/FEEDBACK.md",
                   "requirements-pilot.txt":"pilot/requirements-pilot.txt",
                   "docs/telemetry.md":"docs/pilot-bot-integration.md",
                   "fixtures/incentives/partial-fill-and-exit.json":"fixtures/pilot/partial-fill-and-exit.json"})
    return result


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_content(name,data):
    if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",data):
        raise ValueError(f"Private key material in release input: {name}")
    if re.search(rb"C:[/\\]Users[/\\]",data,re.I):
        raise ValueError(f"Developer home path in release input: {name}")


def build(root,output):
    root=Path(root).resolve();output=Path(output).resolve()
    if output.exists() or output.with_suffix(output.suffix+".sha256").exists():
        raise ValueError("Release output already exists; use a new directory")
    payload={}
    for name,relative in sources().items():
        path=root/relative
        if path.is_symlink() or not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in path.parents if p!=root and p.is_relative_to(root)):
            raise ValueError(f"Release input must be a regular workspace file: {relative}")
        data=path.read_bytes();check_content(name,data);payload[name]=data
    manifest={"version":VERSION,"format":1,"example":"synthetic",
              "files":{name:{"sha256":digest(data),"bytes":len(data)} for name,data in sorted(payload.items())}}
    payload["RELEASE.json"]=(json.dumps(manifest,indent=2,sort_keys=True)+"\n").encode()
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,"x",compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,data in sorted(payload.items()):
            info=zipfile.ZipInfo(f"{PREFIX}/{name}",date_time=(2026,9,13,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=(stat.S_IFREG|0o644)<<16
            archive.writestr(info,data)
    verify(output)
    sha=digest(output.read_bytes())
    output.with_suffix(output.suffix+".sha256").write_text(f"{sha}  {output.name}\n",encoding="ascii")
    return {"archive":str(output),"sha256":sha,"files":len(manifest["files"]),"bytes":output.stat().st_size}


def verify(path):
    expected={f"{PREFIX}/{name}" for name in sources()}|{f"{PREFIX}/RELEASE.json"}
    with zipfile.ZipFile(path) as archive:
        entries=archive.infolist()
        if len(entries)!=len(expected) or {entry.filename for entry in entries}!=expected:
            raise ValueError("Release members do not match the allowlist")
        if sum(entry.file_size for entry in entries)>50*1024*1024:
            raise ValueError("Release exceeds size limit")
        for entry in entries:
            if stat.S_ISLNK(entry.external_attr>>16) or ".." in PurePosixPath(entry.filename).parts:
                raise ValueError("Unsafe release member")
        manifest=json.loads(archive.read(f"{PREFIX}/RELEASE.json"))
        if manifest.get("format")!=1 or manifest.get("version")!=VERSION or manifest.get("example")!="synthetic" or set(manifest.get("files",{}))!=set(sources()):
            raise ValueError("Invalid release manifest")
        for name,record in manifest["files"].items():
            data=archive.read(f"{PREFIX}/{name}");check_content(name,data)
            if record!={"sha256":digest(data),"bytes":len(data)}:
                raise ValueError(f"Release checksum mismatch: {name}")
        return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/f"artifacts/releases/{PREFIX}.zip")
    parser.add_argument("--verify",type=Path)
    args=parser.parse_args()
    if args.verify:
        manifest=verify(args.verify);print(json.dumps({"verified":str(args.verify),"version":manifest["version"],"files":len(manifest["files"])}))
    else: print(json.dumps(build(ROOT,args.output)))


if __name__=="__main__":
    main()
