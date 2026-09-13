"""Kalshi field translation. Analytics never consumes the venue response objects."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .replay import number, timestamp
from .storage import digest

ADAPTER_VERSION = "kalshi-history-1"
FIELDS = set("order_id client_order_id ticker market_ticker fill_id trade_id book_side outcome_side side action count_fp count yes_price_dollars yes_price no_price_dollars fee_cost is_taker created_time last_update_time ts subaccount_number exchange_index status initial_count_fp remaining_count_fp fill_count_fp initial_count remaining_count fill_count expiration_time".split())


def evidence_row(row):
    return {key: value for key, value in row.items() if key in FIELDS}


def fixed(value):
    text = format(number(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def instant(value):
    return timestamp(value).astimezone(timezone.utc).isoformat(timespec="microseconds")


def direction(row):
    book, outcome = row.get("book_side"), row.get("outcome_side")
    if book is not None and book not in ("bid", "ask"):
        raise ValueError("Unrecognized book direction")
    if outcome is not None and outcome not in ("yes", "no"):
        raise ValueError("Unrecognized outcome direction")
    if book and outcome and (book == "bid") != (outcome == "yes"):
        raise ValueError("Conflicting explicit direction fields")
    if book or outcome:
        yes = book == "bid" if book else outcome == "yes"
    else:
        if row.get("action") not in ("buy", "sell") or row.get("side") not in ("yes", "no"):
            raise ValueError("Direction is unavailable")
        yes = (row["action"] == "buy") == (row["side"] == "yes")
    return "increase_yes" if yes else "decrease_yes"


def quantity(row, field, legacy):
    value = row.get(field)
    if value is None:
        value = row.get(legacy)
    return None if value is None else fixed(value)


def normalize(row, kind, scope_id, account, environment):
    instrument = row.get("market_ticker") or row.get("ticker")
    if not isinstance(instrument, str) or not instrument or not row.get("order_id"):
        raise ValueError("Record lacks instrument or order identity")
    subaccount = row.get("subaccount_number")
    if subaccount is not None and (type(subaccount) is not int or subaccount < 0):
        raise ValueError("Invalid subaccount number")
    px = row.get("yes_price_dollars")
    if px is None and row.get("yes_price") is not None:
        px = number(row["yes_price"]) / 100
    if px is not None and not Decimal(0) <= number(px) <= Decimal(1):
        raise ValueError("YES price outside contract payout range")
    payload = {"order_id": row["order_id"], "subaccount": subaccount,
               "exposure_direction": direction(row), "price_usd": fixed(px) if px is not None else None,
               "price_basis": "yes_outcome", "quantity_unit": "contracts", "currency": "USD"}
    if kind == "fills":
        fill_id = row.get("fill_id") or row.get("trade_id")
        qty = quantity(row, "count_fp", "count")
        if not fill_id or qty is None or number(qty) <= 0 or px is None:
            raise ValueError("Fill lacks identity, positive quantity, or price")
        if row.get("fee_cost") is None:
            raise ValueError("Fill fee is unavailable; it cannot be treated as zero")
        at = row.get("created_time")
        if at is None and row.get("ts") is not None:
            at = (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(number(row["ts"]) * 1_000_000))).isoformat()
        if at is None:
            raise ValueError("Fill timestamp is unavailable")
        at = instant(at)
        taker = row.get("is_taker")
        if taker is not None and type(taker) is not bool:
            raise ValueError("Invalid liquidity classification")
        payload.update(fill_id=fill_id, quantity=qty, fee_usd=fixed(row["fee_cost"]),
                       liquidity="unknown" if taker is None else "taker" if taker else "maker")
        event_type = "ORDER_FILL"
        event_id = digest([scope_id, subaccount, event_type, fill_id])
    elif kind == "orders":
        created = instant(row["created_time"]) if row.get("created_time") else None
        updated = instant(row["last_update_time"]) if row.get("last_update_time") else None
        at = updated  # Unknown update time stays unknown; receipt is separate.
        payload.update(client_order_id=row.get("client_order_id"), status=row.get("status"),
                       created_at=created, updated_at=updated,
                       initial_quantity=quantity(row, "initial_count_fp", "initial_count"),
                       filled_quantity=quantity(row, "fill_count_fp", "fill_count"),
                       remaining_quantity=quantity(row, "remaining_count_fp", "remaining_count"))
        for field in ("initial_quantity", "filled_quantity", "remaining_quantity"):
            if payload[field] is not None and number(payload[field]) < 0:
                raise ValueError("Order quantities cannot be negative")
        event_type = "ORDER_OBSERVATION"
        event_id = digest([scope_id, subaccount, event_type, row["order_id"], payload])
    else:
        raise ValueError("Unsupported history record family")
    return {"event_id": event_id, "schema_version": "0.1.0", "venue": "kalshi",
            "environment": environment, "account_id": account, "strategy_id": None,
            "scope_id": scope_id, "type": event_type, "instrument_id": instrument,
            "occurred_at": at, "origin": "exchange_rest", "payload": payload}
