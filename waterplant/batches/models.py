"""Batch ledger data models.

Everything is a plain dataclass with explicit ``as_dict`` / ``from_dict``
serialisation because the backing store is a JSON document. The fields
capture the two independent timelines every batch carries:

* arrival order  -> ``seq`` / ``received_at`` (FIFO)
* shelf life     -> ``expiry_date``          (FEFO)

Consumption order is therefore fully deterministic, see
:func:`fefo_sort_key`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .clock import date_text, parse_date

#: Status a batch is physically observed in.
ACTIVE = "active"
EXPIRED = "expired"
EXHAUSTED = "exhausted"
SCRAPPED = "scrapped"

LIVE_STATUSES = (ACTIVE, EXPIRED)
SPENT_STATUSES = (EXHAUSTED, SCRAPPED)


def fefo_sort_key(batch: "Batch") -> tuple[str, int, str]:
    """Deterministic draw order: earliest expiry, then earliest arrival.

    * batches with an expiry date always sort before open dated stock;
    * equal expiry dates fall back to inbound sequence (strict FIFO);
    * batch id is the final tie breaker so the order never depends on dict
      iteration.
    """

    expiry = batch.expiry_date or "9999-12-31"
    return (expiry, batch.seq, batch.batch_id)


@dataclass
class Batch:
    """One shelf life tracked lot of a chemical."""

    chemical: str
    batch_id: str
    received: float
    expiry_date: str = ""
    consumed: float = 0.0
    scrapped: float = 0.0
    status: str = ACTIVE
    received_at: str = ""
    seq: int = 0
    inbound_id: str = ""
    supplier_ref: str = ""

    def remaining(self) -> float:
        return self.received - self.consumed - self.scrapped

    def is_expired(self, today: dt.date) -> bool:
        if not self.expiry_date:
            return False
        return parse_date(self.expiry_date) < today

    def days_to_expiry(self, today: dt.date) -> int | None:
        if not self.expiry_date:
            return None
        return (parse_date(self.expiry_date) - today).days

    def derived_status(self, today: dt.date) -> str:
        """Recompute status from the physical facts, honoring terminal states."""

        if self.status == SCRAPPED:
            return SCRAPPED
        if self.remaining() <= 1e-9:
            return EXHAUSTED
        if self.is_expired(today):
            return EXPIRED
        return ACTIVE

    def as_dict(self) -> dict[str, object]:
        return {
            "chemical": self.chemical,
            "batch_id": self.batch_id,
            "received": self.received,
            "expiry_date": self.expiry_date,
            "consumed": self.consumed,
            "scrapped": self.scrapped,
            "remaining": self.remaining(),
            "status": self.status,
            "received_at": self.received_at,
            "seq": self.seq,
            "inbound_id": self.inbound_id,
            "supplier_ref": self.supplier_ref,
        }

    def view(self, today: dt.date) -> dict[str, object]:
        """Serialise with the expiry status computed against ``today``."""

        data = self.as_dict()
        data["status"] = self.derived_status(today)
        days = self.days_to_expiry(today)
        data["days_to_expiry"] = days
        return data

    @classmethod
    def from_dict(cls, item: dict[str, object]) -> "Batch":
        return cls(
            chemical=str(item.get("chemical", "")),
            batch_id=str(item.get("batch_id", "")),
            received=float(item.get("received", 0.0)),
            expiry_date=str(item.get("expiry_date", "") or ""),
            consumed=float(item.get("consumed", 0.0)),
            scrapped=float(item.get("scrapped", 0.0)),
            status=str(item.get("status", ACTIVE)),
            received_at=str(item.get("received_at", "")),
            seq=int(item.get("seq", 0)),
            inbound_id=str(item.get("inbound_id", "")),
            supplier_ref=str(item.get("supplier_ref", "")),
        )


@dataclass
class LedgerEntry:
    """One append only batch ledger movement.

    ``quantity`` is always positive; ``movement`` names the direction. The
    entry carries the idempotency ``request_id`` and the business order id so
    a replayed request can be linked back to exactly one ledger movement.
    """

    id: str
    at: str
    movement: str
    chemical: str
    batch_id: str
    quantity: float
    order_id: str = ""
    request_id: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "at": self.at,
            "movement": self.movement,
            "chemical": self.chemical,
            "batch_id": self.batch_id,
            "quantity": self.quantity,
            "order_id": self.order_id,
            "request_id": self.request_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, item: dict[str, object]) -> "LedgerEntry":
        return cls(
            id=str(item.get("id", "")),
            at=str(item.get("at", "")),
            movement=str(item.get("movement", "")),
            chemical=str(item.get("chemical", "")),
            batch_id=str(item.get("batch_id", "")),
            quantity=float(item.get("quantity", 0.0)),
            order_id=str(item.get("order_id", "")),
            request_id=str(item.get("request_id", "")),
            note=str(item.get("note", "")),
        )


@dataclass
class InboundLine:
    """One line of an inbound delivery note."""

    chemical: str
    amount: float
    expiry_date: str = ""
    supplier_ref: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "chemical": self.chemical,
            "amount": self.amount,
            "expiry_date": self.expiry_date,
            "supplier_ref": self.supplier_ref,
        }

    @classmethod
    def from_dict(cls, item: dict[str, object]) -> "InboundLine":
        return cls(
            chemical=str(item.get("chemical", "")),
            amount=float(item.get("amount", 0.0)),
            expiry_date=str(item.get("expiry_date", "") or ""),
            supplier_ref=str(item.get("supplier_ref", "") or ""),
        )


@dataclass
class InboundOrder:
    """A delivery note that books one or more batches into stock.

    Lifecycle: ``received`` -> ``applied`` / ``failed``. ``received`` only
    persists after the first phase of submission, so a crashed submission can
    be recovered idempotently without risking a double booking.
    """

    order_id: str
    request_id: str
    lines: list[InboundLine]
    status: str = "received"
    fingerprint: str = ""
    created_at: str = ""
    applied_at: str = ""
    batch_ids: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "request_id": self.request_id,
            "lines": [line.as_dict() for line in self.lines],
            "status": self.status,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
            "applied_at": self.applied_at,
            "batch_ids": list(self.batch_ids),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, item: dict[str, object]) -> "InboundOrder":
        raw_lines = item.get("lines", [])
        lines = [InboundLine.from_dict(line) for line in raw_lines] if isinstance(raw_lines, list) else []
        raw_batches = item.get("batch_ids", [])
        return cls(
            order_id=str(item.get("order_id", "")),
            request_id=str(item.get("request_id", "")),
            lines=lines,
            status=str(item.get("status", "received")),
            fingerprint=str(item.get("fingerprint", "")),
            created_at=str(item.get("created_at", "")),
            applied_at=str(item.get("applied_at", "")),
            batch_ids=[str(batch) for batch in raw_batches] if isinstance(raw_batches, list) else [],
            error=str(item.get("error", "")),
        )


@dataclass
class Allocation:
    """How much of a consumption order was taken from one batch."""

    batch_id: str
    expiry_date: str
    quantity: float

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "expiry_date": self.expiry_date,
            "quantity": self.quantity,
        }

    @classmethod
    def from_dict(cls, item: dict[str, object]) -> "Allocation":
        return cls(
            batch_id=str(item.get("batch_id", "")),
            expiry_date=str(item.get("expiry_date", "") or ""),
            quantity=float(item.get("quantity", 0.0)),
        )


@dataclass
class ConsumptionOrder:
    """A dosing/withdraw order consumed FEFO with an all or nothing rule."""

    order_id: str
    request_id: str
    chemical: str
    amount: float
    allocations: list[Allocation]
    status: str = "applied"
    purpose: str = ""
    created_at: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "request_id": self.request_id,
            "chemical": self.chemical,
            "amount": self.amount,
            "allocations": [allocation.as_dict() for allocation in self.allocations],
            "status": self.status,
            "purpose": self.purpose,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, item: dict[str, object]) -> "ConsumptionOrder":
        raw = item.get("allocations", [])
        allocations = [Allocation.from_dict(a) for a in raw] if isinstance(raw, list) else []
        return cls(
            order_id=str(item.get("order_id", "")),
            request_id=str(item.get("request_id", "")),
            chemical=str(item.get("chemical", "")),
            amount=float(item.get("amount", 0.0)),
            allocations=allocations,
            status=str(item.get("status", "applied")),
            purpose=str(item.get("purpose", "") or ""),
            created_at=str(item.get("created_at", "")),
        )


@dataclass
class QuarantineEntry:
    """A resend whose request id was reused with a different payload.

    Warehouse rule: such a request is neither dropped nor silently applied;
    it is parked here for a human to approve (book it in under a new order)
    or reject (discard and keep the audit trail).
    """

    quarantine_id: str
    request_id: str
    original_fingerprint: str
    incoming_fingerprint: str
    inbound: InboundOrder
    status: str = "pending"
    created_at: str = ""
    resolved_at: str = ""
    resolution: str = ""
    resolved_order_id: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "quarantine_id": self.quarantine_id,
            "request_id": self.request_id,
            "original_fingerprint": self.original_fingerprint,
            "incoming_fingerprint": self.incoming_fingerprint,
            "inbound": self.inbound.as_dict(),
            "status": self.status,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution,
            "resolved_order_id": self.resolved_order_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, item: dict[str, object]) -> "QuarantineEntry":
        raw_inbound = item.get("inbound", {})
        inbound = (
            InboundOrder.from_dict(raw_inbound)
            if isinstance(raw_inbound, dict)
            else InboundOrder("", "", [])
        )
        return cls(
            quarantine_id=str(item.get("quarantine_id", "")),
            request_id=str(item.get("request_id", "")),
            original_fingerprint=str(item.get("original_fingerprint", "")),
            incoming_fingerprint=str(item.get("incoming_fingerprint", "")),
            inbound=inbound,
            status=str(item.get("status", "pending")),
            created_at=str(item.get("created_at", "")),
            resolved_at=str(item.get("resolved_at", "")),
            resolution=str(item.get("resolution", "")),
            resolved_order_id=str(item.get("resolved_order_id", "")),
            note=str(item.get("note", "")),
        )


def fingerprint_lines(lines: list[InboundLine]) -> str:
    """Canonical payload digest used to detect same id / different body resends.

    JSON with sorted keys is canonical enough for in-process idempotency and
    stays human readable inside the stored order.
    """

    import json

    return json.dumps(
        [line.as_dict() for line in lines], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def today_view(batch: Batch, today: dt.date) -> dict[str, object]:
    return batch.view(today)


__all__ = [
    "ACTIVE",
    "EXHAUSTED",
    "EXPIRED",
    "LIVE_STATUSES",
    "SCRAPPED",
    "SPENT_STATUSES",
    "Allocation",
    "Batch",
    "ConsumptionOrder",
    "InboundLine",
    "InboundOrder",
    "LedgerEntry",
    "QuarantineEntry",
    "fefo_sort_key",
    "fingerprint_lines",
    "parse_date",
    "date_text",
    "today_view",
]
