"""Background queue for archiving session drawings to Aliyun OSS.

Init and chat never wait on this. Jobs are stored as JSON files so a restart
can resume them. One worker process holds an exclusive lock; extra app
workers leave the queue alone.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, filelock

log = logging.getLogger("cad-studio.archive")

SHANGHAI = timezone(timedelta(hours=8))
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (5, 15, 45, 120, 300)

_WORKER: Optional[threading.Thread] = None
_STOP = threading.Event()
_WAKE = threading.Event()
_LOCK = threading.Lock()
_LOCK_FD = None
_UPLOADER: Optional[Callable[..., dict[str, str]]] = None


def _now() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _now_ts() -> float:
    return time.time()


def _data_root() -> Path:
    base = Path(config.CAD_DATA_DIR) if config.CAD_DATA_DIR else (config.STUDIO_ROOT / "data")
    base.mkdir(parents=True, exist_ok=True)
    return base


def queue_root() -> Path:
    root = _data_root() / "archive-queue"
    for sub in ("pending", "running", "done", "failed"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_job(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("could not read archive job %s", path)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _job_path(folder: str, project_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in project_id)
    return queue_root() / folder / f"{safe}.json"


def enqueue(
    project_id: str,
    files: list[dict[str, str]],
    *,
    kind: str = "session",
    name: Optional[str] = None,
) -> Optional[str]:
    """Queue a source/DXF archive. Safe to call repeatedly for the same project."""
    wanted = [
        {"path": str(item["path"]), "kind": str(item.get("kind") or "source"), "name": str(item.get("name") or Path(item["path"]).name)}
        for item in files
        if item.get("path")
    ]
    if not project_id or not wanted:
        return None
    with _LOCK:
        job = {
            "id": f"arc-{project_id}",
            "project_id": project_id,
            "kind": kind or "session",
            "name": name or project_id,
            "files": wanted,
            "status": "pending",
            "attempts": 0,
            "max_attempts": MAX_ATTEMPTS,
            "next_attempt_at": _now_ts(),
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        running = _job_path("running", project_id)
        pending = _job_path("pending", project_id)
        if running.is_file():
            # A run is in flight; leave a fresh pending job to be picked afterwards.
            existing = _read_job(pending) or {}
            job["created_at"] = existing.get("created_at") or job["created_at"]
        elif pending.is_file():
            existing = _read_job(pending) or {}
            job["created_at"] = existing.get("created_at") or job["created_at"]
            job["attempts"] = int(existing.get("attempts") or 0)
        _atomic_write(pending, job)
    _WAKE.set()
    log.info("queued oss archive for %s (%s files)", project_id, len(wanted))
    return job["id"]


def start_worker(*, uploader: Optional[Callable[..., dict[str, str]]] = None) -> bool:
    """Start the single background worker. Returns False if another process holds the lock."""
    global _WORKER, _LOCK_FD, _UPLOADER
    with _LOCK:
        if uploader is not None:
            _UPLOADER = uploader
        if _WORKER is not None and _WORKER.is_alive():
            return True
        queue_root()
        lock_path = queue_root() / "worker.lock"
        fd = filelock.acquire(lock_path)
        if fd is None:
            log.info("archive queue worker already running in another process")
            return False
        fd.seek(0)
        fd.truncate()
        fd.write(f"pid={os_getpid()} started={_now()}\n".encode("utf-8"))
        fd.flush()
        _LOCK_FD = fd
        _recover_running()
        _STOP.clear()
        _WAKE.set()
        _WORKER = threading.Thread(target=_loop, name="oss-archive-queue", daemon=True)
        _WORKER.start()
        log.info("archive queue worker started")
        return True


def os_getpid() -> int:
    import os
    return os.getpid()


def stop_worker(timeout: float = 5.0) -> None:
    global _WORKER, _LOCK_FD
    _STOP.set()
    _WAKE.set()
    thread = _WORKER
    if thread is not None and thread.is_alive():
        thread.join(timeout)
    _WORKER = None
    if _LOCK_FD is not None:
        filelock.release(_LOCK_FD)
        _LOCK_FD = None


def counts() -> dict[str, int]:
    root = queue_root()
    return {
        "pending": len(list((root / "pending").glob("*.json"))),
        "running": len(list((root / "running").glob("*.json"))),
        "done": len(list((root / "done").glob("*.json"))),
        "failed": len(list((root / "failed").glob("*.json"))),
    }


def _recover_running() -> None:
    root = queue_root()
    for path in (root / "running").glob("*.json"):
        job = _read_job(path)
        if not job:
            path.unlink(missing_ok=True)
            continue
        job["status"] = "pending"
        job["error"] = job.get("error") or "进程中断，重新排队。"
        job["updated_at"] = _now()
        pending = _job_path("pending", job.get("project_id") or path.stem)
        if pending.is_file():
            # Keep the newer pending payload (it may have fresher files).
            path.unlink(missing_ok=True)
            continue
        _atomic_write(pending, job)
        path.unlink(missing_ok=True)
        log.info("requeued interrupted archive job %s", job.get("project_id"))


def _next_pending() -> Optional[Path]:
    now = _now_ts()
    ready: list[tuple[float, Path]] = []
    for path in (queue_root() / "pending").glob("*.json"):
        job = _read_job(path)
        if not job:
            continue
        when = float(job.get("next_attempt_at") or 0)
        if when <= now:
            ready.append((when, path))
    if not ready:
        return None
    ready.sort(key=lambda item: item[0])
    return ready[0][1]


def _loop() -> None:
    while not _STOP.is_set():
        _WAKE.clear()
        try:
            path = _next_pending()
            if path is None:
                _WAKE.wait(1.0)
                continue
            _process_path(path)
        except Exception:
            log.exception("archive queue loop error")
            time.sleep(1.0)


def _process_path(pending_path: Path) -> None:
    job = _read_job(pending_path)
    if not job:
        pending_path.unlink(missing_ok=True)
        return
    project_id = job.get("project_id") or pending_path.stem
    running = _job_path("running", project_id)
    with _LOCK:
        if not pending_path.is_file():
            return
        job["status"] = "running"
        job["updated_at"] = _now()
        _atomic_write(running, job)
        pending_path.unlink(missing_ok=True)
    try:
        _run_job(job)
        job["status"] = "done"
        job["error"] = None
        job["finished_at"] = _now()
        job["updated_at"] = _now()
        with _LOCK:
            _atomic_write(_job_path("done", project_id), job)
            running.unlink(missing_ok=True)
            _job_path("failed", project_id).unlink(missing_ok=True)
        log.info("oss archive done for %s", project_id)
    except Exception as exc:  # noqa: BLE001 — recorded on the job
        job["attempts"] = int(job.get("attempts") or 0) + 1
        job["error"] = str(exc)
        job["updated_at"] = _now()
        log.exception("oss archive failed for %s (attempt %s)", project_id, job["attempts"])
        with _LOCK:
            running.unlink(missing_ok=True)
            if job["attempts"] >= int(job.get("max_attempts") or MAX_ATTEMPTS):
                job["status"] = "failed"
                _atomic_write(_job_path("failed", project_id), job)
                _job_path("pending", project_id).unlink(missing_ok=True)
            else:
                delay = BACKOFF_SECONDS[min(job["attempts"] - 1, len(BACKOFF_SECONDS) - 1)]
                job["status"] = "pending"
                job["next_attempt_at"] = _now_ts() + delay
                # A newer enqueue may already be sitting in pending; don't overwrite it.
                if not _job_path("pending", project_id).is_file():
                    _atomic_write(_job_path("pending", project_id), job)


def _run_job(job: dict[str, Any]) -> None:
    from . import db, ossutil, projects

    upload = _UPLOADER or ossutil.upload_file
    if _UPLOADER is None and not ossutil.configured():
        job["skipped"] = True
        job["error"] = "云端存储尚未配置，图纸暂存在服务器本地。"
        log.info("skip oss archive for %s: not configured", job.get("project_id"))
        return

    fields: dict[str, Any] = {}
    for item in job.get("files") or []:
        path = Path(item.get("path") or "")
        if not path.is_file():
            raise FileNotFoundError(f"待归档文件不在了：{path.name or path}")
        kind = item.get("kind") or "source"
        name = item.get("name") or path.name
        rec = upload(path, ossutil.object_key(job["project_id"], name, kind=kind))
        if kind == "dxf":
            fields["oss_dxf_key"] = rec["key"]
            fields["oss_dxf_url"] = rec["url"]
        else:
            fields["oss_source_key"] = rec["key"]
            fields["oss_source_url"] = rec["url"]
    job["oss"] = fields
    if not fields:
        return
    project_id = job["project_id"]
    kind = job.get("kind") or "session"
    if kind == "session":
        projects.update_session(project_id, fields)
        return
    db.update_session(
        project_id,
        {
            "id": project_id,
            "name": job.get("name") or project_id,
            "kind": kind,
            **fields,
        },
    )


def wait_idle(timeout: float = 10.0) -> bool:
    """Test helper: wait until no pending/running jobs remain."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        stats = counts()
        if stats["pending"] == 0 and stats["running"] == 0:
            return True
        time.sleep(0.05)
    return False
