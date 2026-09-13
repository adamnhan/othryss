"""Seed an isolated browser acceptance database with synthetic execution traces."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from test_execution import seed

path = Path(sys.argv[1])
if path.resolve().parent != (ROOT / "artifacts/browser").resolve() or path.exists():
    raise ValueError("Expected a new database in browser artifacts")
scope, other = seed(path)
print(json.dumps({"scope": scope, "other": other}))
