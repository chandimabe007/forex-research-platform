"""Single-instance guard for the capture service.

Two capture processes appending to the same partition would interleave
writes and corrupt the gap picture. The lock file carries the owning PID;
a stale lock (dead PID) is reported so the operator can clear it — the
service itself never steals a live lock.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class CaptureLocked(RuntimeError):
    """Another capture instance holds the lock."""


class CaptureLock:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            holder = self._read_pid()
            if holder is not None and self._pid_alive(holder):
                raise CaptureLocked(
                    f"capture already running (pid {holder}, lock {self._path}); "
                    "stop it or remove the stale lock file after verifying"
                )
        fd = os.open(self._path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                self._path.unlink()
            except FileNotFoundError:
                pass

    def __enter__(self) -> CaptureLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def _read_pid(self) -> int | None:
        try:
            return int(self._path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform == "win32":
            # Best-effort on Windows without extra deps: check via tasklist.
            import subprocess

            probe = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in (probe.stdout or "")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
