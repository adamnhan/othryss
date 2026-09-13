"""Paginated, resumable history traversal. No trading or live collection behavior."""

import json
from pathlib import Path
import hashlib

from .kalshi import ADAPTER_VERSION, evidence_row, instant, normalize
from .storage import utc_now

# Scan current tiers before archives so migrating records can appear in the latter.
STREAMS = ("/portfolio/orders", "/portfolio/fills", "/historical/orders", "/historical/fills")


def cutoff_values(client):
    response = client.request("/historical/cutoff")
    return {key: instant(response[key]) for key in ("orders_updated_ts", "trades_created_ts")}


def run_import(store, client, scope_id, account, *, ticker=None, page_size=100,
               max_pages=1000, resume=None, progress=None, collection=None):
    if not 1 <= page_size <= 1000 or max_pages < 1:
        raise ValueError("Page size must be 1–1000 and max-pages must be positive")
    if resume:
        info = store.import_info(resume)
        if info["scope_id"] != scope_id:
            raise ValueError("Resume account does not match this import")
        if info["status"] in {"traversed", "needs_rescan"}:
            return store.report(resume)
        config = json.loads(info["config_json"])
        if config.get("adapter_version") != ADAPTER_VERSION:
            raise ValueError("Adapter changed; start a new import rather than mixing normalizers")
        if config["environment"] != client.environment or config.get("credential_subaccount") != getattr(client, "credential_subaccount", None):
            raise ValueError("Resume credential environment or subaccount changed")
        ticker, page_size = config["ticker"], config["page_size"]
        before = json.loads(info["cutoff_before_json"])
        run_id = resume
    else:
        before = cutoff_values(client)
        config = {"ticker": ticker, "page_size": page_size, "account": account,
                  "environment": client.environment, "adapter_version": ADAPTER_VERSION,
                  "credential_subaccount": getattr(client, "credential_subaccount", None),
                  "window": "All available history exposed by the selected key and ticker; no date truncation"}
        if collection:
            config.update(collection)
        endpoints = STREAMS if config.get("mode") != "incremental" else STREAMS[:2]
        run_id = store.create_import(scope_id, config, before, endpoints)
    store.status(run_id, "running")
    if progress:
        progress({"run_id": run_id, "status": "running"})
    pages_this_call = 0
    try:
        endpoints = STREAMS if config.get("mode") != "incremental" else STREAMS[:2]
        for endpoint in endpoints:
            while True:
                checkpoint = store.checkpoint(run_id, endpoint)
                if checkpoint["done"]:
                    break
                if pages_this_call >= max_pages:
                    store.status(run_id, "paused", "Page budget reached; resume this run")
                    return store.report(run_id)
                params = {"limit": page_size}
                if config.get("mode") == "incremental" and endpoint == "/portfolio/fills":
                    params.update(min_ts=config["fill_min_ts"], max_ts=config["window_end"])
                if ticker:
                    params["ticker"] = ticker
                if checkpoint["cursor"]:
                    params["cursor"] = checkpoint["cursor"]
                payload = client.request(endpoint, params)
                received_at = utc_now()
                kind = endpoint.rsplit("/", 1)[1]
                if not isinstance(payload.get(kind), list) or not isinstance(payload.get("cursor"), str):
                    raise ValueError("Page lacks a record list or explicit pagination cursor; cannot assume completion")
                next_cursor = payload["cursor"]
                if next_cursor and (next_cursor == checkpoint["cursor"] or store.used_cursor(run_id, endpoint, next_cursor)):
                    raise ValueError("Pagination cursor cycle detected; checkpoint retained")
                rows = payload[kind]
                records, evidence = [], []
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("Malformed history row")
                    if ticker and (row.get("market_ticker") or row.get("ticker")) != ticker:
                        raise ValueError("API returned a record outside the requested ticker")
                    bound_subaccount = config.get("credential_subaccount")
                    if bound_subaccount is not None and row.get("subaccount_number") != bound_subaccount:
                        raise ValueError("Record is outside the credential's declared subaccount scope")
                    records.append(normalize(row, kind, scope_id, account, client.environment))
                    evidence.append(evidence_row(row))
                inserted, duplicates = store.commit_page(run_id, endpoint, checkpoint, records, evidence, next_cursor, received_at,
                                                        retain_duplicates=not (config.get("collector", False) or config.get("reconciliation_audit", False)))
                pages_this_call += 1
                if progress:
                    progress({"run_id": run_id, "stream": endpoint, "page": checkpoint["pages"] + 1,
                              "rows": len(records), "inserted": inserted, "duplicates": duplicates})
        after = cutoff_values(client)
        # A moving archive boundary can invalidate a traversal's coverage claim.
        store.status(run_id, "traversed" if after == before else "needs_rescan",
                     None if after == before else "Historical cutoff changed; start another import to rescan both tiers", after)
    except (Exception, KeyboardInterrupt) as exc:
        message = "Interrupted; resume from the last committed page" if isinstance(exc, KeyboardInterrupt) else str(exc)
        store.status(run_id, "failed", message)
        if progress:
            progress({"run_id": run_id, "status": "failed", "error": message})
        raise
    return store.report(run_id)


def import_saved_report(store, path, scope_id, account):
    """Ingest real saved LIP fills, without representing it as an API traversal."""
    raw = Path(path).read_bytes()
    report = json.loads(raw)
    if report.get("guardrails", {}).get("live") is not True or not isinstance(report.get("fills"), list):
        raise ValueError("Expected a LIP report explicitly marked live with a fills list")
    records = [normalize(row, "fills", scope_id, account, "saved-report") for row in report["fills"]]
    for record in records:
        record["origin"] = "exchange_rest_response_saved_by_bot"
    run_id = store.create_import(scope_id, {
        "source_filename": Path(path).name, "source_sha256": hashlib.sha256(raw).hexdigest(),
        "account": account, "environment": "saved-report",
        "adapter_version": ADAPTER_VERSION, "coverage": "Saved fills only; no independent API request or order snapshots",
    }, {}, ["saved/fills"])
    try:
        store.commit_page(run_id, "saved/fills", store.checkpoint(run_id, "saved/fills"), records,
                          [evidence_row(row) for row in report["fills"]], "", utc_now())
        store.status(run_id, "saved_only")
    except Exception as exc:
        store.status(run_id, "failed", str(exc))
        raise
    return store.report(run_id)
