"""Translate discovery-fixture metadata at the boundary, outside replay logic."""

import json
from pathlib import Path

from .replay import ReplayStore

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "fixtures/incentives/partial-fill-and-exit.json"


def build_explorer(path=DEFAULT_FIXTURE, replays=1):
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if fixture["fixture_version"] != "0.0.1":
        raise ValueError("Unsupported fixture version")
    if not 1 <= replays <= 100:
        raise ValueError("Replay count must be between 1 and 100")
    store = ReplayStore()
    for _ in range(replays):
        store.ingest(fixture["source_sha256"], fixture["events"])
    result = store.project()
    result["fixture"] = {
        "name": "Partial fill & exit", "source_filename": fixture["source_filename"],
        "source_sha256": fixture["source_sha256"], "coverage": fixture["coverage"],
        "identifiers": fixture["identifiers"], "replays": replays,
        "synthetic": fixture.get("synthetic", False),
        # This is preserved report metadata, not an independently reconciled position.
        "reported_final_position": fixture["expected"]["bot_reported_final_position"],
        "reported_final_at": fixture["expected"]["bot_reported_final_position_at"],
    }
    return result
