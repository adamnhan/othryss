"""Venue-independent replay of the initial normalized event contract."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid decimal value") from exc
    if not result.is_finite():
        raise ValueError("Decimal values must be finite")
    return result


def display(value):
    return format(value, "f")


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Event timestamps must include a timezone")
    return parsed


def event_time(record):
    # Snapshot source-update time can be absent; receipt remains an observation time.
    value = record.get("occurred_at") or record.get("received_at")
    if value is None:
        raise ValueError("Event has neither a source timestamp nor an observation timestamp")
    return timestamp(value)


class ReplayStore:
    """In-memory projection input. Scoped event and execution identities deduplicate."""

    def __init__(self):
        self.events = {}
        self.fills = {}
        self.attempts = 0
        self.duplicates = 0

    def ingest(self, namespace, records):
        # Validate a complete batch before committing it to this store.
        candidate = deepcopy(self)
        for record in records:
            candidate._ingest(namespace, record)
        self.__dict__.update(candidate.__dict__)

    def _ingest(self, namespace, record):
        event_time(record)
        kind, payload = record["type"], record["payload"]
        if kind not in {"ORDER_FILL", "BOT_STATE_OBSERVATION", "ORDER_OBSERVATION"}:
            raise ValueError(f"Unsupported event type: {kind}")
        key = (namespace, record["event_id"])
        if key in self.events:
            if self.events[key] != record:
                raise ValueError("Conflicting records share an event identity")
            self.attempts += 1
            self.duplicates += 1
            return
        if kind == "ORDER_FILL":
            if payload["exposure_direction"] not in {"increase_yes", "decrease_yes"}:
                raise ValueError("Unsupported exposure direction")
            if payload["price_basis"] != "yes_outcome":
                raise ValueError("Unsupported price basis")
            if number(payload["quantity"]) <= 0:
                raise ValueError("Fill quantity must be positive")
            if not Decimal(0) <= number(payload["price_usd"]) <= Decimal(1):
                raise ValueError("YES-outcome price must be between zero and one")
            number(payload["fee_usd"])
            fill_key = (namespace, record["venue"], record.get("account_id"),
                        payload.get("subaccount"), payload["fill_id"])
            # Same execution may be delivered under another transport event ID.
            identity = (timestamp(record["occurred_at"]), record["instrument_id"], payload)
            if fill_key in self.fills:
                if self.fills[fill_key] != identity:
                    raise ValueError("Conflicting records share a fill identity")
                self.attempts += 1
                self.duplicates += 1
                return
            self.fills[fill_key] = deepcopy(identity)
        elif kind == "ORDER_OBSERVATION":
            for field in ("initial_quantity", "filled_quantity", "remaining_quantity", "price_usd"):
                if payload.get(field) is not None:
                    number(payload[field])
        else:
            for field in ("signed_position", "reported_order_size", "order_price_usd"):
                if payload.get(field) is not None:
                    number(payload[field])
        self.events[key] = deepcopy(record)
        self.attempts += 1

    def project(self):
        entries = sorted(self.events.items(), key=lambda item: (
            event_time(item[1]), item[0]))
        totals = {key: Decimal(0) for key in ("volume", "net_quantity", "fees", "gross_cash_flow")}
        orders, points, records = {}, [], []
        fill_count = 0
        for (namespace, _), record in entries:
            record = deepcopy(record)
            record["namespace"] = namespace
            records.append(record)
            payload = record["payload"]
            order_id = payload.get("order_id")
            order = None
            if order_id:
                order_key = (namespace, record["venue"], record.get("account_id"),
                             record["instrument_id"], order_id)
                order = orders.setdefault(order_key, {
                    "order_id": order_id, "instrument_id": record["instrument_id"],
                    "namespace": namespace, "first_observed_at": None,
                    "first_reported_size": None, "last_reported_remaining": None,
                    "last_reported_at": None, "filled_quantity": Decimal(0),
                    "fees": Decimal(0), "liquidity": set(), "directions": set(),
                    "fill_count": 0, "exchange_terminal_state": "unknown",
                })
            if record["type"] == "ORDER_FILL":
                qty, price, fee = (number(payload[key]) for key in ("quantity", "price_usd", "fee_usd"))
                direction = 1 if payload["exposure_direction"] == "increase_yes" else -1
                totals["volume"] += qty
                totals["net_quantity"] += qty * direction
                totals["fees"] += fee
                totals["gross_cash_flow"] -= qty * price * direction
                fill_count += 1
                if order:
                    order["filled_quantity"] += qty
                    order["fees"] += fee
                    order["fill_count"] += 1
                    order["liquidity"].add(payload["liquidity"])
                    order["directions"].add(payload["exposure_direction"])
                points.append({"occurred_at": record["occurred_at"], "event_id": record["event_id"],
                               "series": "net_fills", "value": display(totals["net_quantity"])})
            elif record["type"] == "ORDER_OBSERVATION":
                if order:
                    order["last_exchange_observation"] = {
                        "status": payload.get("status"),
                        "initial_quantity": payload.get("initial_quantity"),
                        "filled_quantity": payload.get("filled_quantity"),
                        "remaining_quantity": payload.get("remaining_quantity"),
                        "source_updated_at": record.get("occurred_at"),
                        "received_at": record.get("received_at"),
                    }
                # Order-snapshot fill counters never create executions or inventory.
            else:
                if order:
                    if order["first_observed_at"] is None:
                        order["first_observed_at"] = record["occurred_at"]
                        order["first_reported_size"] = payload.get("reported_order_size")
                    order["last_reported_remaining"] = payload.get("reported_order_size")
                    order["last_reported_at"] = record["occurred_at"]
                if payload.get("signed_position") is not None:
                    points.append({"occurred_at": record["occurred_at"], "event_id": record["event_id"],
                                   "series": "bot_position", "value": payload["signed_position"]})
        totals["net_cash_flow"] = totals["gross_cash_flow"] - totals["fees"]
        formatted_orders = []
        for order in orders.values():
            for key in ("filled_quantity", "fees"):
                order[key] = display(order[key])
            for key in ("liquidity", "directions"):
                order[key] = sorted(order[key])
            formatted_orders.append(order)
        return {
            "totals": {key: display(value) for key, value in totals.items()},
            "fill_count": fill_count, "unique_events": len(records),
            "ingestion_attempts": self.attempts, "duplicates_skipped": self.duplicates,
            "orders": formatted_orders, "events": records, "position_points": points,
            "reconciliation": {"status": "unavailable", "confirmed_incidents": None,
                               "reason": "No independent exchange-position snapshots are available."},
        }
