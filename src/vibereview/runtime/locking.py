"""Process-safe project writer and task-allocation locks."""

from __future__ import annotations

import fcntl
import os
import stat
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType


class LockTimeoutError(TimeoutError):
    pass


class AdvisoryFileLock:
    _guard = threading.Lock()
    _thread_locks: dict[str, threading.Lock] = {}

    def __init__(
        self,
        path: Path,
        timeout_seconds: float = 30.0,
        *,
        directory_opener: Callable[[], int] | None = None,
    ):
        self.path = path
        self.timeout_seconds = timeout_seconds
        self._directory_opener = directory_opener
        self._directory_descriptor: int | None = None
        self._handle = None
        # Lock identity is lexical. Resolving here would follow an attacker-
        # controlled lock or parent symlink before the no-follow open below.
        key = str(path.absolute())
        with self._guard:
            self._thread_lock = self._thread_locks.setdefault(key, threading.Lock())

    def __enter__(self) -> "AdvisoryFileLock":
        if not self._thread_lock.acquire(timeout=self.timeout_seconds):
            raise LockTimeoutError(f"timed out acquiring thread lock {self.path}")
        try:
            if self._directory_opener is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._directory_descriptor = os.open(
                    self.path.parent,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | os.O_CLOEXEC,
                )
            else:
                self._directory_descriptor = self._directory_opener()
            descriptor = os.open(
                self.path.name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=self._directory_descriptor,
            )
            try:
                info = os.fstat(descriptor)
                current = os.stat(
                    self.path.name,
                    dir_fd=self._directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or not stat.S_ISREG(current.st_mode)
                    or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise OSError(f"lock file is unsafe: {self.path}")
                self._handle = os.fdopen(descriptor, "r+b")
                descriptor = -1
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
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
            if self._directory_descriptor is not None:
                os.close(self._directory_descriptor)
                self._directory_descriptor = None
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
        if self._directory_descriptor is not None:
            os.close(self._directory_descriptor)
            self._directory_descriptor = None
        self._thread_lock.release()
