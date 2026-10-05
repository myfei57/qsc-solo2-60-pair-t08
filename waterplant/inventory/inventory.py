"""Chemical inventory ledger with explicit receiving and dosing batches.

The warehouse rule is fixed: stock is consumed by first-expiry-first-out
(``FEFO``).  When two usable batches expire on the same day, the batch received
earlier is consumed first (FIFO tie-breaker).  Legacy lots without an expiry
date remain after dated lots and are also surfaced as data-quality alerts.

Receipts and consumption use caller supplied idempotency keys.  A receipt may
contain many lots but is a warehouse all-or-nothing document: validation and
lot checks happen before stock is added.  A crash after its pending marker was
stored is repaired by ``recover_receipts``; the same document can be retried
safely because pending receipts are completed rather than booked twice.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from waterplant.audit import AUDIT_KEY, Auditor, Entry
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .exceptions import InsufficientInventory, InventoryConflict
from .report import (
    DEFAULT_EXPIRY_WARNING_DAYS,
    InventoryState,
    validate_identifier,
    validate_positive_quantity,
    validate_quantity,
    validate_warning_days,
)

LOTS_KEY = "inventory:lots"
REORDER_KEY = "inventory:reorder-level"
WARNING_DAYS_KEY = "inventory:expiry-warning-days"
RECEIPTS_KEY = "inventory:receipts"
MOVEMENTS_KEY = "inventory:movements"
IDEMPOTENCY_KEY = "inventory:idempotency"

DEFAULT_REORDER_LEVEL = 25.0
EPSILON = 1e-9
STRATEGY = "FEFO_EXPIRY_FIRST__FIFO_TIEBREAK"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format") from exc


def _load_json_list(data: dict[str, str], key: str) -> list[object]:
    raw = data.get(key)
    if raw is None:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


@dataclass
class Lot:
    """One received batch of a chemical."""

    chemical: str
    lot_id: str
    received: float
    consumed: float = 0.0
    received_on: str = ""
    expires_on: str = ""
    status: str = "active"
    received_seq: int = 0
    received_at: str = ""
    strategy: str = "fefo"
    document_id: str = ""

    def remaining(self) -> float:
        if self.status == "disposed":
            return 0.0
        return max(0.0, self.received - self.consumed)

    def is_expired(self, as_of: date | None = None) -> bool:
        if not self.expires_on or self.remaining() <= EPSILON or self.status == "disposed":
            return False
        return _parse_date(self.expires_on, "expires_on") < (as_of or _today())

    def days_to_expiry(self, as_of: date | None = None) -> int | None:
        if not self.expires_on:
            return None
        return (_parse_date(self.expires_on, "expires_on") - (as_of or _today())).days

    def as_dict(self, as_of: date | None = None, warning_days: int = DEFAULT_EXPIRY_WARNING_DAYS) -> dict[str, object]:
        days = self.days_to_expiry(as_of)
        expired = self.is_expired(as_of)
        remaining = self.remaining()
        if self.status == "disposed":
            expiry_state = "disposed"
        elif expired:
            expiry_state = "expired"
        elif remaining <= EPSILON:
            expiry_state = "depleted"
        elif days is not None and remaining > EPSILON:
            expiry_state = "near" if days <= warning_days else "fresh"
        elif not self.expires_on:
            expiry_state = "missing_expiry"
        else:
            expiry_state = "fresh"
        return {
            "chemical": self.chemical,
            "lot_id": self.lot_id,
            "received": self.received,
            "consumed": self.consumed,
            "remaining": remaining,
            "available": 0.0 if expired or self.status == "disposed" else remaining,
            "received_on": self.received_on,
            "received_at": self.received_at,
            "expires_on": self.expires_on,
            "days_to_expiry": days,
            "expiry_state": expiry_state,
            "status": self.status,
            "received_seq": self.received_seq,
            "allocation_strategy": self.strategy,
            "document_id": self.document_id,
        }


@dataclass(frozen=True)
class Allocation:
    """The amount of one lot used by a dosing event."""

    lot_id: str
    amount: float
    expires_on: str

    def as_dict(self) -> dict[str, object]:
        return {"lot_id": self.lot_id, "amount": self.amount, "expires_on": self.expires_on}


class Inventory:
    """Receives, allocates and reports chemical batch stock."""

    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _load_lots(self) -> list[Lot]:
        raw, _ = self._store.get(LOTS_KEY)
        if not raw:
            return []
        return self._lots_from_raw(raw)

    @staticmethod
    def _lots_from_raw(raw: str) -> list[Lot]:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        lots: list[Lot] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            chemical = str(item.get("chemical", ""))
            lot_id = str(item.get("lot_id", ""))
            received_on = str(item.get("received_on", ""))
            expires_on = str(item.get("expires_on", ""))
            legacy = bool(received_on or expires_on)
            lots.append(
                Lot(
                    chemical=chemical,
                    lot_id=lot_id,
                    received=float(item.get("received", 0.0)),
                    consumed=float(item.get("consumed", 0.0)),
                    received_on=received_on,
                    expires_on=expires_on,
                    status=str(item.get("status", "active")),
                    received_seq=int(item.get("received_seq", 0)),
                    received_at=str(item.get("received_at", "")),
                    strategy=str(item.get("allocation_strategy", "fefo" if legacy else "fifo-legacy")),
                    document_id=str(item.get("document_id", "")),
                )
            )
        return Inventory._sort_lots(lots)

    @staticmethod
    def _sort_lots(lots: list[Lot], as_of: date | None = None) -> list[Lot]:
        """Return stock order: usable FEFO, same expiry FIFO, legacy lots last."""

        current = as_of or _today()

        def key(lot: Lot) -> tuple[int, date, int, str]:
            if lot.expires_on:
                expiry = _parse_date(lot.expires_on, "expires_on")
                expired = expiry < current
                return (2 if expired else 0, expiry, lot.received_seq, lot.lot_id)
            return (1, date.max, lot.received_seq, lot.lot_id)

        return sorted(lots, key=key)

    @staticmethod
    def _list_from_data(data: dict[str, str], key: str) -> list[object]:
        return _load_json_list(data, key)

    @staticmethod
    def _save_to_data(data: dict[str, str], key: str, value: object) -> None:
        data[key] = json.dumps(value, ensure_ascii=False)

    def _audit_entries(self, data: dict[str, str]) -> list[dict[str, object]]:
        raw_items = self._list_from_data(data, AUDIT_KEY)
        entries: list[dict[str, object]] = []
        for item in raw_items:
            if isinstance(item, dict):
                entries.append(item)
            else:
                try:
                    parsed = json.loads(str(item))
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    entries.append(parsed)
        return entries

    def _append_audit(self, data: dict[str, str], entry: Entry) -> None:
        entries = self._audit_entries(data)
        entries.append(entry.as_dict())
        self._save_to_data(data, AUDIT_KEY, entries)

    def _append_movement(self, data: dict[str, str], movement: dict[str, object]) -> None:
        movements = self._list_from_data(data, MOVEMENTS_KEY)
        movements.append(movement)
        self._save_to_data(data, MOVEMENTS_KEY, movements)

    # ------------------------------------------------------------------
    # Configuration and read models
    # ------------------------------------------------------------------

    def reorder_level(self) -> float:
        value, present = load_float(self._store, REORDER_KEY)
        return value if present else DEFAULT_REORDER_LEVEL

    def set_reorder_level(self, value: float) -> float:
        validate_quantity(value, "level")
        save_float(self._store, REORDER_KEY, value)
        return value

    def expiry_warning_days(self) -> int:
        raw, present = self._store.get(WARNING_DAYS_KEY)
        if not present:
            return DEFAULT_EXPIRY_WARNING_DAYS
        try:
            return int(float(raw))
        except ValueError:
            return DEFAULT_EXPIRY_WARNING_DAYS

    def set_expiry_warning_days(self, value: int) -> int:
        validate_warning_days(value)
        self._store.put(WARNING_DAYS_KEY, str(value))
        return value

    def lots(self, chemical: str | None = None, as_of: date | None = None) -> list[Lot]:
        lots = self._sort_lots(self._load_lots(), as_of)
        if chemical is None:
            return lots
        return [lot for lot in lots if lot.chemical == chemical]

    def chemicals(self) -> list[str]:
        names: list[str] = []
        for lot in self._load_lots():
            if lot.chemical not in names:
                names.append(lot.chemical)
        return names

    def balance(self, chemical: str | None = None) -> float:
        return sum(lot.remaining() for lot in self.lots(chemical))

    def available_balance(self, chemical: str | None = None, as_of: date | None = None) -> float:
        return sum(
            lot.remaining()
            for lot in self.lots(chemical, as_of)
            if not lot.is_expired(as_of) and lot.status == "active"
        )

    def needs_reorder(self, as_of: date | None = None) -> list[str]:
        level = self.reorder_level()
        return [
            name
            for name in self.chemicals()
            if self.available_balance(name, as_of) < level
        ]

    def alerts(self, as_of: date | None = None) -> list[dict[str, object]]:
        current = as_of or _today()
        warning_days = self.expiry_warning_days()
        alerts: list[dict[str, object]] = []
        for chemical in self.chemicals():
            available = self.available_balance(chemical, current)
            if available < self.reorder_level():
                alerts.append(
                    {
                        "severity": "warning",
                        "type": "low_stock",
                        "chemical": chemical,
                        "message": (
                            f"{chemical} available={available:.4f} below reorder "
                            f"level={self.reorder_level():.4f}"
                        ),
                    }
                )
            for lot in self.lots(chemical, current):
                remaining = lot.remaining()
                if remaining <= EPSILON or lot.status == "disposed":
                    continue
                if lot.is_expired(current):
                    alerts.append(
                        {
                            "severity": "critical",
                            "type": "expired",
                            "chemical": chemical,
                            "lot_id": lot.lot_id,
                            "remaining": remaining,
                            "message": f"{chemical} lot {lot.lot_id} expired on {lot.expires_on}",
                        }
                    )
                    continue
                days = lot.days_to_expiry(current)
                if not lot.expires_on:
                    alerts.append(
                        {
                            "severity": "warning",
                            "type": "missing_expiry",
                            "chemical": chemical,
                            "lot_id": lot.lot_id,
                            "remaining": remaining,
                            "message": f"{chemical} lot {lot.lot_id} has no expiry date",
                        }
                    )
                elif days is not None and days <= warning_days:
                    message = (
                        f"{chemical} lot {lot.lot_id} expires today"
                        if days == 0
                        else f"{chemical} lot {lot.lot_id} expires in {days} days"
                    )
                    alerts.append(
                        {
                            "severity": "warning",
                            "type": "near_expiry",
                            "chemical": chemical,
                            "lot_id": lot.lot_id,
                            "remaining": remaining,
                            "days_to_expiry": days,
                            "message": message,
                        }
                    )
        for receipt in self.receipts():
            if receipt["status"] in {"pending", "conflict"}:
                alerts.append(
                    {
                        "severity": "warning" if receipt["status"] != "conflict" else "critical",
                        "type": f"receipt_{receipt['status']}",
                        "document_id": receipt["document_id"],
                        "message": f"receipt {receipt['document_id']} is {receipt['status']}",
                    }
                )
        return alerts

    def state(self, as_of: date | None = None) -> InventoryState:
        current = as_of or _today()
        warning_days = self.expiry_warning_days()
        return InventoryState(
            reorder_level=self.reorder_level(),
            balance=self.balance(),
            available_balance=self.available_balance(as_of=current),
            needs_reorder=self.needs_reorder(current),
            lots=[lot.as_dict(current, warning_days) for lot in self.lots(as_of=current)],
            alerts=self.alerts(current),
            strategy=STRATEGY,
            expiry_warning_days=self.expiry_warning_days(),
            receipts=self.receipts(),
            movements=self.movements(),
        )

    def describe(self) -> str:
        state = self.state()
        return (
            f"inventory balance={state.balance:.4f} available={state.available_balance:.4f} "
            f"reorder={state.reorder_level:.4f} alerts={len(state.alerts)}"
        )

    # ------------------------------------------------------------------
    # Legacy single-lot receive
    # ------------------------------------------------------------------

    def receive(
        self,
        chemical: str,
        lot_id: str,
        amount: float,
        expires_on: str = "",
        received_on: str | None = None,
        document_id: str = "",
        as_of: date | None = None,
        idempotency_key: str = "",
    ) -> Lot:
        """Book one lot; used by the simple endpoint and migration callers."""

        validate_identifier(chemical, "chemical")
        validate_identifier(lot_id, "lot_id")
        validate_positive_quantity(amount)
        current = as_of or _today()
        received_date = current.isoformat() if received_on is None else received_on
        parsed_received = _parse_date(received_date, "received_on")
        strategy = "fefo" if expires_on else "fifo-legacy"
        if expires_on:
            parsed_expiry = _parse_date(expires_on, "expires_on")
            if parsed_expiry <= parsed_received:
                raise ValueError("expires_on must be later than received_on")
        if parsed_received > current:
            raise ValueError("received_on cannot be in the future")

        doc_id = document_id or idempotency_key or f"legacy-{chemical}-{lot_id}"

        def mutate(data: dict[str, str]) -> None:
            existing_doc = self._find_receipt_data(data, doc_id)
            if existing_doc:
                fingerprint = self._fingerprint(
                    [
                        {
                            "chemical": chemical,
                            "lot_id": lot_id,
                            "amount": amount,
                            "expires_on": expires_on,
                            "received_on": received_date,
                        }
                    ]
                )
                if existing_doc.get("fingerprint") != fingerprint:
                    raise InventoryConflict(f"document {doc_id} was already submitted with different data")
                return

            lots = self._lots_from_raw(data.get(LOTS_KEY, "[]"))
            if any(lot.chemical == chemical and lot.lot_id == lot_id for lot in lots):
                raise InventoryConflict(f"lot {lot_id} for {chemical} already exists")
            seq = max((lot.received_seq for lot in lots), default=0) + 1
            lot = Lot(
                chemical=chemical,
                lot_id=lot_id,
                received=amount,
                consumed=0.0,
                received_on=received_date,
                expires_on=expires_on,
                received_seq=seq,
                received_at=_utc_now(),
                strategy=strategy,
                document_id=doc_id,
            )
            lots.append(lot)
            receipt = {
                "document_id": doc_id,
                "status": "received",
                "items": [
                    {
                        "chemical": chemical,
                        "lot_id": lot_id,
                        "amount": amount,
                        "expires_on": expires_on,
                        "received_on": received_date,
                    }
                ],
                "fingerprint": self._fingerprint(
                    [
                        {
                            "chemical": chemical,
                            "lot_id": lot_id,
                            "amount": amount,
                            "expires_on": expires_on,
                            "received_on": received_date,
                        }
                    ]
                ),
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
            }
            receipts = self._list_from_data(data, RECEIPTS_KEY)
            receipts.append(receipt)
            self._append_movement(
                data,
                {
                    "id": uuid.uuid4().hex,
                    "time": _utc_now(),
                    "type": "receive",
                    "document_id": doc_id,
                    "chemical": chemical,
                    "lot_id": lot_id,
                    "amount": amount,
                },
            )
            self._save_to_data(
                data,
                LOTS_KEY,
                [
                    lot.as_dict(current, self.expiry_warning_days())
                    for lot in self._sort_lots(lots, current)
                ],
            )
            self._save_to_data(data, RECEIPTS_KEY, receipts)

        self._store.atomic(mutate)
        return self._get_lot(chemical, lot_id)

    def _get_lot(self, chemical: str, lot_id: str) -> Lot:
        for lot in self._load_lots():
            if lot.chemical == chemical and lot.lot_id == lot_id:
                return lot
        raise ValueError(f"lot {lot_id} for {chemical} not found")

    # ------------------------------------------------------------------
    # Multi-lot inbound receipts with pending recovery and conflict review
    # ------------------------------------------------------------------

    def submit_receipt(
        self,
        document_id: str,
        items: list[dict[str, object]],
        as_of: date | None = None,
    ) -> dict[str, object]:
        """Submit a warehouse receiving document idempotently.

        The pending marker is committed before stock is touched.  A crash/resend
        after that marker is therefore a recovery case rather than a second
        receipt.
        """

        current = as_of or _today()
        validate_identifier(document_id, "document_id")
        normalized = self._normalize_items(items, current)
        fingerprint = self._fingerprint(normalized)

        def begin(data: dict[str, str]) -> tuple[str, dict[str, object] | None]:
            receipt = self._find_receipt_data(data, document_id)
            if receipt:
                status = str(receipt.get("status"))
                if status == "discarded":
                    raise InventoryConflict(
                        f"document {document_id} was discarded; issue a new document id"
                    )
                if receipt.get("fingerprint") != fingerprint:
                    receipt["status"] = "conflict"
                    receipt["conflict"] = {
                        "received_fingerprint": fingerprint,
                        "at": _utc_now(),
                        "warehouse_action": "hold_for_manual_confirmation",
                    }
                    receipt["updated_at"] = _utc_now()
                    self._replace_receipt(data, receipt)
                    self._append_movement(
                        data,
                        {
                            "id": uuid.uuid4().hex,
                            "time": _utc_now(),
                            "type": "receipt_conflict",
                            "document_id": document_id,
                            "amount": sum(float(item["amount"]) for item in normalized),
                        },
                    )
                    return "conflict", receipt
                if status == "conflict":
                    return "conflict", receipt
                if status == "pending":
                    return "pending", receipt
                return "received", receipt

            receipt = {
                "document_id": document_id,
                "status": "pending",
                "items": normalized,
                "fingerprint": fingerprint,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
            }
            receipts = self._list_from_data(data, RECEIPTS_KEY)
            receipts.append(receipt)
            self._save_to_data(data, RECEIPTS_KEY, receipts)
            return "pending", receipt

        phase, receipt = self._store.atomic(begin)
        if phase in {"conflict", "received"}:
            return receipt or {}

        def complete(data: dict[str, str]) -> dict[str, object]:
            self._complete_receipt(data, document_id, normalized, current)
            completed = self._find_receipt_data(data, document_id)
            if completed is None:
                raise ValueError(f"pending receipt {document_id} not found")
            return completed

        return self._store.atomic(complete)

    def _normalize_items(self, items: list[dict[str, object]], current: date) -> list[dict[str, object]]:
        if not isinstance(items, list) or not items:
            raise ValueError("items must contain at least one lot")
        normalized: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("each receipt item must be an object")
            chemical = str(item.get("chemical", "")).strip()
            lot_id = str(item.get("lot_id", "")).strip()
            validate_identifier(chemical, "chemical")
            validate_identifier(lot_id, "lot_id")
            amount = float(item.get("amount", 0.0))
            validate_positive_quantity(amount)
            received_on = str(item.get("received_on") or current.isoformat())
            parsed_received = _parse_date(received_on, "received_on")
            if parsed_received > current:
                raise ValueError("received_on cannot be in the future")
            expires_on = str(item.get("expires_on", "")).strip()
            validate_identifier(expires_on, "expires_on")
            parsed_expiry = _parse_date(expires_on, "expires_on")
            if parsed_expiry <= parsed_received:
                raise ValueError("expires_on must be later than received_on")
            key = (chemical, lot_id)
            if key in seen:
                raise ValueError(f"duplicate lot {lot_id} for {chemical} in receipt")
            seen.add(key)
            normalized.append(
                {
                    "chemical": chemical,
                    "lot_id": lot_id,
                    "amount": amount,
                    "received_on": received_on,
                    "expires_on": expires_on,
                }
            )
        return normalized

    @staticmethod
    def _fingerprint(items: list[dict[str, object]]) -> str:
        return json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _find_receipt_data(self, data: dict[str, str], document_id: str) -> dict[str, object] | None:
        for receipt in self._list_from_data(data, RECEIPTS_KEY):
            if isinstance(receipt, dict) and receipt.get("document_id") == document_id:
                return receipt
        return None

    def _replace_receipt(self, data: dict[str, str], updated: dict[str, object]) -> None:
        receipts = self._list_from_data(data, RECEIPTS_KEY)
        replaced = [
            updated
            if isinstance(receipt, dict) and receipt.get("document_id") == updated.get("document_id")
            else receipt
            for receipt in receipts
        ]
        self._save_to_data(data, RECEIPTS_KEY, replaced)

    def _complete_receipt(
        self,
        data: dict[str, str],
        document_id: str,
        items: list[dict[str, object]],
        current: date,
    ) -> None:
        lots = self._lots_from_raw(data.get(LOTS_KEY, "[]"))
        existing = {(lot.chemical, lot.lot_id): lot.document_id for lot in lots}
        for item in items:
            key = (str(item["chemical"]), str(item["lot_id"]))
            owner = existing.get(key)
            if owner is not None and owner != document_id:
                raise InventoryConflict(
                    f"lot {item['lot_id']} for {item['chemical']} already exists"
                )
        seq = max((lot.received_seq for lot in lots), default=0)
        for item in items:
            key = (str(item["chemical"]), str(item["lot_id"]))
            if key in existing:
                # A crash after some but not all document lots were written
                # leaves the earlier lots in place; do not receive them twice.
                continue
            seq += 1
            lot = Lot(
                chemical=str(item["chemical"]),
                lot_id=str(item["lot_id"]),
                received=float(item["amount"]),
                received_on=str(item["received_on"]),
                expires_on=str(item["expires_on"]),
                received_seq=seq,
                received_at=_utc_now(),
                strategy="fefo",
                document_id=document_id,
            )
            lots.append(lot)
            self._append_movement(
                data,
                {
                    "id": uuid.uuid4().hex,
                    "time": _utc_now(),
                    "type": "receive",
                    "document_id": document_id,
                    "chemical": lot.chemical,
                    "lot_id": lot.lot_id,
                    "amount": lot.received,
                },
            )
        receipt = self._find_receipt_data(data, document_id)
        if receipt is None:
            raise ValueError(f"pending receipt {document_id} not found")
        receipt["status"] = "received"
        receipt["updated_at"] = _utc_now()
        receipt.pop("conflict", None)
        self._replace_receipt(data, receipt)
        self._save_to_data(
            data,
            LOTS_KEY,
            [
                lot.as_dict(current, self.expiry_warning_days())
                for lot in self._sort_lots(lots, current)
            ],
        )

    def recover_receipts(self, as_of: date | None = None) -> list[dict[str, object]]:
        """Complete receipts left pending by a failed or retried submission."""

        current = as_of or _today()

        def mutate(data: dict[str, str]) -> list[dict[str, object]]:
            recovered: list[dict[str, object]] = []
            pending = [
                receipt
                for receipt in self._list_from_data(data, RECEIPTS_KEY)
                if isinstance(receipt, dict) and receipt.get("status") == "pending"
            ]
            for receipt in pending:
                document_id = str(receipt["document_id"])
                items = receipt.get("items")
                if not isinstance(items, list):
                    normalized: list[dict[str, object]] | None = None
                else:
                    try:
                        normalized = self._normalize_items(items, current)
                    except ValueError as exc:
                        normalized = None
                        receipt["status"] = "conflict"
                        receipt["conflict"] = {
                            "error": str(exc),
                            "at": _utc_now(),
                            "warehouse_action": "hold_for_manual_confirmation",
                        }
                        receipt["updated_at"] = _utc_now()
                        self._replace_receipt(data, receipt)
                        recovered.append(receipt)
                if normalized is None:
                    continue

                lots_now = self._lots_from_raw(data.get(LOTS_KEY, "[]"))
                foreign_lot = next(
                    (
                        item
                        for item in normalized
                        if next(
                            (
                                lot.document_id
                                for lot in lots_now
                                if lot.chemical == str(item["chemical"])
                                and lot.lot_id == str(item["lot_id"])
                            ),
                            document_id,
                        )
                        != document_id
                    ),
                    None,
                )
                if foreign_lot is not None:
                    receipt["status"] = "conflict"
                    receipt["conflict"] = {
                        "error": (
                            f"lot {foreign_lot['lot_id']} for {foreign_lot['chemical']} "
                            "already belongs to another document"
                        ),
                        "at": _utc_now(),
                        "warehouse_action": "hold_for_manual_confirmation",
                    }
                    receipt["updated_at"] = _utc_now()
                    self._replace_receipt(data, receipt)
                    recovered.append(receipt)
                    continue
                if receipt.get("fingerprint") != self._fingerprint(normalized):
                    receipt["status"] = "conflict"
                    receipt["updated_at"] = _utc_now()
                    self._replace_receipt(data, receipt)
                    recovered.append(receipt)
                    continue
                try:
                    self._complete_receipt(data, document_id, normalized, current)
                except InventoryConflict as exc:
                    receipt["status"] = "conflict"
                    receipt["conflict"] = {
                        "error": str(exc),
                        "at": _utc_now(),
                        "warehouse_action": "hold_for_manual_confirmation",
                    }
                    receipt["updated_at"] = _utc_now()
                    self._replace_receipt(data, receipt)
                    recovered.append(receipt)
                    continue
                completed = self._find_receipt_data(data, document_id)
                if completed:
                    recovered.append(completed)
            return recovered

        return self._store.atomic(mutate)

    def discard_receipt(self, document_id: str, note: str = "manual warehouse confirmation") -> dict[str, object]:
        """Warehouse decision for a held duplicate/conflict: keep it unbooked."""

        validate_identifier(document_id, "document_id")

        def mutate(data: dict[str, str]) -> dict[str, object]:
            receipt = self._find_receipt_data(data, document_id)
            if not receipt:
                raise ValueError(f"receipt {document_id} not found")
            if receipt.get("status") not in {"conflict", "pending"}:
                raise InventoryConflict(f"receipt {document_id} is {receipt.get('status')} and cannot be discarded")
            receipt["status"] = "discarded"
            receipt["resolution"] = {"action": "discard", "note": note, "at": _utc_now()}
            receipt["updated_at"] = _utc_now()
            self._replace_receipt(data, receipt)
            self._append_movement(
                data,
                {
                    "id": uuid.uuid4().hex,
                    "time": _utc_now(),
                    "type": "receipt_discarded",
                    "document_id": document_id,
                    "amount": sum(float(item.get("amount", 0.0)) for item in receipt.get("items", [])),
                },
            )
            return receipt

        return self._store.atomic(mutate)

    def receipts(self) -> list[dict[str, object]]:
        raw, present = self._store.get(RECEIPTS_KEY)
        if not present:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    def receipt(self, document_id: str) -> dict[str, object] | None:
        for receipt in self.receipts():
            if receipt.get("document_id") == document_id:
                return receipt
        return None

    def movements(self) -> list[dict[str, object]]:
        raw, present = self._store.get(MOVEMENTS_KEY)
        if not present:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    # ------------------------------------------------------------------
    # FEFO consumption and dosing audit consistency
    # ------------------------------------------------------------------

    def consume_allocations(
        self,
        chemical: str,
        amount: float,
        request_id: str = "",
        as_of: date | None = None,
    ) -> list[Allocation]:
        """Consume usable stock under FEFO and write the dosing audit atomically."""

        validate_identifier(chemical, "chemical")
        validate_positive_quantity(amount)
        current = as_of or _today()
        request_id = request_id or f"auto-{uuid.uuid4().hex}"

        def mutate(data: dict[str, str]) -> list[Allocation]:
            cached = self._cached_result(data, request_id)
            if cached is not None:
                if cached.get("chemical") != chemical or abs(float(cached.get("amount", 0.0)) - amount) > EPSILON:
                    raise InventoryConflict(f"request {request_id} was already submitted with different data")
                return [
                    Allocation(
                        lot_id=str(item.get("lot_id", "")),
                        amount=float(item.get("amount", 0.0)),
                        expires_on=str(item.get("expires_on", "")),
                    )
                    for item in cached.get("allocations", [])
                    if isinstance(item, dict)
                ]

            lots = self._lots_from_raw(data.get(LOTS_KEY, "[]"))
            usable = [
                lot
                for lot in lots
                if lot.chemical == chemical
                and lot.status == "active"
                and not lot.is_expired(current)
                and lot.remaining() > EPSILON
            ]
            usable = self._sort_lots(usable, current)
            available = sum(lot.remaining() for lot in usable)
            if available + EPSILON < amount:
                expired = sum(
                    lot.remaining()
                    for lot in lots
                    if lot.chemical == chemical and lot.is_expired(current)
                )
                suffix = f"; {expired:.4f} is expired and blocked" if expired > EPSILON else ""
                raise InsufficientInventory(
                    f"insufficient usable {chemical} inventory: requested={amount:.4f} "
                    f"available={available:.4f}{suffix}"
                )

            outstanding = amount
            allocations: list[Allocation] = []
            for lot in usable:
                if outstanding <= EPSILON:
                    break
                taken = min(lot.remaining(), outstanding)
                lot.consumed += taken
                outstanding -= taken
                allocations.append(
                    Allocation(lot_id=lot.lot_id, amount=taken, expires_on=lot.expires_on)
                )
            if outstanding > EPSILON:
                raise InsufficientInventory(f"insufficient {chemical} inventory")

            audit_entry = Auditor.make_entry(
                chemical,
                f"{amount:.4f}",
                ref=request_id,
            )
            self._append_audit(data, audit_entry)
            for allocation in allocations:
                self._append_movement(
                    data,
                    {
                        "id": uuid.uuid4().hex,
                        "time": _utc_now(),
                        "type": "consume",
                        "request_id": request_id,
                        "audit_id": audit_entry.id,
                        "chemical": chemical,
                        "lot_id": allocation.lot_id,
                        "amount": allocation.amount,
                        "expires_on": allocation.expires_on,
                    },
                )
            self._save_to_data(
                data,
                LOTS_KEY,
                [
                    lot.as_dict(current, self.expiry_warning_days())
                    for lot in self._sort_lots(lots, current)
                ],
            )
            result = {
                "request_id": request_id,
                "chemical": chemical,
                "amount": amount,
                "audit_id": audit_entry.id,
                "allocations": [allocation.as_dict() for allocation in allocations],
            }
            idempotency = self._list_from_data(data, IDEMPOTENCY_KEY)
            idempotency.append(result)
            self._save_to_data(data, IDEMPOTENCY_KEY, idempotency)
            return allocations

        return self._store.atomic(mutate)

    def consume(
        self,
        chemical: str,
        amount: float,
        request_id: str = "",
        as_of: date | None = None,
    ) -> float:
        """Backward-compatible amount result for older single-key callers."""

        self.consume_allocations(chemical, amount, request_id=request_id, as_of=as_of)
        return amount

    @staticmethod
    def _cached_result(data: dict[str, str], request_id: str) -> dict[str, object] | None:
        for item in _load_json_list(data, IDEMPOTENCY_KEY):
            if isinstance(item, dict) and item.get("request_id") == request_id:
                return item
        return None

    def dispense(
        self,
        chemical: str,
        amount: float,
        request_id: str,
        as_of: date | None = None,
    ) -> dict[str, object]:
        """Public batch-dosing operation; request id is mandatory for callers."""

        validate_identifier(request_id, "request_id")
        allocations = self.consume_allocations(
            chemical, amount, request_id=request_id, as_of=as_of
        )
        cached = self.consumption_result(request_id)
        audit_id = ""
        if cached is not None:
            audit_id = str(cached.get("audit_id", ""))
        return {
            "request_id": request_id,
            "chemical": chemical,
            "amount": amount,
            "allocations": [allocation.as_dict() for allocation in allocations],
            "balance": self.available_balance(chemical, as_of),
            "audit_id": audit_id,
        }

    def consumption_result(self, request_id: str) -> dict[str, object] | None:
        raw, present = self._store.get(IDEMPOTENCY_KEY)
        if not present:
            return None
        for item in _load_json_list({IDEMPOTENCY_KEY: raw}, IDEMPOTENCY_KEY):
            if isinstance(item, dict) and item.get("request_id") == request_id:
                return item
        return None

    def sweep_expired(self, as_of: date | None = None) -> list[dict[str, object]]:
        """Record that currently expired lots are blocked from dosing."""

        current = as_of or _today()

        def mutate(data: dict[str, str]) -> list[dict[str, object]]:
            lots = self._lots_from_raw(data.get(LOTS_KEY, "[]"))
            expired: list[dict[str, object]] = []
            changed = False
            for lot in lots:
                if lot.status == "active" and lot.is_expired(current) and lot.remaining() > EPSILON:
                    lot.status = "expired"
                    changed = True
                    expired.append(lot.as_dict(current, self.expiry_warning_days()))
                    self._append_movement(
                        data,
                        {
                            "id": uuid.uuid4().hex,
                            "time": _utc_now(),
                            "type": "expire",
                            "chemical": lot.chemical,
                            "lot_id": lot.lot_id,
                            "amount": lot.remaining(),
                        },
                    )
            if changed:
                self._save_to_data(
                    data,
                    LOTS_KEY,
                    [
                        lot.as_dict(current, self.expiry_warning_days())
                        for lot in self._sort_lots(lots, current)
                    ],
                )
            return expired

        return self._store.atomic(mutate)

    def dispose_lot(self, chemical: str, lot_id: str, reason: str = "manual disposal") -> Lot:
        """Mark an unusable/expired lot disposed without rewriting its history."""

        validate_identifier(reason, "reason")

        def mutate(data: dict[str, str]) -> Lot:
            lots = self._lots_from_raw(data.get(LOTS_KEY, "[]"))
            target: Lot | None = None
            amount = 0.0
            for lot in lots:
                if lot.chemical == chemical and lot.lot_id == lot_id:
                    target = lot
                    amount = lot.remaining()
                    break
            if target is None:
                raise ValueError(f"lot {lot_id} for {chemical} not found")
            if target.status == "disposed":
                return target
            target.status = "disposed"
            self._append_movement(
                data,
                {
                    "id": uuid.uuid4().hex,
                    "time": _utc_now(),
                    "type": "dispose",
                    "chemical": chemical,
                    "lot_id": lot_id,
                    "amount": amount,
                    "reason": reason,
                },
            )
            self._save_to_data(
                data,
                LOTS_KEY,
                [
                    lot.as_dict(_today(), self.expiry_warning_days())
                    for lot in self._sort_lots(lots)
                ],
            )
            return target

        return self._store.atomic(mutate)
