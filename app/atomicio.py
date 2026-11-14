"""File writes that survive Windows sharing violations.

POSIX lets you rename over a file another process has open; Windows does not, and
raises ``PermissionError`` (WinError 5) instead. Every status / session / chat
write here is paired with a polling reader, so the plain tmp+replace sequence
fails intermittently on Windows while being perfectly safe on Linux.

Three cooperating pieces:

* ``replace_with_retry`` keeps the atomic rename and waits out short-lived locks.
* ``write_text_atomic`` writes the tmp file, renames, and - for files whose loss
  would matter - falls back to an in-place write as a last resort. Callers that
  can afford to skip a write (progress status is rewritten on the next tick) pass
  ``required=False`` so a failure never truncates a file a reader is using.
* ``read_text_tolerant`` retries the read side, so a reader that collides with an
  in-place write sees the new content instead of an error.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

# WinError 5 (access denied) and 32 (sharing violation).
_SHARING_ERRNOS = {13, 32}
_SHARING_WINERRORS = {5, 32}

# Windows: writers are infrequent and polling readers hold handles briefly, so a
# short burst of retries clears almost every collision.
if os.name == "nt":
    WRITE_ATTEMPTS, WRITE_DELAY = 40, 0.02
    READ_ATTEMPTS, READ_DELAY = 20, 0.01
else:
    WRITE_ATTEMPTS, WRITE_DELAY = 3, 0.01
    READ_ATTEMPTS, READ_DELAY = 3, 0.01


def _is_sharing_violation(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "winerror", None) in _SHARING_WINERRORS:
        return True
    return exc.errno in _SHARING_ERRNOS


def replace_with_retry(
    tmp: Path, dest: Path, *, attempts: int = WRITE_ATTEMPTS, delay: float = WRITE_DELAY
) -> None:
    """Move ``tmp`` onto ``dest``, waiting out transient Windows file locks."""
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except OSError as exc:
            if not _is_sharing_violation(exc):
                raise
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
    assert last is not None
    raise last


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def write_text_atomic(
    path: Path,
    text: str,
    encoding: str = "utf-8",
    *,
    required: bool = True,
    attempts: int = WRITE_ATTEMPTS,
    delay: float = WRITE_DELAY,
) -> bool:
    """Write ``text`` to ``path``, replacing it atomically when possible.

    Returns True when the content landed. With ``required=False`` a persistent
    lock makes the write a no-op (returns False) instead of truncating the file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    try:
        replace_with_retry(tmp, path, attempts=attempts, delay=delay)
        return True
    except OSError as exc:
        if not required:
            # Rewritten on the next tick anyway; never clobber a live reader.
            log.debug("skipped non-critical write to %s: %s", path, exc)
            _unlink_quietly(tmp)
            return False
        # The content matters: write through the handle a reader still holds open.
        log.warning("in-place write to %s after rename failed: %s", path, exc)
        path.write_text(text, encoding=encoding)
        _unlink_quietly(tmp)
        return True


def read_text_tolerant(
    path: Path,
    encoding: str = "utf-8",
    *,
    attempts: int = READ_ATTEMPTS,
    delay: float = READ_DELAY,
) -> str:
    """Read ``path``, retrying while a writer briefly holds it open."""
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return Path(path).read_text(encoding=encoding)
        except OSError as exc:
            if not _is_sharing_violation(exc):
                raise
            last = exc
            if attempt + 1 < attempts:
                time.sleep(delay)
    assert last is not None
    raise last
