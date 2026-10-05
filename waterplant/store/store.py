"""Thread safe JSON document behind the control service."""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

try:  # The control service targets Linux containers.
    import fcntl
except ImportError:  # pragma: no cover - Windows development hosts
    fcntl = None

T = TypeVar("T")


class Store:
    """A file backed mapping from string keys to string values.

    Writes are atomic: the document is written to a sibling temporary file and
    then moved over the target path so a crash cannot leave a partial document.
    """

    def __init__(self, path: str, data: dict[str, str] | None = None) -> None:
        self._path = path
        self._data: dict[str, str] = dict(data) if data else {}
        self._lock = threading.RLock()
        self._file_lock = open(f"{self._path}.lock", "a+", encoding="utf-8")
        self._closed = False
        self._transaction_depth_local = threading.local()
        self._file_lock_depth = 0
        if data:
            with self._lock:
                if fcntl is not None:
                    fcntl.flock(self._file_lock.fileno(), fcntl.LOCK_EX)
                try:
                    self._save_locked()
                finally:
                    if fcntl is not None:
                        fcntl.flock(self._file_lock.fileno(), fcntl.LOCK_UN)

    @property
    def path(self) -> str:
        return self._path

    @property
    def _depth(self) -> int:
        return int(getattr(self._transaction_depth_local, "value", 0))

    @_depth.setter
    def _depth(self, value: int) -> None:
        self._transaction_depth_local.value = value

    @contextmanager
    def _locked(self, *, reload: bool = False) -> Iterator[None]:
        reentrant = self._depth > 0
        if not reentrant:
            self._lock.acquire()
            if fcntl is not None:
                fcntl.flock(self._file_lock.fileno(), fcntl.LOCK_EX)
                self._file_lock_depth = 1
        self._depth += 1
        try:
            if reload and not reentrant:
                self._reload_locked()
            yield
        finally:
            self._depth -= 1
            if not reentrant:
                if fcntl is not None and not self._closed:
                    if self._file_lock_depth:
                        fcntl.flock(self._file_lock.fileno(), fcntl.LOCK_UN)
                    self._file_lock_depth = 0
                self._lock.release()

    def _reload_locked(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except FileNotFoundError:
            self._data = {}
            return
        if not raw.strip():
            self._data = {}
            return
        loaded = json.loads(raw)
        if not isinstance(loaded, dict):
            raise ValueError("store document must be a JSON object")
        self._data = {str(key): str(value) for key, value in loaded.items()}

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
        with self._locked(reload=True):
            if key in self._data:
                return self._data[key], True
            return "", False

    def put(self, key: str, value: str) -> None:
        with self._locked(reload=True):
            self._data[key] = value
            self._save_locked()

    def atomic(self, mutator: Callable[[dict[str, str]], T]) -> T:
        """Run a multi-key mutation once and persist all keys together.

        The mutator changes the supplied mapping only.  Returning normally commits
        the whole transaction; raising rolls the in-memory mapping back so a
        failed batch ledger update cannot leave a dosing record behind.
        """

        reentrant = self._depth > 0
        if not reentrant:
            self._lock.acquire()
            if fcntl is not None:
                fcntl.flock(self._file_lock.fileno(), fcntl.LOCK_EX)
                self._file_lock_depth = 1
            self._reload_locked()
        self._depth += 1
        snapshot = dict(self._data)
        try:
            result = mutator(self._data)
        except Exception:
            self._data = snapshot
            raise
        else:
            if not reentrant:
                self._save_locked()
            return result
        finally:
            self._depth -= 1
            if not reentrant:
                if fcntl is not None and not self._closed:
                    if self._file_lock_depth:
                        fcntl.flock(self._file_lock.fileno(), fcntl.LOCK_UN)
                    self._file_lock_depth = 0
                self._lock.release()

    def delete(self, key: str) -> None:
        with self._locked(reload=True):
            if key not in self._data:
                return
            del self._data[key]
            self._save_locked()

    def keys(self) -> list[str]:
        with self._locked(reload=True):
            return sorted(self._data)

    def count(self) -> int:
        with self._locked(reload=True):
            return len(self._data)

    def clear(self) -> None:
        with self._locked():
            self._data = {}
            self._save_locked()

    def close(self) -> None:
        with self._locked():
            if self._closed:
                return
            self._save_locked()
            self._closed = True
            self._file_lock.close()

    def __del__(self) -> None:
        file_lock = getattr(self, "_file_lock", None)
        if file_lock is not None and not getattr(self, "_closed", True):
            self._closed = True
            file_lock.close()

    def _save_locked(self) -> None:
        raw = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        temporary = f"{self._path}.tmp-{uuid.uuid4().hex}"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(raw)
        os.replace(temporary, self._path)
