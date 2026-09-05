"""Process-safe project writer and task-allocation locks."""

from __future__ import annotations

import fcntl
import threading
import time
from pathlib import Path
from types import TracebackType


class LockTimeoutError(TimeoutError):
    pass


class AdvisoryFileLock:
    _guard = threading.Lock()
    _thread_locks: dict[str, threading.Lock] = {}

    def __init__(self, path: Path, timeout_seconds: float = 30.0):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._handle = None
        key = str(path.resolve())
        with self._guard:
            self._thread_lock = self._thread_locks.setdefault(key, threading.Lock())

    def __enter__(self) -> "AdvisoryFileLock":
        if not self._thread_lock.acquire(timeout=self.timeout_seconds):
            raise LockTimeoutError(f"timed out acquiring thread lock {self.path}")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a+")
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError(f"timed out acquiring file lock {self.path}")
                    time.sleep(0.01)
        except BaseException:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._thread_lock.release()
            raise

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        self._thread_lock.release()

