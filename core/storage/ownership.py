"""Exclusive local API authority, backed by a process-held OS file lock.

The lock file is intentionally never deleted: unlinking it would let another
process lock a different inode while the first still owns the original. A stale
file is harmless; closing the descriptor or process exit releases ownership.
This is for a local filesystem, not distributed fencing or a network share.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


class DataDirectoryInUseError(RuntimeError):
    """Another local API instance already owns this data directory."""


class DataDirectoryOwnership:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise DataDirectoryInUseError(f"Already own data directory: {self.directory}")
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / ".api-owner.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # Descriptors are non-inheritable across exec by Python default.
            # No PID parsing, expiry, or lock-file deletion is involved.
            if sys.platform == "win32":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if isinstance(exc, (BlockingIOError, PermissionError)):
                raise DataDirectoryInUseError(
                    f"Data directory is already owned by another local API: {self.directory}"
                ) from exc
            raise
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        """Release after API shutdown and all of its job execution has stopped."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


__all__ = ["DataDirectoryInUseError", "DataDirectoryOwnership"]
