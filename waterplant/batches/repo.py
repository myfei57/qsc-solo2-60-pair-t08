"""JSON document repositories for the batch subsystem.

Each aggregate is one JSON list (or map) kept under a single store key so the
service layer can update several aggregates inside one ``store.transaction``
and rely on the single atomic file flush for crash safety.
"""

from __future__ import annotations

import json

from waterplant.store.store import Store

from .models import (
    Batch,
    ConsumptionOrder,
    InboundOrder,
    LedgerEntry,
    QuarantineEntry,
)

BATCHES_KEY = "batches:batches"
LEDGER_KEY = "batches:ledger"
INBOUND_KEY = "batches:inbound-orders"
CONSUMPTION_KEY = "batches:consumption-orders"
IDEMPOTENCY_KEY = "batches:idempotency"
QUARANTINE_KEY = "batches:quarantine"
SEQ_KEY = "batches:seq"
NEAR_EXPIRY_KEY = "batches:near-expiry-days"
DEFAULT_NEAR_EXPIRY_DAYS = 30


def _load_json_list(store: Store, key: str) -> list[object]:
    raw, present = store.get(key)
    if not present:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def _load_json_map(store: Store, key: str) -> dict[str, object]:
    raw, present = store.get(key)
    if not present:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class BatchRepository:
    """Batch stock rows plus the monotonic arrival sequence."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def load(self) -> list[Batch]:
        rows: list[Batch] = []
        for item in _load_json_list(self._store, BATCHES_KEY):
            if isinstance(item, dict):
                rows.append(Batch.from_dict(item))
        return rows

    def save(self, batches: list[Batch]) -> None:
        self._store.put(BATCHES_KEY, _dump([batch.as_dict() for batch in batches]))

    def next_seq(self) -> int:
        """Reserve and return the next global batch arrival number."""

        raw, present = self._store.get(SEQ_KEY)
        seq = 1
        if present:
            try:
                seq = int(raw) + 1
            except ValueError:
                seq = 1
        self._store.put(SEQ_KEY, str(seq))
        return seq

    def near_expiry_days(self) -> int:
        raw, present = self._store.get(NEAR_EXPIRY_KEY)
        if not present:
            return DEFAULT_NEAR_EXPIRY_DAYS
        try:
            return int(raw)
        except ValueError:
            return DEFAULT_NEAR_EXPIRY_DAYS

    def set_near_expiry_days(self, days: int) -> int:
        self._store.put(NEAR_EXPIRY_KEY, str(days))
        return days


class LedgerRepository:
    """Append only batch movements."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def load(self) -> list[LedgerEntry]:
        entries: list[LedgerEntry] = []
        for item in _load_json_list(self._store, LEDGER_KEY):
            if isinstance(item, dict):
                rows = LedgerEntry.from_dict(item)
                if rows.id:
                    entries.append(rows)
        return entries

    def save(self, entries: list[LedgerEntry]) -> None:
        self._store.put(LEDGER_KEY, _dump([entry.as_dict() for entry in entries]))


class InboundRepository:
    """Inbound delivery notes keyed by order id."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def load(self) -> list[InboundOrder]:
        rows: list[InboundOrder] = []
        for item in _load_json_list(self._store, INBOUND_KEY):
            if isinstance(item, dict):
                order = InboundOrder.from_dict(item)
                if order.order_id:
                    rows.append(order)
        return rows

    def save(self, orders: list[InboundOrder]) -> None:
        self._store.put(INBOUND_KEY, _dump([order.as_dict() for order in orders]))


class ConsumptionRepository:
    """Applied dosing/withdraw orders keyed by order id."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def load(self) -> list[ConsumptionOrder]:
        rows: list[ConsumptionOrder] = []
        for item in _load_json_list(self._store, CONSUMPTION_KEY):
            if isinstance(item, dict):
                order = ConsumptionOrder.from_dict(item)
                if order.order_id:
                    rows.append(order)
        return rows

    def save(self, orders: list[ConsumptionOrder]) -> None:
        self._store.put(CONSUMPTION_KEY, _dump([order.as_dict() for order in orders]))


class IdempotencyRepository:
    """Stored first-time result for every client supplied request id."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def load(self) -> dict[str, dict[str, object]]:
        mapping = _load_json_map(self._store, IDEMPOTENCY_KEY)
        return {str(key): value for key, value in mapping.items() if isinstance(value, dict)}

    def save(self, records: dict[str, dict[str, object]]) -> None:
        self._store.put(IDEMPOTENCY_KEY, _dump(records))


class QuarantineRepository:
    """Conflicting resends awaiting a warehouse decision."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def load(self) -> list[QuarantineEntry]:
        rows: list[QuarantineEntry] = []
        for item in _load_json_list(self._store, QUARANTINE_KEY):
            if isinstance(item, dict):
                entry = QuarantineEntry.from_dict(item)
                if entry.quarantine_id:
                    rows.append(entry)
        return rows

    def save(self, entries: list[QuarantineEntry]) -> None:
        self._store.put(QUARANTINE_KEY, _dump([entry.as_dict() for entry in entries]))
