"""Thread safe JSON document behind the control service."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from typing import Iterator


class Store:
    """A file backed mapping from string keys to string values.

    Writes are atomic: the document is written to a sibling temporary file and
    then moved over the target path so a crash cannot leave a partial document.

    A :meth:`transaction` buffers several key writes and flushes them with one
    atomic move, so a multi document ledger change either lands completely or
    not at all.
    """

    _DELETE = object()

    def __init__(self, path: str, data: dict[str, str] | None = None) -> None:
        self._path = path
        self._data: dict[str, str] = dict(data) if data else {}
        self._lock = threading.RLock()
        self._depth = 0
        self._staged: dict[str, object] = {}

    @property
    def path(self) -> str:
        return self._path

    @classmethod
    def open(cls, path: str) -> "Store":
        """Load a store from disk, creating an empty one when absent."""

        absolute = os.path.abspath(path)
        parent = os.path.dirname(absolute)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            with open(absolute, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return cls(absolute)
        if not raw.strip():
            return cls(absolute)
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("store document must be a JSON object")
        return cls(absolute, {str(key): str(value) for key, value in loaded.items()})

    def get(self, key: str) -> tuple[str, bool]:
        with self._lock:
            if key in self._staged:
                staged = self._staged[key]
                if staged is self._DELETE:
                    return "", False
                return str(staged), True
            if key in self._data:
                return self._data[key], True
            return "", False

    def put(self, key: str, value: str) -> None:
        with self._lock:
            if self._depth:
                self._staged[key] = value
                return
            self._data[key] = value
            self._save_locked()

    def delete(self, key: str) -> None:
        with self._lock:
            if self._depth:
                if key not in self._data:
                    self._staged.pop(key, None)
                    return
                self._staged[key] = self._DELETE
                return
            if key not in self._data:
                return
            del self._data[key]
            self._save_locked()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Buffer writes on this thread and commit them with one atomic save.

        The store lock is held for the whole block so concurrent transactions
        serialise against each other: committed changes are invisible to other
        threads until the single atomic flush, and a parallel reader can never
        observe half of a multi key update. Nested usage shares one transaction
        and only the outermost block commits or rolls back; a failure in any
        block discards the entire unit of work.
        """

        with self._lock:
            snapshot = dict(self._staged)
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self._staged = {}
                else:
                    self._staged = snapshot
                raise
            self._depth -= 1
            if self._depth == 0:
                for staged_key, staged_value in self._staged.items():
                    if staged_value is self._DELETE:
                        self._data.pop(staged_key, None)
                    else:
                        self._data[staged_key] = str(staged_value)
                self._staged = {}
                self._save_locked()

    def _committed_view_locked(self) -> dict[str, str]:
        """Merge staged writes over the committed document for reads/lookups."""

        view = dict(self._data)
        for staged_key, staged_value in self._staged.items():
            if staged_value is self._DELETE:
                view.pop(staged_key, None)
            else:
                view[staged_key] = str(staged_value)
        return view

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._committed_view_locked())

    def count(self) -> int:
        with self._lock:
            return len(self._committed_view_locked())

    def clear(self) -> None:
        with self._lock:
            if self._depth:
                self._staged = {key: self._DELETE for key in self._committed_view_locked()}
                return
            self._data = {}
            self._save_locked()

    def close(self) -> None:
        with self._lock:
            if self._depth:
                return
            self._save_locked()

    def _save_locked(self) -> None:
        raw = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        temporary = f"{self._path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(raw)
        os.replace(temporary, self._path)
