"""Prepare a reviewable LIP hook without importing or modifying the bot repository."""
import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/integrations/lip"))
    parser.add_argument("--sdk-only", action="store_true", help="Upgrade only the SDK when the state hook is already installed")
    args = parser.parse_args()
    raw = args.source.read_bytes()
    source = raw.decode("utf-8")
    tree = ast.parse(source)
    probe = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Probe")
    init = next(n for n in probe.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    assignment = next(n for n in init.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "run_id" for t in n.targets))
    lines = source.splitlines(keepends=True)
    newline = "\r\n" if b"\r\n" in raw else "\n"
    hook = newline.join([
        "        # Optional Othryss telemetry: explicit environment opt-in, local spool only.",
        "        try:",
        "            from othryss_telemetry import attach_probe",
        "            attach_probe(self.k, ticker=self.ticker, run_id=self.run_id, probe=self)",
        "        except Exception:",
        "            pass", ""])
    old = "attach_probe(self.k, ticker=self.ticker, run_id=self.run_id)"
    if old in source:
        patched = source.replace(old, "attach_probe(self.k, ticker=self.ticker, run_id=self.run_id, probe=self)")
    elif "probe=self)" in source:
        if not args.sdk_only:
            raise ValueError("State hook already installed; use --sdk-only for an SDK upgrade")
        patched = source
    else:
        lines.insert(assignment.end_lineno, hook)
        patched = "".join(lines)
    compile(patched, "lip_requote_probe.py", "exec")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "lip_requote_probe.py").write_bytes(patched.encode())
    (args.output / "lip_requote_probe.patch").write_text("".join(difflib.unified_diff(source.splitlines(True), patched.splitlines(True), fromfile="a/scripts/lip_requote_probe.py", tofile="b/scripts/lip_requote_probe.py")), encoding="utf-8")
    sdk = Path(__file__).with_name("othryss_telemetry.py").read_bytes()
    (args.output / "othryss_telemetry.py").write_bytes(sdk)
    req = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "KX")
    req = next(n for n in req.body if isinstance(n, ast.FunctionDef) and n.name == "req")
    (args.output / "original_req.py").write_text(ast.unparse(req) + "\n", encoding="utf-8")
    manifest = {"source": str(args.source.resolve()), "source_sha256": hashlib.sha256(raw).hexdigest(),
                "patched_sha256": hashlib.sha256(patched.encode()).hexdigest(), "sdk_sha256": hashlib.sha256(sdk).hexdigest(),
                "activated": False, "hook": "Probe.__init__, immediately after run_id assignment"}
    prior_sdk = args.source.resolve().parents[1] / "othryss_telemetry.py"
    if prior_sdk.exists():
        manifest["previous_sdk_sha256"] = hashlib.sha256(prior_sdk.read_bytes()).hexdigest()
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
