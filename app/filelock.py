"""Cross-platform advisory file lock used to elect a single worker process.

POSIX uses ``fcntl.flock``. Windows has no flock; ``msvcrt.locking`` locks a
byte range, so the file is kept at least one byte long and byte 0 is locked.

``acquire`` never blocks: it returns ``None`` when another process already
holds the lock. The returned handle is the open binary file object; pass it
back to ``release`` to unlock and close.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Optional

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_PLACEHOLDER = b"\0"


def acquire(lock_path: Path | str) -> Optional[IO[bytes]]:
    """Try to take an exclusive lock. Returns the handle, or None if busy."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            # msvcrt locks a byte range and fails on an empty file; make sure
            # byte 0 exists before locking it.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(_PLACEHOLDER)
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, PermissionError, OSError):
        handle.close()
        return None
    return handle


def release(handle: Optional[IO[bytes]]) -> None:
    """Unlock and close a handle returned by ``acquire``. Safe to call twice."""
    if handle is None:
        return
    try:
        if os.name == "nt":
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    finally:
        try:
            handle.close()
        except Exception:
            pass
