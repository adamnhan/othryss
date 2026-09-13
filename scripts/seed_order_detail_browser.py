"""Create an isolated order investigation acceptance dataset."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_order_detail import seed

path = Path(sys.argv[1])
refs = path.with_suffix(".quotes.sqlite")
if path.resolve().parent != (ROOT / "artifacts/browser").resolve() or path.exists() or refs.exists():
    raise ValueError("Expected new databases in browser artifacts")
scope, other = seed(path, refs)
print(json.dumps({"scope": scope, "other": other, "refs": str(refs)}))
