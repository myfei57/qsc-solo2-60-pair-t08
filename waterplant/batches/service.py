"""Batch inventory service: inbound, FEFO consumption, recovery and alerts.

This is the transactional heart of the subsystem. The ordering rules that the
warehouse process fixes are enforced here, in one place:

* draw order is FEFO (earliest expiry first) with FIFO (arrival sequence) as
  the tie breaker, via :func:`waterplant.batches.models.fefo_sort_key`;
* expired stock is never drawn by a normal consumption;
* a consumption is all or nothing — short stock rejects the whole order and
  no batch, ledger, audit or dosing record is written;
* resends keyed by the same ``request_id`` return the stored first result
  (idempotent replay); same id with a different inbound body is parked in the
  quarantine list for manual approve/reject instead of being dropped;
* every multi aggregate mutation runs inside one ``store.transaction``, so
  the batch ledger, the dosing record and the audit trail commit as a unit
  and the same batch can never be deducted twice by a retried request.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from waterplant.inventory.report import validate_quantity
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from . import audit as audit_kind
from .alerts import Alert, collect_alerts
from .audit import BatchAuditor
from .clock import Clock, parse_date
from .models import (
    ACTIVE,
    EXHAUSTED,
    EXPIRED,
    SCRAPPED,
    Allocation,
    Batch,
    ConsumptionOrder,
    InboundLine,
    InboundOrder,
    LedgerEntry,
    QuarantineEntry,
    fefo_sort_key,
    fingerprint_lines,
)
from .repo import (
    BatchRepository,
    ConsumptionRepository,
    IdempotencyRepository,
    InboundRepository,
    LedgerRepository,
    QuarantineRepository,
)

REORDER_KEY_PREFIX = "inventory:reorder-level:"
GLOBAL_REORDER_KEY = "inventory:reorder-level"
EPSILON = 1e-9

#: Idempotency operation names stored alongside the first result.
OP_INBOUND = "inbound"
OP_CONSUME = "consume"
OP_SCRAP = "scrap"

#: Inbound lifecycle states.
RECEIVED = "received"
APPLIED = "applied"
FAILED = "failed"


class IdempotencyConflict(ValueError):
    """A request id was reused with a different fingerprint."""


class QuarantineConflict(IdempotencyConflict):
    """An inbound resend conflict; the payload was parked for manual review."""

    def __init__(self, quarantine_id: str, message: str) -> None:
        super().__init__(message)
        self.quarantine_id = quarantine_id


class _InboundResend(Exception):
    """Internal control flow: a request id was found while staging an inbound."""

    def __init__(self, record: dict[str, object]) -> None:
        super().__init__("inbound request id already present")
        self.record = record


@dataclass
class ConsumptionResult:
    """What a consumption order did, returned and replayed by request id."""

    order: ConsumptionOrder
    allocations: list[Allocation]
    balance: float
    replayed: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order.order_id,
            "request_id": self.order.request_id,
            "chemical": self.order.chemical,
            "consumed": self.order.amount,
            "balance": self.balance,
            "replayed": self.replayed,
            "allocations": [allocation.as_dict() for allocation in self.allocations],
        }


@dataclass
class InboundResult:
    """Outcome of an inbound submission, including resend handling."""

    order: InboundOrder
    batches: list[Batch]
    replayed: bool = False

    def as_dict(self, today: dt.date | None = None) -> dict[str, object]:
        views = (
            [batch.as_dict() for batch in self.batches]
            if today is None
            else [batch.view(today) for batch in self.batches]
        )
        return {
            "order_id": self.order.order_id,
            "request_id": self.order.request_id,
            "status": self.order.status,
            "replayed": self.replayed,
            "batch_ids": list(self.order.batch_ids),
            "batches": views,
        }


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _validate_positive(amount: float, field: str) -> None:
    validate_quantity(amount, field)
    if amount <= EPSILON:
        raise ValueError(f"{field} must be positive")


def _validate_lines(lines: list[InboundLine]) -> None:
    if not lines:
        raise ValueError("inbound order must contain at least one line")
    for index, line in enumerate(lines):
        if not line.chemical.strip():
            raise ValueError(f"line {index} chemical must not be empty")
        _validate_positive(line.amount, f"line {index} amount")
        if line.expiry_date:
            parse_date(line.expiry_date)


class BatchService:
    """Coordinates repositories, audit, clock and idempotency for one store."""

    def __init__(self, store: Store, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or Clock()
        self.batches = BatchRepository(store)
        self.ledger = LedgerRepository(store)
        self.inbounds = InboundRepository(store)
        self.consumptions = ConsumptionRepository(store)
        self.idempotency = IdempotencyRepository(store)
        self.quarantine = QuarantineRepository(store)
        self.auditor = BatchAuditor(store)

    # ----- configuration ---------------------------------------------------

    @property
    def clock(self) -> Clock:
        return self._clock

    def near_expiry_days(self) -> int:
        return self.batches.near_expiry_days()

    def set_near_expiry_days(self, days: int) -> int:
        if days < 0:
            raise ValueError("near_expiry_days must be non-negative")
        return self.batches.set_near_expiry_days(int(days))

    def reorder_level(self, chemical: str) -> float:
        value, present = load_float(self._store, f"{REORDER_KEY_PREFIX}{chemical}")
        if present:
            return value
        value, present = load_float(self._store, GLOBAL_REORDER_KEY)
        return value if present else 25.0

    def set_reorder_level(self, chemical: str, level: float) -> float:
        validate_quantity(level, "level")
        save_float(self._store, f"{REORDER_KEY_PREFIX}{chemical}", level)
        return level

    # ----- inbound ---------------------------------------------------------

    def submit_inbound(
        self,
        lines: list[InboundLine],
        request_id: str,
        order_id: str = "",
    ) -> InboundResult:
        """Book an inbound delivery note idempotently.

        Phase one persists the note as ``received`` before any batch exists.
        Phase two applies it inside another transaction. A crash between the
        two leaves a ``received`` note that :meth:`recover` resumes with no
        double booking.
        """

        _validate_lines(lines)
        request_id = (request_id or "").strip()
        if not request_id:
            raise ValueError("request_id is required for idempotent inbound")
        fingerprint = fingerprint_lines(lines)

        records = self.idempotency.load()
        if records.get(request_id) is not None:
            return self._handle_inbound_resend(
                records[request_id], request_id, fingerprint, lines
            )

        order = InboundOrder(
            order_id=order_id or _new_id("IN"),
            request_id=request_id,
            lines=list(lines),
            status=RECEIVED,
            fingerprint=fingerprint,
            created_at=self._clock.now(),
        )
        try:
            with self._store.transaction():
                # Re-check inside the serialised transaction so two concurrent
                # submissions with the same request id cannot both pass the
                # outer read and book the delivery twice.
                records = self.idempotency.load()
                staged = records.get(request_id)
                if staged is not None:
                    raise _InboundResend(staged)
                pending = self.inbounds.load()
                pending.append(order)
                self.inbounds.save(pending)
                # Reserve the idempotency key in the same commit as the received
                # note. A crash now leaves a resumable ``received`` order; the
                # key is already held so a resend cannot open a second order.
                records[request_id] = {
                    "op": OP_INBOUND,
                    "order_id": order.order_id,
                    "fingerprint": fingerprint,
                    "status": RECEIVED,
                    "at": order.created_at,
                }
                self.idempotency.save(records)
        except _InboundResend as resend:
            return self._handle_inbound_resend(
                resend.record, request_id, fingerprint, lines
            )

        try:
            return self._apply_inbound(order.order_id)
        except ValueError:
            # Business rule failure (re-validated against live state): keep
            # the note as failed so the warehouse can inspect and resubmit.
            self._mark_inbound_failed(order.order_id)
            raise

    def _handle_inbound_resend(
        self,
        existing: dict[str, object],
        request_id: str,
        fingerprint: str,
        lines: list[InboundLine],
    ) -> InboundResult:
        operation = str(existing.get("op", ""))
        if operation != OP_INBOUND:
            raise IdempotencyConflict(
                f"request_id {request_id} already used by a {operation} request"
            )
        if str(existing.get("fingerprint", "")) != fingerprint:
            quarantine_id = self._park_resend(existing, request_id, fingerprint, lines)
            raise QuarantineConflict(
                quarantine_id,
                f"request_id {request_id} resent with a different payload; "
                f"parked as {quarantine_id} for manual confirmation",
            )
        order_id = str(existing.get("order_id", ""))
        order = self._require_inbound(order_id)
        # A resend that arrives while the first submission is still in its
        # received phase (or after a crash between the two phases) resumes the
        # booking instead of opening a second order.
        if order.status == RECEIVED:
            return self._apply_inbound(order_id)
        return InboundResult(order=order, batches=self._order_batches(order), replayed=True)

    def _park_resend(
        self,
        existing: dict[str, object],
        request_id: str,
        fingerprint: str,
        lines: list[InboundLine],
    ) -> str:
        """Park the conflicting resend; repeated conflicts stay idempotent."""

        pending = [entry for entry in self.quarantine.load() if entry.status == "pending"]
        for entry in pending:
            if (
                entry.request_id == request_id
                and entry.incoming_fingerprint == fingerprint
            ):
                return entry.quarantine_id
        order = InboundOrder(
            order_id=_new_id("INQ"),
            request_id=request_id,
            lines=list(lines),
            status="quarantined",
            fingerprint=fingerprint,
            created_at=self._clock.now(),
        )
        entry = QuarantineEntry(
            quarantine_id=_new_id("Q"),
            request_id=request_id,
            original_fingerprint=str(existing.get("fingerprint", "")),
            incoming_fingerprint=fingerprint,
            inbound=order,
            status="pending",
            created_at=self._clock.now(),
        )
        with self._store.transaction():
            rows = self.quarantine.load()
            rows.append(entry)
            self.quarantine.save(rows)
            self.auditor.record(
                audit_kind.QUARANTINE,
                ",".join(sorted({line.chemical for line in lines})),
                "resend with same request_id but different payload",
                order_id=order.order_id,
                request_id=request_id,
            )
        return entry.quarantine_id

    def _apply_inbound(self, order_id: str) -> InboundResult:
        with self._store.transaction():
            orders = self.inbounds.load()
            order = next((candidate for candidate in orders if candidate.order_id == order_id), None)
            if order is None:
                raise ValueError(f"unknown inbound order {order_id}")
            if order.status == APPLIED:
                return InboundResult(order=order, batches=self._order_batches(order), replayed=True)
            if order.status == FAILED:
                return InboundResult(order=order, batches=self._order_batches(order), replayed=True)

            _validate_lines(order.lines)
            rows = self.batches.load()
            ledger_entries = self.ledger.load()
            known = {batch.batch_id for batch in rows}
            today = self._clock.today()
            created: list[Batch] = []
            for index, line in enumerate(order.lines):
                batch_id = f"{order.order_id}-L{index + 1}"
                if batch_id in known:  # crashed mid apply: this batch is booked
                    continue
                seq = self.batches.next_seq()
                batch = Batch(
                    chemical=line.chemical,
                    batch_id=batch_id,
                    received=line.amount,
                    expiry_date=line.expiry_date,
                    status=EXPIRED if line.expiry_date and parse_date(line.expiry_date) < today else ACTIVE,
                    received_at=order.created_at,
                    seq=seq,
                    inbound_id=order.order_id,
                    supplier_ref=line.supplier_ref,
                )
                rows.append(batch)
                created.append(batch)
                ledger_entries.append(
                    LedgerEntry(
                        id=_new_id("LED"),
                        at=self._clock.now(),
                        movement="in",
                        chemical=line.chemical,
                        batch_id=batch_id,
                        quantity=line.amount,
                        order_id=order.order_id,
                        request_id=order.request_id,
                        note=(
                            f"expired on receipt {line.expiry_date}"
                            if batch.status == EXPIRED
                            else "inbound"
                        ),
                    )
                )

            order.status = APPLIED
            order.applied_at = self._clock.now()
            order.batch_ids = [batch.batch_id for batch in rows if batch.inbound_id == order.order_id]

            self.batches.save(rows)
            self.ledger.save(ledger_entries)
            self.inbounds.save(orders)
            records = self.idempotency.load()
            records[order.request_id] = {
                "op": OP_INBOUND,
                "order_id": order.order_id,
                "fingerprint": order.fingerprint,
                "status": APPLIED,
                "at": order.applied_at,
            }
            self.idempotency.save(records)
            for batch in created:
                self.auditor.record(
                    audit_kind.RECEIVE,
                    batch.chemical,
                    f"received {batch.received:.4f} expiry={batch.expiry_date or 'open'}",
                    batch_id=batch.batch_id,
                    order_id=order.order_id,
                    request_id=order.request_id,
                )

        return InboundResult(order=order, batches=self._order_batches(order), replayed=False)

    def _mark_inbound_failed(self, order_id: str) -> None:
        with self._store.transaction():
            orders = self.inbounds.load()
            order = next((candidate for candidate in orders if candidate.order_id == order_id), None)
            if order is None or order.status != RECEIVED:
                return
            order.status = FAILED
            order.applied_at = self._clock.now()
            order.error = "business validation failed during apply"
            self.inbounds.save(orders)
            records = self.idempotency.load()
            if order.request_id in records:
                records[order.request_id]["status"] = FAILED
                self.idempotency.save(records)
            self.auditor.record(
                audit_kind.ORDER_FAILED,
                ",".join(sorted({line.chemical for line in order.lines})),
                order.error,
                order_id=order.order_id,
                request_id=order.request_id,
            )

    def recover(self) -> list[str]:
        """Resume every inbound note still in the ``received`` phase.

        Safe to run after a crash and safe to repeat: each note applies with
        deterministic batch ids and an already booked batch is skipped.
        """

        recovered: list[str] = []
        for order in self.inbounds.load():
            if order.status != RECEIVED:
                continue
            self._apply_inbound(order.order_id)
            recovered.append(order.order_id)
            self.auditor.record(
                audit_kind.RECOVER,
                ",".join(sorted({line.chemical for line in order.lines})),
                "resumed interrupted inbound order",
                order_id=order.order_id,
                request_id=order.request_id,
            )
        return recovered

    # ----- consumption -----------------------------------------------------

    def consume(
        self,
        chemical: str,
        amount: float,
        request_id: str,
        order_id: str = "",
        purpose: str = "",
    ) -> ConsumptionResult:
        """Withdraw ``amount`` FEFO, all or nothing, idempotently."""

        chemical = (chemical or "").strip()
        if not chemical:
            raise ValueError("chemical must not be empty")
        _validate_positive(amount, "amount")
        request_id = (request_id or "").strip()
        if not request_id:
            raise ValueError("request_id is required for idempotent consumption")

        records = self.idempotency.load()
        existing = records.get(request_id)
        if existing is not None:
            return self._replay_consumption(existing, request_id, chemical, amount)

        order = ConsumptionOrder(
            order_id=order_id or _new_id("USE"),
            request_id=request_id,
            chemical=chemical,
            amount=amount,
            allocations=[],
            status=APPLIED,
            purpose=purpose,
            created_at=self._clock.now(),
        )

        with self._store.transaction():
            rows = self.batches.load()
            today = self._clock.today()
            candidates = sorted(
                (
                    batch
                    for batch in rows
                    if batch.chemical == chemical
                    and batch.derived_status(today) == ACTIVE
                    and batch.remaining() > EPSILON
                ),
                key=fefo_sort_key,
            )

            allocations = self._plan_allocation(candidates, amount)
            by_id = {batch.batch_id: batch for batch in rows}
            ledger_entries = self.ledger.load()
            for allocation in allocations:
                batch = by_id[allocation.batch_id]
                batch.consumed += allocation.quantity
                if batch.remaining() <= EPSILON:
                    batch.status = EXHAUSTED
                ledger_entries.append(
                    LedgerEntry(
                        id=_new_id("LED"),
                        at=self._clock.now(),
                        movement="out",
                        chemical=chemical,
                        batch_id=batch.batch_id,
                        quantity=allocation.quantity,
                        order_id=order.order_id,
                        request_id=request_id,
                        note=purpose or "consumption",
                    )
                )
            order.allocations = allocations
            consumption_orders = self.consumptions.load()
            consumption_orders.append(order)

            self.batches.save(rows)
            self.ledger.save(ledger_entries)
            self.consumptions.save(consumption_orders)
            records[request_id] = {
                "op": OP_CONSUME,
                "order_id": order.order_id,
                "fingerprint": f"{chemical}|{amount:.6f}",
                "at": order.created_at,
            }
            self.idempotency.save(records)
            self.auditor.record(
                audit_kind.CONSUME,
                chemical,
                f"consumed {amount:.4f} across {len(allocations)} batch(es): "
                + ",".join(f"{a.batch_id}:{a.quantity:.4f}" for a in allocations),
                order_id=order.order_id,
                request_id=request_id,
            )

        return ConsumptionResult(
            order=order, allocations=allocations, balance=self.balance(chemical)
        )

    @staticmethod
    def _plan_allocation(candidates: list[Batch], amount: float) -> list[Allocation]:
        outstanding = amount
        allocations: list[Allocation] = []
        for batch in candidates:
            available = batch.remaining()
            if available <= EPSILON:
                continue
            taken = available if available < outstanding else outstanding
            allocations.append(
                Allocation(batch_id=batch.batch_id, expiry_date=batch.expiry_date, quantity=taken)
            )
            outstanding -= taken
            if outstanding <= EPSILON:
                break
        if outstanding > EPSILON:
            total = amount - outstanding
            raise ValueError(
                f"insufficient non-expired inventory: requested {amount:.4f}, "
                f"available {total:.4f}"
            )
        return allocations

    def _replay_consumption(
        self, existing: dict[str, object], request_id: str, chemical: str, amount: float
    ) -> ConsumptionResult:
        operation = str(existing.get("op", ""))
        if operation != OP_CONSUME:
            raise IdempotencyConflict(
                f"request_id {request_id} already used by a {operation} request"
            )
        expected_fingerprint = f"{chemical}|{amount:.6f}"
        if str(existing.get("fingerprint", "")) != expected_fingerprint:
            raise IdempotencyConflict(
                f"request_id {request_id} resent with a different consumption payload"
            )
        order_id = str(existing.get("order_id", ""))
        order = next(
            (candidate for candidate in self.consumptions.load() if candidate.order_id == order_id),
            None,
        )
        if order is None:
            raise ValueError(f"stored consumption order {order_id} is missing")
        self.auditor.record(
            audit_kind.REPLAY,
            chemical,
            f"idempotent replay of {order_id}",
            order_id=order_id,
            request_id=request_id,
        )
        return ConsumptionResult(
            order=order,
            allocations=order.allocations,
            balance=self.balance(chemical),
            replayed=True,
        )

    # ----- scrap / expiry --------------------------------------------------

    def scrap_batch(
        self,
        batch_id: str,
        amount: float | None = None,
        request_id: str = "",
        reason: str = "",
    ) -> dict[str, object]:
        """Write off stock (typically expired) from one batch, audited."""

        request_id = (request_id or "").strip()
        records = self.idempotency.load()
        if request_id and request_id in records:
            existing = records[request_id]
            conflict = str(existing.get("op", "")) != OP_SCRAP or str(
                existing.get("batch_id", "")
            ) != batch_id
            if not conflict and amount is not None:
                try:
                    conflict = abs(float(existing.get("quantity", 0.0)) - amount) > EPSILON
                except (TypeError, ValueError):
                    conflict = True
            if conflict:
                raise IdempotencyConflict(
                    f"request_id {request_id} already used by another scrap request"
                )
            return {"batch_id": batch_id, "replayed": True, "scrapped": existing.get("quantity", 0.0)}

        with self._store.transaction():
            rows = self.batches.load()
            batch = next((candidate for candidate in rows if candidate.batch_id == batch_id), None)
            if batch is None:
                raise ValueError(f"unknown batch {batch_id}")
            current = batch.remaining()
            if amount is None:
                target = current
            else:
                _validate_positive(amount, "amount")
                target = amount
            if target > current + EPSILON:
                raise ValueError(
                    f"cannot scrap {target:.4f}, only {current:.4f} remaining on {batch_id}"
                )
            batch.scrapped += target
            if batch.remaining() <= EPSILON:
                batch.status = SCRAPPED
            elif batch.status == ACTIVE:
                # partial write off leaves the batch in its current shelf state
                pass
            ledger_entries = self.ledger.load()
            ledger_entries.append(
                LedgerEntry(
                    id=_new_id("LED"),
                    at=self._clock.now(),
                    movement="scrap",
                    chemical=batch.chemical,
                    batch_id=batch_id,
                    quantity=target,
                    order_id="",
                    request_id=request_id,
                    note=reason or "manual scrap",
                )
            )
            self.batches.save(rows)
            self.ledger.save(ledger_entries)
            if request_id:
                records[request_id] = {
                    "op": OP_SCRAP,
                    "batch_id": batch_id,
                    "quantity": target,
                    "fingerprint": f"{batch_id}|{target:.6f}",
                    "at": self._clock.now(),
                }
                self.idempotency.save(records)
            self.auditor.record(
                audit_kind.SCRAP,
                batch.chemical,
                f"scrapped {target:.4f} reason={reason or 'manual'}",
                batch_id=batch_id,
                request_id=request_id,
            )
        return {"batch_id": batch_id, "scrapped": target, "replayed": False}

    def refresh_expiry_status(self) -> list[Batch]:
        """Flip live batches that crossed their expiry date to ``expired``.

        Returns the batches whose status changed so a scheduled job can alert
        on exactly the new transitions. Reads are calendar based, so a batch
        never silently flips back.
        """

        changed: list[Batch] = []
        with self._store.transaction():
            today = self._clock.today()
            rows = self.batches.load()
            for batch in rows:
                if batch.status != ACTIVE:
                    continue
                if batch.is_expired(today) and batch.remaining() > EPSILON:
                    batch.status = EXPIRED
                    changed.append(batch)
                    self.auditor.record(
                        audit_kind.EXPIRE,
                        batch.chemical,
                        f"batch crossed expiry date {batch.expiry_date}",
                        batch_id=batch.batch_id,
                    )
            self.batches.save(rows)
        return changed

    # ----- quarantine resolution ------------------------------------------

    def quarantine_entries(self, status: str = "") -> list[QuarantineEntry]:
        entries = self.quarantine.load()
        return [entry for entry in entries if not status or entry.status == status]

    def resolve_quarantine(
        self, quarantine_id: str, approve: bool, note: str = ""
    ) -> dict[str, object]:
        """Approve (book in under a fresh order) or reject a parked resend."""

        with self._store.transaction():
            entries = self.quarantine.load()
            entry = next(
                (candidate for candidate in entries if candidate.quarantine_id == quarantine_id),
                None,
            )
            if entry is None:
                raise ValueError(f"unknown quarantine entry {quarantine_id}")
            if entry.status != "pending":
                raise ValueError(f"quarantine {quarantine_id} already {entry.status}")
            entry.status = "approved" if approve else "rejected"
            entry.resolved_at = self._clock.now()
            entry.resolution = note
            if approve:
                inbound = entry.inbound
                new_order = InboundOrder(
                    order_id=_new_id("IN"),
                    request_id=inbound.request_id,
                    lines=list(inbound.lines),
                    status=RECEIVED,
                    fingerprint=inbound.fingerprint,
                    created_at=self._clock.now(),
                )
                orders = self.inbounds.load()
                orders.append(new_order)
                self.inbounds.save(orders)
                entry.resolved_order_id = new_order.order_id
            self.quarantine.save(entries)
            self.auditor.record(
                audit_kind.QUARANTINE_APPROVE if approve else audit_kind.QUARANTINE_REJECT,
                ",".join(sorted({line.chemical for line in entry.inbound.lines})),
                f"manual {'approval' if approve else 'rejection'} {note}".strip(),
                order_id=entry.resolved_order_id,
                request_id=entry.request_id,
            )
            resolved_order_id = entry.resolved_order_id

        if approve and resolved_order_id:
            result = self._apply_inbound(resolved_order_id)
            return {
                "quarantine_id": quarantine_id,
                "decision": "approved",
                "order_id": resolved_order_id,
                "batch_ids": [batch.batch_id for batch in result.batches],
            }
        return {"quarantine_id": quarantine_id, "decision": "rejected", "order_id": ""}

    # ----- queries ---------------------------------------------------------

    def list_batches(self, chemical: str = "") -> list[Batch]:
        rows = self.batches.load()
        if chemical:
            rows = [batch for batch in rows if batch.chemical == chemical]
        return sorted(rows, key=fefo_sort_key)

    def _require_inbound(self, order_id: str) -> InboundOrder:
        order = next(
            (candidate for candidate in self.inbounds.load() if candidate.order_id == order_id),
            None,
        )
        if order is None:
            raise ValueError(f"unknown inbound order {order_id}")
        return order

    def _order_batches(self, order: InboundOrder) -> list[Batch]:
        wanted = set(order.batch_ids)
        return [batch for batch in self.batches.load() if batch.batch_id in wanted]

    def list_inbounds(self) -> list[InboundOrder]:
        return self.inbounds.load()

    def list_consumptions(self, chemical: str = "") -> list[ConsumptionOrder]:
        orders = self.consumptions.load()
        if chemical:
            orders = [order for order in orders if order.chemical == chemical]
        return orders

    def list_ledger(self, chemical: str = "", batch_id: str = "") -> list[LedgerEntry]:
        entries = self.ledger.load()
        if chemical:
            entries = [entry for entry in entries if entry.chemical == chemical]
        if batch_id:
            entries = [entry for entry in entries if entry.batch_id == batch_id]
        return entries

    def balance(self, chemical: str, include_expired: bool = False) -> float:
        today = self._clock.today()
        total = 0.0
        for batch in self.batches.load():
            if batch.chemical != chemical:
                continue
            status = batch.derived_status(today)
            if status in (EXHAUSTED, SCRAPPED):
                continue
            if status == EXPIRED and not include_expired:
                continue
            total += batch.remaining()
        return total

    def alerts(self) -> list[Alert]:
        chemicals = {batch.chemical for batch in self.batches.load()}
        levels = {chemical: self.reorder_level(chemical) for chemical in chemicals}
        return collect_alerts(
            self.batches.load(),
            self._clock.today(),
            self.near_expiry_days(),
            levels,
            25.0,
        )

    def state(self) -> dict[str, object]:
        today = self._clock.today()
        batches = self.list_batches()
        chemicals = sorted({batch.chemical for batch in batches})
        balances = {
            chemical: {
                "usable": self.balance(chemical),
                "including_expired": self.balance(chemical, include_expired=True),
            }
            for chemical in chemicals
        }
        return {
            "today": today.isoformat(),
            "near_expiry_days": self.near_expiry_days(),
            "consumption_policy": "FEFO (earliest expiry first, FIFO arrival order as tie break)",
            "resend_policy": "identical resend replays; conflicting resend quarantined for manual review",
            "chemicals": chemicals,
            "balances": balances,
            "batches": [batch.view(today) for batch in batches],
            "alerts": [alert.as_dict() for alert in self.alerts()],
            "quarantine_pending": len(self.quarantine_entries("pending")),
            "inbound_pending": len(
                [order for order in self.inbounds.load() if order.status == RECEIVED]
            ),
        }
