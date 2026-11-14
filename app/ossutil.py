"""Upload session drawings to Aliyun OSS. Secrets stay in env, never in responses."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from . import config

log = logging.getLogger("cad-studio.oss")

_BUCKET = None


def configured() -> bool:
    return config.oss_configured()


def _bucket():
    global _BUCKET
    if _BUCKET is not None:
        return _BUCKET
    if not configured():
        raise RuntimeError("对象存储还没有配置。")
    import oss2
    auth = oss2.Auth(config.OSS_ACCESS_KEY_ID, config.OSS_ACCESS_KEY_SECRET)
    # Hangzhou is cross-region from the app host; fail fast instead of blocking init.
    _BUCKET = oss2.Bucket(auth, config.OSS_ENDPOINT, config.OSS_BUCKET, connect_timeout=(15, 300))
    return _BUCKET


def object_key(project_id: str, filename: str, *, kind: str = "source") -> str:
    safe = Path(filename).name or kind
    prefix = config.STUDIO_OSS_PREFIX.strip("/")
    return f"{prefix}/sessions/{project_id}/{kind}/{safe}"


def public_url(key: str) -> str:
    bucket = config.OSS_BUCKET
    region = config.OSS_REGION
    return f"https://{bucket}.{region}.aliyuncs.com/{quote(key, safe='/')}"


def upload_file(path: Path, key: str, *, content_type: Optional[str] = None) -> dict[str, str]:
    import threading
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    elif path.suffix.lower() == ".dxf":
        headers["Content-Type"] = "application/dxf"
    elif path.suffix.lower() == ".dwg":
        headers["Content-Type"] = "application/acad"
    elif path.suffix.lower() == ".pdf":
        headers["Content-Type"] = "application/pdf"
    result: dict[str, object] = {}

    def _put() -> None:
        try:
            import oss2
            size = path.stat().st_size
            # Large drawings go over a slow cross-region link; use multipart so a
            # single stalled write does not abort the whole file.
            if size >= 8 * 1024 * 1024:
                oss2.resumable_upload(
                    _bucket(),
                    key,
                    str(path),
                    headers=headers or None,
                    multipart_threshold=8 * 1024 * 1024,
                    part_size=8 * 1024 * 1024,
                    num_threads=2,
                )
            else:
                _bucket().put_object_from_file(key, str(path), headers=headers or None)
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001 — pass back to caller
            result["err"] = exc

    size = path.stat().st_size
    # ~4s per MB, at least 3 minutes, cap 15 minutes.
    limit = min(900, max(180, int(size / (256 * 1024)) + 60))
    worker = threading.Thread(target=_put, name="oss-put", daemon=True)
    worker.start()
    worker.join(limit)
    if worker.is_alive():
        raise TimeoutError(f"上传到云端超时：{path.name}")
    if "err" in result:
        raise result["err"]  # type: ignore[misc]
    return {"key": key, "url": public_url(key)}


def upload_bytes(data: bytes, key: str, *, content_type: str = "application/octet-stream") -> dict[str, str]:
    _bucket().put_object(key, data, headers={"Content-Type": content_type})
    return {"key": key, "url": public_url(key)}
