"""Isolated synthetic fill/reference evidence for browser acceptance."""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/"tests")]
from test_markouts import seed

parser=argparse.ArgumentParser()
parser.add_argument("--db",type=Path,required=True)
parser.add_argument("--reference-db",type=Path,required=True)
args=parser.parse_args()
for path in (args.db,args.reference_db):
    if path.resolve().parent!=(ROOT/"artifacts/browser").resolve():
        raise ValueError("Synthetic databases must stay in browser artifacts")
scope,other=seed(args.db,args.reference_db)
print(json.dumps({"scope":scope,"other":other}))
