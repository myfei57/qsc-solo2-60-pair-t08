"""Append only audit entries for every chemical dose."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from waterplant.store.store import Store

from .report import AuditState

AUDIT_KEY = "audit:entries"


@dataclass(frozen=True)
class Entry:
    """One recorded dosing decision."""

    id: str
    time: str
    kind: str
    detail: str
    ref: str = ""

    def as_dict(self) -> dict[str, str]:
        payload = {"id": self.id, "time": self.time, "kind": self.kind, "detail": self.detail}
        if self.ref:
            payload["ref"] = self.ref
        return payload


class Auditor:
    """Records and queries dosing entries."""

    def __init__(self, store: Store) -> None:
        self._store = store

    @staticmethod
    def make_entry(kind: str, detail: str, ref: str = "", entry_id: str | None = None) -> Entry:
        return Entry(
            id=entry_id or uuid.uuid4().hex,
            time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            kind=kind,
            detail=detail,
            ref=ref,
        )

    def record(self, kind: str, detail: str, ref: str = "") -> Entry:
        entry = self.make_entry(kind, detail, ref)
        self.append_entry(entry)
        return entry

    def append_entry(self, entry: Entry) -> None:
        """Append an entry in one atomic store transaction."""

        def mutate(data: dict[str, str]) -> None:
            try:
                parsed = json.loads(data.get(AUDIT_KEY, "[]"))
            except ValueError:
                parsed = []
            items = parsed if isinstance(parsed, list) else []
            items.append(entry.as_dict())
            data[AUDIT_KEY] = json.dumps(items, ensure_ascii=False)

        self._store.atomic(mutate)

    @staticmethod
    def _entry_from_payload(payload: dict[str, object]) -> Entry:
        return Entry(
            id=str(payload.get("id", "")),
            time=str(payload.get("time", "")),
            kind=str(payload.get("kind", "")),
            detail=str(payload.get("detail", "")),
            ref=str(payload.get("ref", "")),
        )

    def entries(self) -> list[Entry]:
        raw, present = self._store.get(AUDIT_KEY)
        if not present:
            return []
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(parsed, list):
            return []
        entries: list[Entry] = []
        for item in parsed:
            if isinstance(item, dict):
                payload = item
            else:
                try:
                    payload = json.loads(str(item))
                except ValueError:
                    continue
            if isinstance(payload, dict):
                entries.append(self._entry_from_payload(payload))
        return entries

    def last(self) -> Entry | None:
        entries = self.entries()
        if not entries:
            return None
        return entries[-1]

    def count(self) -> int:
        return len(self.entries())

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def filter(self, kind: str) -> list[Entry]:
        return [entry for entry in self.entries() if entry.kind == kind]

    def kinds(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            if entry.kind not in seen:
                seen.append(entry.kind)
        return seen

    def totals(self) -> dict[str, float]:
        """Sum the numeric dose recorded for each chemical."""

        totals: dict[str, float] = {}
        for entry in self.entries():
            try:
                amount = float(entry.detail)
            except ValueError:
                continue
            totals[entry.kind] = totals.get(entry.kind, 0.0) + amount
        return totals

    def state(self) -> AuditState:
        entries = self.entries()
        return AuditState(entries=[entry.as_dict() for entry in entries], count=len(entries))

    def describe(self) -> str:
        return f"audit entries={len(self.entries())}"
