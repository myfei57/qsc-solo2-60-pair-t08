"""Append only audit trail for every batch related state change.

Distinct from the dosing :class:`~waterplant.audit.Auditor`: that log records
per chemical dose decisions, this log records the batch ledger lifecycle
(receive, consume, scrap, expiry flip, idempotent replay, quarantine and the
human resolution of a quarantine). Entries are intentionally structured so
the console can show who/what changed which batch.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

BATCH_AUDIT_KEY = "batches:audit-entries"

#: Movement verbs used as audit kinds, kept here so they cannot drift.
RECEIVE = "batch.receive"
CONSUME = "batch.consume"
SCRAP = "batch.scrap"
EXPIRE = "batch.expire"
REPLAY = "batch.replay"
QUARANTINE = "inbound.quarantine"
QUARANTINE_APPROVE = "inbound.quarantine.approve"
QUARANTINE_REJECT = "inbound.quarantine.reject"
RECOVER = "inbound.recover"
ORDER_FAILED = "inbound.failed"


@dataclass(frozen=True)
class AuditEntry:
    """One structured batch audit row."""

    id: str
    at: str
    kind: str
    chemical: str
    batch_id: str
    detail: str
    order_id: str = ""
    request_id: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "at": self.at,
            "kind": self.kind,
            "chemical": self.chemical,
            "batch_id": self.batch_id,
            "detail": self.detail,
            "order_id": self.order_id,
            "request_id": self.request_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "AuditEntry":
        return cls(
            id=str(payload.get("id", "")),
            at=str(payload.get("at", "")),
            kind=str(payload.get("kind", "")),
            chemical=str(payload.get("chemical", "")),
            batch_id=str(payload.get("batch_id", "")),
            detail=str(payload.get("detail", "")),
            order_id=str(payload.get("order_id", "")),
            request_id=str(payload.get("request_id", "")),
        )


class BatchAuditor:
    """Records and queries batch lifecycle entries.

    ``record`` appends through the normal command helper. Because that helper
    reads and writes the store, it participates transparently in an open
    ``store.transaction``: the append is staged and flushed in the same atomic
    move as the ledger, so an audit row and its stock move commit together.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def record(
        self,
        kind: str,
        chemical: str,
        detail: str,
        batch_id: str = "",
        order_id: str = "",
        request_id: str = "",
        at: str = "",
        entry_id: str = "",
    ) -> AuditEntry:
        entry = self.build(
            kind,
            chemical,
            detail,
            batch_id=batch_id,
            order_id=order_id,
            request_id=request_id,
            at=at,
            entry_id=entry_id,
        )
        append_command(self._store, BATCH_AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def build(
        self,
        kind: str,
        chemical: str,
        detail: str,
        batch_id: str = "",
        order_id: str = "",
        request_id: str = "",
        at: str = "",
        entry_id: str = "",
    ) -> AuditEntry:
        return AuditEntry(
            id=entry_id or uuid.uuid4().hex,
            at=at
            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            kind=kind,
            chemical=chemical,
            batch_id=batch_id,
            detail=detail,
            order_id=order_id,
            request_id=request_id,
        )

    def entries(self) -> list[AuditEntry]:
        rows: list[AuditEntry] = []
        for item in load_commands(self._store, BATCH_AUDIT_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                rows.append(AuditEntry.from_dict(payload))
        return rows

    def filter(self, kind: str = "", chemical: str = "", batch_id: str = "") -> list[AuditEntry]:
        return [
            entry
            for entry in self.entries()
            if (not kind or entry.kind == kind)
            and (not chemical or entry.chemical == chemical)
            and (not batch_id or entry.batch_id == batch_id)
        ]
