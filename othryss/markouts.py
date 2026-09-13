"""Bounded, read-only fill markouts over retained reference observations."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

from .reference import decimal, price
from .replay import timestamp
from .storage import digest

HORIZONS = (1, 5, 30)
VERSION = "receipt-markout-1"
POLICY = {
    "version": VERSION, "horizons_seconds": list(HORIZONS), "max_quote_age_seconds": 1,
    "retrieval_version": "bounded-horizons-2", "max_quotes_per_horizon": 2000,
    "max_observation_gap_seconds": 20, "confirmation_wait_seconds": 20,
    "clock_basis": "exchange_fill_time_vs_local_quote_receipt", "fees_included": False,
    "formula": "exposure_sign * (future_yes_midpoint - fill_yes_price)",
    "interpretation": "Receipt-time estimates, not exact exchange-time prices or strategy P&L. Positive means the future midpoint is favorable relative to the execution price. Fees, incentives and settlements are excluded.",
    "selection": "Latest observation at or before each target, at most one second old. No interpolation, forward price selection, or fallback past an invalid observation. Continuous same-connection evidence must bracket the fill and target.",
}


def iso(at):
    return at.astimezone(timezone.utc).isoformat(timespec="microseconds")


def fixed(value):
    return "0" if value == 0 else format(value, "f")


def evaluate(fill, quotes, gaps, now, *, horizons=HORIZONS):
    """Pure calculation. Quotes must include the scoped interval and boundary records."""
    results=[]
    try:
        p=fill["payload"]
        when=timestamp(fill["occurred_at"])
        if fill["type"]!="ORDER_FILL" or p["price_basis"]!="yes_outcome" or p["currency"]!="USD" or p["quantity_unit"]!="contracts":
            raise ValueError("Unsupported fill economics")
        qty=decimal(p["quantity"]); execution=price(p["price_usd"])
        if qty<=0 or p["exposure_direction"] not in {"increase_yes","decrease_yes"}: raise ValueError("Invalid fill")
        sign=1 if p["exposure_direction"]=="increase_yes" else -1
    except (ValueError, KeyError, TypeError):
        return [{"horizon_seconds":h,"status":"unavailable","reason":"invalid_fill","per_contract_usd":None,"quantity_weighted_usd":None} for h in horizons]
    timed=sorted([(timestamp(q["received_at"]),q) for q in quotes],key=lambda item:(item[0],item[1]["quote_id"]))
    for horizon in horizons:
        target=when+timedelta(seconds=horizon)
        result={"horizon_seconds":horizon,"target_at":iso(target),"status":"unavailable","reason":None,
                "per_contract_usd":None,"quantity_weighted_usd":None,"reference":None}
        results.append(result)
        if now<target:
            result.update(status="pending",reason="horizon_not_elapsed"); continue
        before=[(at,q) for at,q in timed if at<=when]
        if not before or (when-before[-1][0]).total_seconds()>20:
            result["reason"]="capture_missing_at_fill"; continue
        start,anchor=before[-1]
        candidates=[(at,q) for at,q in timed if at<=target]
        at,reference=candidates[-1]
        result["reference"]=reference
        result["quote_age_seconds"]=fixed(Decimal(str((target-at).total_seconds())))
        if (target-at).total_seconds()>1:
            result["reason"]="quote_too_old"; continue
        if reference.get("source_at") and not 0<=(target-timestamp(reference["source_at"])).total_seconds()<=1:
            result["reason"]="timing_uncertain"; continue
        after=[(at,q) for at,q in timed if at>target]
        confirmation=after[0] if after and (after[0][0]-target).total_seconds()<=20 else None
        end=confirmation[0] if confirmation else target
        overlap=[g for g in gaps if timestamp(g["started_at"])<=end and (g["ended_at"] is None or timestamp(g["ended_at"])>start)]
        if overlap:
            result.update(reason="capture_gap",gaps=overlap); continue
        if confirmation is None:
            result.update(status="pending" if now<target+timedelta(seconds=20) else "unavailable",reason="awaiting_confirmation" if now<target+timedelta(seconds=20) else "confirmation_missing"); continue
        interval=[q for t,q in timed if start<=t<=end and q["quote_id"]>=anchor["quote_id"] and q["quote_id"]<=confirmation[1]["quote_id"]]
        failure=None if len(interval)>=2 and reference in interval and interval[0]==anchor and interval[-1]==confirmation[1] else "clock_discontinuity"
        previous=None
        for q in interval:
            try:
                if q["scope_id"]!=fill["scope_id"] or q["instrument_id"]!=fill["instrument_id"]:
                    failure="reference_scope_mismatch"; break
                if q["connection_id"]!=anchor["connection_id"] or q["sid"]!=anchor["sid"]:
                    failure="connection_changed"; break
                if q["quality"]!="valid" or q["schema_version"]!="reference-1" or q["price_basis"]!="yes_outcome" or q["currency"]!="USD" or q["quantity_unit"]!="contracts":
                    failure="invalid_reference"; break
                bid,ask,mid=price(q["bid"]),price(q["ask"]),price(q["midpoint"])
                if bid>=ask or mid!=(bid+ask)/2: raise ValueError("Invalid midpoint")
                if q.get("source_at") and not -1<=(timestamp(q["received_at"])-timestamp(q["source_at"])).total_seconds()<=2:
                    failure="timing_uncertain"; break
                if previous:
                    wall=(timestamp(q["received_at"])-timestamp(previous["received_at"])).total_seconds()
                    elapsed=(int(q["received_monotonic_ns"])-int(previous["received_monotonic_ns"]))/1e9
                    if wall>20:
                        failure="observation_gap"; break
                    if elapsed<0 or abs(elapsed-wall)>0.25:
                        failure="clock_discontinuity"; break
                    if q["sequence"]<=previous["sequence"]:
                        failure="sequence_reversal"; break
                previous=q
            except (ValueError,KeyError,TypeError):
                failure="invalid_reference"; break
        result["evidence"]={"anchor":anchor,"confirmation":confirmation[1],"interval_records":len(interval),"interval_digest":digest(interval)}
        if failure:
            result["reason"]=failure; continue
        with localcontext() as context:
            context.prec=80
            value=sign*(price(reference["midpoint"])-execution)
            weighted=value*qty
        result.update(status="estimate",reason=None,per_contract_usd=fixed(value),quantity_weighted_usd=fixed(weighted))
    return results


def fill_markouts(refs, fill, now):
    """Read each exact anchor-to-confirmation interval, retaining every observation.

    Busy periods outside that interval cannot exhaust a horizon's evidence budget.
    A long horizon hitting its cap does not invalidate shorter horizons.
    """
    if refs is None or not fill.get("occurred_at"):
        return evaluate(fill, [], [], now)
    at = timestamp(fill["occurred_at"])
    scope, instrument = fill["scope_id"], fill["instrument_id"]
    anchor = refs.execute("""SELECT quote_id,received_at FROM quotes WHERE scope_id=? AND instrument_id=?
        AND received_at>=? AND received_at<=? ORDER BY received_at DESC,quote_id DESC LIMIT 1""",
        (scope, instrument, iso(at-timedelta(seconds=20)), iso(at))).fetchone()
    if not anchor:
        return evaluate(fill, [], [], now)
    results = []
    for horizon in HORIZONS:
        target = at + timedelta(seconds=horizon)
        if now < target:
            results.extend(evaluate(fill, [], [], now, horizons=(horizon,)))
            continue
        confirmation = refs.execute("""SELECT quote_id,received_at FROM quotes WHERE scope_id=? AND instrument_id=?
            AND received_at>? AND received_at<=? ORDER BY received_at,quote_id LIMIT 1""",
            (scope, instrument, iso(target), iso(target+timedelta(seconds=20)))).fetchone()
        end = confirmation["received_at"] if confirmation else iso(target)
        end_id = confirmation["quote_id"] if confirmation else 9223372036854775807
        raw = refs.execute("""SELECT quote_id,record_json FROM quotes WHERE scope_id=? AND instrument_id=?
            AND (received_at,quote_id)>=(?,?) AND (received_at,quote_id)<=(?,?)
            ORDER BY received_at,quote_id LIMIT ?""",
            (scope, instrument, anchor["received_at"], anchor["quote_id"], end, end_id, POLICY["max_quotes_per_horizon"]+1)).fetchall()
        gaps = [dict(r) for r in refs.execute("""SELECT * FROM gaps WHERE scope_id=? AND instrument_id=?
            AND started_at<=? AND (ended_at IS NULL OR ended_at>?) ORDER BY gap_id LIMIT 101""",
            (scope, instrument, end, anchor["received_at"]))]
        if len(raw)>POLICY["max_quotes_per_horizon"] or len(gaps)>100:
            results.append({"horizon_seconds":horizon,"target_at":iso(target),"status":"unavailable","reason":"evidence_limit",
                            "per_contract_usd":None,"quantity_weighted_usd":None})
        else:
            quotes = [json.loads(r["record_json"]) | {"quote_id":r["quote_id"]} for r in raw]
            results.extend(evaluate(fill, quotes, gaps, now, horizons=(horizon,)))
    return results


def catalog(reader, reference_path, scope, instrument="", order="", limit=25, offset=0, now=None, *, reference_connection=None):
    reader.account(scope)
    if not 1<=limit<=50 or not 0<=offset<=100000 or len(instrument)>200 or len(order)>200:
        raise ValueError("Markout bounds: limit 1-50, offset 0-100000, filters at most 200 characters")
    now=now or datetime.now(timezone.utc)
    where="scope_id=? AND type='ORDER_FILL' AND (?='' OR instrument_id=?) AND (?='' OR order_id=?)"
    params=(scope,instrument,instrument,order,order)
    total=reader.db.execute("SELECT COUNT(*) FROM events WHERE "+where,params).fetchone()[0]
    fills=[json.loads(r[0]) for r in reader.db.execute("SELECT canonical_json FROM events WHERE "+where+" ORDER BY occurred_at DESC,event_id LIMIT ? OFFSET ?",params+(limit,offset))]
    refs=reference_connection
    owned=False
    rows=[]
    try:
        if refs is None and reference_path is not None and Path(reference_path).is_file():
            refs=sqlite3.connect(Path(reference_path).resolve().as_uri()+"?mode=ro",uri=True); refs.row_factory=sqlite3.Row
            owned=True
            refs.execute("PRAGMA query_only=ON"); refs.execute("BEGIN")
            if refs.execute("PRAGMA user_version").fetchone()[0]!=1: raise ValueError("Unsupported reference schema")
        for fill in fills:
            rows.append({"fill":fill,"markouts":fill_markouts(refs,fill,now)})
        summary=[]
        for h in HORIZONS:
            selected=[(row,next(m for m in row["markouts"] if m["horizon_seconds"]==h)) for row in rows]
            available=[(row,m) for row,m in selected if m["status"]=="estimate"]
            with localcontext() as context:
                context.prec=80
                qty=sum((decimal(row["fill"]["payload"]["quantity"]) for row,m in available),Decimal(0))
                weighted=sum((Decimal(m["quantity_weighted_usd"]) for row,m in available),Decimal(0))
                summary.append({"horizon_seconds":h,"estimated_fills":len(available),"pending_fills":sum(m["status"]=="pending" for _,m in selected),"unavailable_fills":sum(m["status"]=="unavailable" for _,m in selected),"eligible_quantity":fixed(qty),"quantity_weighted_sum_usd":fixed(weighted),"quantity_weighted_mean_usd":fixed((weighted/qty).quantize(Decimal("0.000001"))) if qty else None})
        return {"scope_id":scope,"computed_at":iso(now),"policy":POLICY,"rows":rows,"summary":summary,"total":total,"limit":limit,"offset":offset,
                "coverage":"Summaries cover only the displayed fill page and eligible quantities. Unavailable values are excluded, never replaced with zero. Results are recomputed from retained local evidence; exports preserve displayed fill and boundary quotes, not the entire intervening stream."}
    finally:
        if owned and refs is not None: refs.close()
