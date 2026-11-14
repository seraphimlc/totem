"""Prepare a drawing session: convert, first index, preview cache, then unlock chat."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import ezdxf
from ezdxf import bbox as ezbbox

from . import cad_render, config, known, projects
from . import indexing

log = logging.getLogger("cad-studio.init")

SHANGHAI = timezone(timedelta(hours=8))
MAX_UPLOAD_BYTES = 80 * 1024 * 1024
ALLOWED_SUFFIX = {".dwg", ".dxf"}

# High-level steps shown in the UI. Cloud archive is a background
# queue and is not part of this checklist.
STEPS = (
    ("save", "保存图纸"),
    ("index", "建立索引"),
    ("preview", "准备预览"),
)

_LOCKS: dict[str, threading.Lock] = {}
_META_LOCK = threading.Lock()
_THREADS: dict[str, threading.Thread] = {}
_PROGRESS_WRITTEN: dict[str, float] = {}

# Overall bar spans. Save is usually brief; index and preview get the visible range.
_STEP_SPAN = {
    "save": (0.0, 10.0),
    "index": (10.0, 55.0),
    "preview": (55.0, 100.0),
}


def report_progress(
    proj: dict[str, Any],
    st: dict[str, Any],
    *,
    step: str,
    current: int,
    total: int,
    message: Optional[str] = None,
    flush: bool = False,
) -> None:
    """Update the precise init bar. Writes to disk at most every 200ms, unless flushed."""
    total_n = max(int(total or 1), 1)
    current_n = max(0, min(int(current or 0), total_n))
    start, end = _STEP_SPAN.get(step, (0.0, 100.0))
    percent = start + (end - start) * (current_n / total_n)
    if st.get("ready") or st.get("state") == "ready":
        percent = 100.0
    st["progress"] = {
        "percent": round(percent, 1),
        "current": current_n,
        "total": total_n,
        "step": step,
    }
    if message:
        st["message"] = message
    pid = str(proj.get("id") or "")
    now = time.monotonic()
    last = _PROGRESS_WRITTEN.get(pid, 0.0)
    if flush or (now - last) >= 0.2:
        write_status(proj, st)
        _PROGRESS_WRITTEN[pid] = now


def _now() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _job_lock(project_id: str) -> threading.Lock:
    with _META_LOCK:
        lock = _LOCKS.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[project_id] = lock
        return lock


def studio_dir(proj: dict[str, Any]) -> Path:
    root = Path(proj["root"]) if proj.get("root") else None
    if not root:
        raise ValueError("项目目录还不存在。")
    path = root / ".studio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def status_path(proj: dict[str, Any]) -> Path:
    return studio_dir(proj) / "init.json"


def default_status(message: str = "尚未开始准备") -> dict[str, Any]:
    return {
        "schema": "cad-nl-studio-init/v1",
        "state": "pending",
        "phase": "pending",
        "message": message,
        "ready": False,
        "error": None,
        "done": 0,
        "total": len(STEPS),
        "steps": [
            {"id": sid, "label": label, "state": "pending"} for sid, label in STEPS
        ],
        "progress": {"percent": 0, "current": 0, "total": 1, "step": None},
        "updated_at": _now(),
    }


def read_status(proj: dict[str, Any]) -> dict[str, Any]:
    path = None
    try:
        path = status_path(proj)
    except Exception:
        return default_status("项目目录还不存在。")
    if not path.is_file():
        # Already-indexed built-in drawings still need a preview warmup.
        if _dxf_ready(proj) and _index_ready(proj):
            data = default_status("图纸已在，正在准备预览。")
            _set_step(data, "save", "done")
            _set_step(data, "index", "done")
            data["phase"] = "preview"
            return _normalize_steps(data)
        return default_status()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_status("准备状态读不出来，将重新开始。")
    if not isinstance(data, dict):
        return default_status()
    data.setdefault("ready", data.get("state") == "ready")
    return _normalize_steps(data)


def write_status(proj: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    data["updated_at"] = _now()
    path = status_path(proj)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data


def _set_step(data: dict[str, Any], step_id: str, state: str, message: Optional[str] = None) -> None:
    for step in data.get("steps") or []:
        if step.get("id") == step_id:
            step["state"] = state
    if message:
        data["message"] = message
    data["phase"] = step_id
    done = sum(1 for s in (data.get("steps") or []) if s.get("state") == "done")
    data["done"] = done
    data["total"] = len(data.get("steps") or STEPS)


def _normalize_steps(data: dict[str, Any]) -> dict[str, Any]:
    """Drop retired steps such as cloud upload, and keep counts aligned."""
    by_id = {s.get("id"): s for s in (data.get("steps") or []) if isinstance(s, dict)}
    data["steps"] = [
        {
            "id": sid,
            "label": label,
            "state": (by_id.get(sid) or {}).get("state") or "pending",
        }
        for sid, label in STEPS
    ]
    if data.get("phase") == "upload":
        if data.get("ready") or data.get("state") == "ready":
            data["phase"] = "ready"
        else:
            data["phase"] = next((s["id"] for s in data["steps"] if s.get("state") != "done"), "preview")
    data["total"] = len(STEPS)
    data["done"] = sum(1 for s in data["steps"] if s.get("state") == "done")
    return data


def is_ready(proj: dict[str, Any]) -> bool:
    st = read_status(proj)
    return bool(st.get("ready") or st.get("state") == "ready")


def _existing_dxf(proj: dict[str, Any]) -> Optional[Path]:
    candidates: list[Path] = []
    if proj.get("dxf_path"):
        candidates.append(Path(proj["dxf_path"]))
    root = Path(proj["root"]) if proj.get("root") else None
    if root:
        candidates.append(root / (proj.get("deliverable") or "out/source.dxf"))
        out = root / "out"
        if out.is_dir():
            candidates.extend(sorted(out.glob("*.dxf")))
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file() and path.suffix.lower() == ".dxf" and path.stat().st_size > 32:
            return path
    return None


def _dxf_ready(proj: dict[str, Any]) -> bool:
    return _existing_dxf(proj) is not None


def _index_path(proj: dict[str, Any]) -> Path:
    if proj.get("index_json"):
        return Path(proj["index_json"])
    return Path(proj["root"]) / "index.json"


def _index_ready(proj: dict[str, Any]) -> bool:
    """True when the full drawing index is in SQLite (or a leftover index.json can be imported)."""
    try:
        data = projects.load_index(proj)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("sheets") or data.get("rooms") or data.get("layers"))


def _preview_required_regions(proj: dict[str, Any]) -> list[dict[str, Any]]:
    regions = projects.iter_regions(proj)
    if not regions:
        return []
    default_id = projects.default_region_id(proj, regions)
    default = next((r for r in regions if r["id"] == default_id), regions[0])
    same = [r for r in regions if r["kind"] == "room" and r.get("sheet_id") == default.get("sheet_id")]
    required = [default]
    required.extend(same[:20])
    return required


def _required_previews_ready(proj: dict[str, Any]) -> bool:
    if not _dxf_ready(proj) or not _index_ready(proj):
        return False
    try:
        from . import apply
        dxf = apply.current_dxf(proj)
        required = _preview_required_regions(proj)
        if not required:
            ext = None
            try:
                doc = cad_render.load_doc(dxf, encoding=proj.get("encoding") or "utf-8")
                box = ezbbox.extents(doc.modelspace(), fast=True)
                if box:
                    ext = [float(box.extmin.x), float(box.extmin.y), float(box.extmax.x), float(box.extmax.y)]
            except Exception:
                return False
            return bool(ext) and cad_render.cached_preview_exists(dxf, ext)
        return all(cad_render.cached_preview_exists(dxf, r["bbox"]) for r in required)
    except Exception:
        return False


def _first_incomplete_step(proj: dict[str, Any], *, force: bool = False) -> Optional[str]:
    if force:
        return "save"
    if not _dxf_ready(proj):
        return "save"
    if not _index_ready(proj):
        return "index"
    if not _required_previews_ready(proj):
        return "preview"
    return None


def _step_label(step_id: str) -> str:
    return dict(STEPS).get(step_id, step_id)


def _iter_projects() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in projects.list_specs():
        try:
            out.append(projects.resolve_project(spec["id"]))
        except Exception:
            log.exception("could not resolve project %s", spec.get("id"))
    return out


def mark_running_interrupted() -> int:
    """Called on shutdown so the next start can resume from the last finished step."""
    count = 0
    for proj in _iter_projects():
        try:
            if not proj.get("root"):
                continue
            st = read_status(proj)
            if st.get("ready") or st.get("state") != "running":
                continue
            nxt = _first_incomplete_step(proj)
            label = _step_label(nxt or "preview")
            st["state"] = "interrupted"
            st["ready"] = False
            st["message"] = f"准备被中断了，将从「{label}」继续。"
            write_status(proj, st)
            count += 1
        except Exception:
            log.exception("could not mark init interrupted for %s", proj.get("id"))
    if count:
        log.info("marked %s running init job(s) interrupted", count)
    return count


def resume_unfinished() -> int:
    """Resume sessions left pending, running, or interrupted after a restart."""
    count = 0
    for proj in _iter_projects():
        try:
            if not proj.get("root") or not Path(proj["root"]).is_dir():
                continue
            st = read_status(proj)
            if st.get("ready") or st.get("state") == "error":
                continue
            if st.get("state") in {"running", "interrupted", "pending"}:
                start(proj)
                count += 1
        except Exception:
            log.exception("could not resume init for %s", proj.get("id"))
    if count:
        log.info("resumed %s unfinished init job(s)", count)
    return count


def public_status(proj: dict[str, Any]) -> dict[str, Any]:
    st = read_status(proj)
    ready = bool(st.get("ready") or st.get("state") == "ready")
    state = st.get("state") or "pending"
    return {
        "state": state,
        "phase": st.get("phase") or "pending",
        "message": st.get("message") or "",
        "ready": ready,
        "error": st.get("error"),
        "done": int(st.get("done") or 0),
        "total": int(st.get("total") or len(STEPS)),
        "steps": st.get("steps") or [],
        "progress": st.get("progress") or {"percent": 0, "current": 0, "total": 1, "step": None},
        "resumed": bool(st.get("resumed")),
        "resumable": (not ready) and state in {"error", "interrupted", "running", "pending"},
        "updated_at": st.get("updated_at"),
    }


def start(proj: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Start or resume initialization in the background. Safe to call repeatedly."""
    st = read_status(proj)
    if st.get("ready") and not force:
        return public_status(proj)
    pid = proj["id"]
    with _META_LOCK:
        alive = _THREADS.get(pid)
        if alive and alive.is_alive() and not force:
            return public_status(proj)
        if not st.get("ready"):
            st["state"] = "running"
            st["error"] = None
            nxt = _first_incomplete_step(proj, force=force)
            if not force and nxt and nxt != "save":
                st["resumed"] = True
                st["message"] = f"上次准备中断了，正在从「{_step_label(nxt)}」继续。"
            else:
                st["resumed"] = False
                if not st.get("message"):
                    st["message"] = "开始准备这张图。"
            write_status(proj, st)
        thread = threading.Thread(target=_run, args=(pid, force), name=f"init-{pid}", daemon=True)
        _THREADS[pid] = thread
        thread.start()
    return public_status(proj)


def _run(project_id: str, force: bool) -> None:
    lock = _job_lock(project_id)
    if not lock.acquire(blocking=False):
        return
    try:
        proj = projects.resolve_project(project_id)
        st = read_status(proj)
        if st.get("ready") and not force:
            return
        st["state"] = "running"
        st["ready"] = False
        st["error"] = None
        write_status(proj, st)
        _prepare(proj, st, force=force)
        proj = projects.resolve_project(project_id)
        st = read_status(proj)
        st["state"] = "ready"
        st["ready"] = True
        st["phase"] = "ready"
        st["message"] = "准备完成，可以开始对话了。"
        _set_step(st, "save", "done")
        _set_step(st, "index", "done")
        _set_step(st, "preview", "done")
        st["progress"] = {"percent": 100, "current": 1, "total": 1, "step": "preview"}
        write_status(proj, st)
        # Keep warming remaining regions after unlock.
        try:
            _warm_remaining(proj)
        except Exception:
            log.exception("background preview warm failed for %s", project_id)
    except Exception as exc:
        log.exception("init failed for %s", project_id)
        try:
            proj = projects.resolve_project(project_id)
            st = read_status(proj)
            st["state"] = "error"
            st["ready"] = False
            st["error"] = str(exc)
            st["message"] = str(exc)
            write_status(proj, st)
        except Exception:
            log.exception("could not record init error for %s", project_id)
    finally:
        lock.release()


def _prepare(proj: dict[str, Any], st: dict[str, Any], *, force: bool = False) -> None:
    if force:
        for sid, _label in STEPS:
            _set_step(st, sid, "pending")
        st["resumed"] = False

    nxt = _first_incomplete_step(proj, force=force)
    if not force and nxt and nxt != "save":
        st["resumed"] = True
        report_progress(
            proj,
            st,
            step=nxt,
            current=0,
            total=1,
            message=f"上次准备中断了，正在从「{_step_label(nxt)}」继续。",
            flush=True,
        )

    if not force and _dxf_ready(proj):
        dxf = _existing_dxf(proj)
        assert dxf is not None
        _set_step(st, "save", "done", "图纸已保存。")
        report_progress(proj, st, step="save", current=1, total=1, message="图纸已保存。", flush=True)
    else:
        _set_step(st, "save", "running", "正在确认图纸文件。")
        report_progress(proj, st, step="save", current=0, total=1, message="正在确认图纸文件。", flush=True)
        dxf = _ensure_dxf(proj, st)
        _set_step(st, "save", "done", "图纸已保存。")
        report_progress(proj, st, step="save", current=1, total=1, message="图纸已保存。", flush=True)
    _enqueue_archive(proj, dxf)

    if not force and _index_ready(proj):
        report_progress(proj, st, step="index", current=8, total=10, message="索引已就绪，正在载入。", flush=True)
        _persist_index(proj)
        _set_step(st, "index", "done", "索引已就绪。")
        report_progress(proj, st, step="index", current=10, total=10, message="索引已就绪。", flush=True)
    else:
        _set_step(st, "index", "running", "正在建立索引。")
        report_progress(proj, st, step="index", current=0, total=10, message="正在识别图纸编码。", flush=True)
        matched = known.match_known_drawing(dxf)
        encoding = (matched or {}).get("encoding") or known.detect_dxf_encoding(dxf)
        if proj.get("kind") == "session":
            projects.update_session(proj["id"], {"encoding": encoding})
            proj["encoding"] = encoding

        def on_index(current: int, total: int, message: str, flush: bool = False) -> None:
            report_progress(proj, st, step="index", current=current, total=total, message=message, flush=flush)

        if matched:
            apply_known_index(proj, dxf, matched, on_progress=on_index)
        else:
            build_first_index(
                dxf,
                Path(proj["root"]),
                proj.get("name") or proj["id"],
                encoding=encoding,
                project_id=proj["id"],
                on_progress=on_index,
            )
            report_progress(proj, st, step="index", current=9, total=10, message="正在选定默认图幅。", flush=True)
            _pick_default_sheet(proj)
        _set_step(st, "index", "done", "索引已写入数据库。")
        report_progress(proj, st, step="index", current=10, total=10, message="索引已写入数据库。", flush=True)

    # Re-resolve so regions come from the new index.
    proj = projects.resolve_project(proj["id"])
    _set_step(st, "preview", "running", "正在准备默认图幅预览。")
    report_progress(proj, st, step="preview", current=0, total=1, message="正在准备默认图幅预览。", flush=True)
    _warm_required(proj, st)
    _set_step(st, "preview", "done", "默认预览已准备好。")
    report_progress(proj, st, step="preview", current=1, total=1, message="默认预览已准备好。", flush=True)


def _ensure_dxf(proj: dict[str, Any], st: dict[str, Any]) -> Path:
    existing = _existing_dxf(proj)
    if existing:
        return existing
    source = Path(proj["root"]) / "source"
    originals = []
    if source.is_dir():
        originals = sorted(source.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not originals:
        raise ValueError("还没有找到上传的图纸文件。")
    src = originals[0]
    dest = Path(proj["root"]) / (proj.get("deliverable") or "out/source.dxf")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.suffix.lower() == ".dxf" and dest.stat().st_size > 32:
        return dest
    if src.suffix.lower() == ".dxf":
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return dest
    if src.suffix.lower() == ".dwg":
        report_progress(proj, st, step="save", current=0, total=1, message="正在把 DWG 转成 DXF。", flush=True)
        convert_dwg(src, dest)
        return dest
    raise ValueError("只接受 DWG 或 DXF 图纸。")


def find_dwg2dxf() -> Optional[str]:
    env = (os.getenv("CAD_DWG2DXF") or "").strip()
    candidates = [
        env,
        "/tmp/cad-tools/prefix/bin/dwg2dxf",
        "/opt/cad-tools/prefix/bin/dwg2dxf",
        "/opt/cad-studio/tools/bin/dwg2dxf",
        "/usr/local/bin/dwg2dxf",
        shutil.which("dwg2dxf") or "",
    ]
    for path in candidates:
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return path
    return None


def convert_dwg(src: Path, dest: Path) -> Path:
    tool = find_dwg2dxf()
    if not tool:
        raise ValueError("服务器目前不能直接读取 DWG。请先在 AutoCAD 里另存为 DXF（建议 AC1021）后再上传。")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = dest.parent / ".convert-tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # dwg2dxf writes next to the source by default; copy first.
    tmp_src = tmp_dir / src.name
    shutil.copy2(src, tmp_src)
    import subprocess
    cmd = [tool, "-y", "-o", str(tmp_dir), str(tmp_src)]
    # Some builds only accept the input path.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception:
        proc = subprocess.run([tool, str(tmp_src)], capture_output=True, text=True, timeout=180)
    produced = list(tmp_dir.glob("*.dxf")) + list(tmp_dir.glob("*.DXF"))
    if not produced:
        raise ValueError("DWG 转换没有成功。请改传 DXF，或检查原文件是否完好。")
    shutil.copy2(produced[0], dest)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    # Normalize to AC1021 when possible.
    try:
        doc = ezdxf.readfile(str(dest))
        try:
            from . import cad_render
            n = cad_render.sanitize_doc_text_backgrounds(doc)
            if n:
                log.info("sanitized %s corrupt text backgrounds in %s", n, dest)
        except Exception:
            log.exception("could not sanitize text backgrounds in %s", dest)
        doc.saveas(str(dest), encoding="utf-8")
    except Exception:
        try:
            doc = ezdxf.readfile(str(dest), encoding="gbk")
            try:
                from . import cad_render
                cad_render.sanitize_doc_text_backgrounds(doc)
            except Exception:
                pass
            doc.saveas(str(dest))
        except Exception:
            log.exception("could not normalize converted dxf")
    return dest


def _pick_default_sheet(proj: dict[str, Any]) -> None:
    try:
        index = projects.load_index(projects.resolve_project(proj["id"]))
        sheets = index.get("sheets") or []
        if not sheets:
            return
        def pref(sh: dict[str, Any]) -> int:
            title = (sh.get("title") or sh.get("id") or "") + (sh.get("kind") or "")
            if _BAD_TITLE.search(title):
                return -20
            score = 0
            if "装饰平面" in title or "平面布置" in title:
                score += 40
            if re.search(r"(详图|平面图|布置图)", title):
                score += 16
            if re.search(r"(1F|一层)", title):
                score += 10
            if "水施-007" in title or "卫生间详图" in title:
                score += 20
            return score
        picked = max(sheets, key=pref)
        if proj.get("kind") == "session":
            projects.update_session(proj["id"], {"default_sheet": picked.get("id")})
    except Exception:
        log.exception("could not set default sheet")



def _persist_scene(proj: dict[str, Any], region: dict[str, Any], *, source: str = "work") -> None:
    try:
        from . import scene
        scene.persist_region_scene(proj, region, source=source)
    except Exception:
        log.exception("could not persist scene for %s %s", proj.get("id"), (region or {}).get("id"))


def _warm_required(proj: dict[str, Any], st: dict[str, Any]) -> None:
    from . import apply

    dxf = apply.current_dxf(proj)
    encoding = proj.get("encoding") or "utf-8"
    regions = projects.iter_regions(proj)
    if not regions:
        # Fall back to model-space extents.
        doc = cad_render.load_doc(dxf, encoding=encoding)
        ext = ezbbox.extents(doc.modelspace(), fast=True)
        if not ext:
            raise ValueError("图纸是空的，无法准备预览。")
        bbox = [float(ext.extmin.x), float(ext.extmin.y), float(ext.extmax.x), float(ext.extmax.y)]
        if cad_render.cached_preview_exists(dxf, bbox):
            _persist_scene(proj, {"id": "整图", "bbox": bbox, "title": "整图", "kind": "sheet"})
            report_progress(proj, st, step="preview", current=1, total=1, message="整图预览已有缓存。", flush=True)
            return
        report_progress(proj, st, step="preview", current=0, total=1, message="正在准备整图预览。", flush=True)
        cad_render.base_preview(dxf, bbox, encoding=encoding)
        _persist_scene(proj, {"id": "整图", "bbox": bbox, "title": "整图", "kind": "sheet"})
        report_progress(proj, st, step="preview", current=1, total=1, message="整图预览已准备好。", flush=True)
        return
    required = _preview_required_regions(proj)
    total = len(required)
    already = sum(1 for r in required if cad_render.cached_preview_exists(dxf, r["bbox"]))
    if already:
        report_progress(
            proj,
            st,
            step="preview",
            current=already,
            total=total,
            message=f"已有 {already}/{total} 张预览，继续准备其余的。" if already < total else f"所需预览已全部就绪（{total}/{total}）。",
            flush=True,
        )
    for i, region in enumerate(required, start=1):
        title = region.get("title") or region["id"]
        if cad_render.cached_preview_exists(dxf, region["bbox"]):
            _persist_scene(proj, region)
            report_progress(
                proj,
                st,
                step="preview",
                current=i,
                total=total,
                message=f"预览已有（{i}/{total}）：{title}",
                flush=True,
            )
            continue
        report_progress(
            proj,
            st,
            step="preview",
            current=i - 1,
            total=total,
            message=f"正在准备预览（{i}/{total}）：{title}",
            flush=True,
        )
        cad_render.base_preview(dxf, region["bbox"], encoding=encoding)
        _persist_scene(proj, region)
        report_progress(
            proj,
            st,
            step="preview",
            current=i,
            total=total,
            message=f"正在准备预览（{i}/{total}）：{title}",
            flush=True,
        )


def _warm_remaining(proj: dict[str, Any]) -> None:
    from . import apply

    if not is_ready(proj):
        return
    dxf = apply.current_dxf(proj)
    encoding = proj.get("encoding") or "utf-8"
    regions = projects.iter_regions(proj)
    default_id = projects.default_region_id(proj, regions)
    default = next((r for r in regions if r["id"] == default_id), None)
    already = set()
    if default:
        already.add(default["id"])
        for r in regions:
            if r["kind"] == "room" and r.get("sheet_id") == default.get("sheet_id"):
                already.add(r["id"])
    for region in regions:
        if region["id"] in already:
            continue
        try:
            cad_render.base_preview(dxf, region["bbox"], encoding=encoding)
            _persist_scene(proj, region)
        except Exception:
            log.exception("warm remaining failed for %s %s", proj["id"], region["id"])



def _store_index(project_id: str, index: dict[str, Any], index_md: str) -> None:
    from . import db
    existed = db.load_index(project_id) is not None
    db.save_index(project_id, index, index_md)
    db.append_changelog(project_id, "更新图纸索引。" if existed else "上传图纸并建立首次索引。")


def _persist_index(proj: dict[str, Any]) -> None:
    # Import leftover index.json / changelog files into SQLite when a row is missing.
    projects.load_index(proj)


def _enqueue_archive(proj: dict[str, Any], dxf: Path) -> None:
    """Put local source + DXF onto the background OSS queue. Never raises."""
    from . import archive_queue

    files: list[dict[str, str]] = []
    source_dir = Path(proj["root"]) / "source"
    if source_dir.is_dir():
        found = [f for f in source_dir.iterdir() if f.is_file()]
        if found:
            src = sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)[0]
            files.append({"path": str(src), "kind": "source", "name": src.name})
    if dxf and dxf.is_file():
        files.append({"path": str(dxf), "kind": "dxf", "name": dxf.name})
    if not files:
        return
    try:
        archive_queue.enqueue(
            proj["id"],
            files,
            kind=str(proj.get("kind") or "session"),
            name=str(proj.get("name") or proj["id"]),
        )
    except Exception:
        log.exception("could not enqueue oss archive for %s", proj.get("id"))


# ---------- first index for a newly uploaded drawing ----------

_SKIP_TEXT = re.compile(
    r"(比例|说明|注：|注意|图号|图名|日期|设计|绘图|审核|会签|项目|建设单位|通常|本图|详见|目录)"
)
_SHEET_TITLE = re.compile(r"(平面图|布置图|详图|系统图|原理图|施工图|给排水|喷淋)")
_BAD_TITLE = re.compile(r"(^(比例|日期|图号|图名|会签|设计|审核|绘图)$|毫米|轴网|标高|图例栏)")
_ROOMISH_LAYER = re.compile(r"(房间|功能|ROOM|FUNC|文字|标注|TEXT|NAME|名称)", re.I)
_FRAME_LAYER = re.compile(r"(BORDER|图框|图框线|FRAME|TITLE|图签)", re.I)
_FRAME_BLOCK = re.compile(r"(SBB|图框|图签|TITLE.?BLOCK|A[0-3]_?FRAME)", re.I)



def _sanitize(obj):
    """Replace lone surrogates and other non-UTF8 text from DXF strings so JSON can be written."""
    if isinstance(obj, str):
        return obj.encode("utf-8", "replace").decode("utf-8")
    if isinstance(obj, dict):
        return {_sanitize(k): _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    if isinstance(obj, tuple):
        return type(obj)(_sanitize(x) for x in obj)
    return obj


def apply_known_index(
    proj: dict[str, Any],
    dxf: Path,
    item: dict[str, Any],
    *,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict[str, Any]:
    def _ping(current: int, total: int, message: str) -> None:
        if on_progress:
            on_progress(current, total, message)

    src_dir = known.known_index_dir(item)
    src_json = src_dir / "index.json"
    if not src_json.is_file():
        raise FileNotFoundError(f"找不到已校对索引：{item.get('id')}")
    _ping(1, 4, "正在读取已校对的图纸索引。")
    root = Path(proj["root"])
    root.mkdir(parents=True, exist_ok=True)
    index = json.loads(src_json.read_text(encoding="utf-8"))
    source = index.get("source") if isinstance(index.get("source"), dict) else {}
    source = dict(source)
    source["applied_known_index"] = item.get("id")
    source["indexed_at"] = _now()
    source["note"] = "上传图纸与已校对索引为同一份文件，沿用该索引中的图名和房间名，未编造名称。"
    index["source"] = source
    _ping(2, 4, "正在整理索引。")
    src_md = src_dir / "INDEX.md"
    if src_md.is_file():
        md = src_md.read_text(encoding="utf-8")
    else:
        md = _index_markdown(index)
    fields = {"encoding": item.get("encoding") or "utf-8"}
    if item.get("default_sheet"):
        fields["default_sheet"] = item["default_sheet"]
    if item.get("default_subview"):
        fields["default_subview"] = item["default_subview"]
    if item.get("dxf_version"):
        fields["dxf_version"] = item["dxf_version"]
    if proj.get("kind") == "session":
        projects.update_session(proj["id"], fields)
        proj.update(fields)
    _ping(3, 4, "正在把索引写入数据库。")
    _store_index(proj["id"], index, md)
    _ping(4, 4, "已套用校对过的图纸索引。")
    return index


def build_first_index(
    dxf_path: Path,
    root: Path,
    name: str,
    *,
    encoding: str = "utf-8",
    project_id: Optional[str] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> dict[str, Any]:
    # Work units inside the index step, so the overall bar can move during long scans.
    READ, LAYERS, INSERTS, BLOCKS, TEXTS, SHEETS, ROOMS, WRITE, STORE = 18, 4, 22, 8, 20, 10, 8, 5, 5
    total = READ + LAYERS + INSERTS + BLOCKS + TEXTS + SHEETS + ROOMS + WRITE + STORE

    def _ping(current: int, message: str) -> None:
        if on_progress:
            on_progress(max(0, min(current, total)), total, message)

    _ping(0, "正在读取图纸。")
    try:
        doc = ezdxf.readfile(str(dxf_path), encoding=encoding)
    except Exception:
        doc = ezdxf.readfile(str(dxf_path))
    msp = doc.modelspace()
    done = READ
    _ping(done, "正在整理图层。")

    layers = []
    for layer in doc.layers:
        try:
            layers.append({"name": layer.dxf.name, "color": int(getattr(layer.dxf, "color", 7) or 7), "on": not layer.is_off(), "frozen": layer.is_frozen()})
        except Exception:
            layers.append({"name": str(getattr(layer.dxf, "name", ""))})
    done += LAYERS
    _ping(done, "正在扫描图块插入（含底图块内部）。")
    raw_inserts = indexing.collect_inserts(doc, msp)
    inserts = raw_inserts
    top_inserts = [rec for rec in raw_inserts if not rec.get("nested")]
    done += INSERTS
    _ping(done, "正在收集文字（含底图块内部）。")

    def _on_texts(current: int, total_texts: int) -> None:
        frac = current / max(total_texts, 1)
        _ping(done + int(TEXTS * frac), f"正在收集文字（{current}/{total_texts}）。")

    texts = indexing.collect_texts(doc, msp, on_progress=_on_texts)
    done += TEXTS
    _ping(done, "正在识别图幅。")
    sheets = _detect_sheets(doc, msp, top_inserts, texts)
    done += SHEETS
    _ping(done, "正在识别房间。")
    rooms = _detect_rooms(texts, sheets, [])
    done += ROOMS
    _ping(done, "正在识别设备符号并写入索引。")
    indexing.enrich_inserts(doc, inserts)
    indexing.assign_locations(inserts, sheets)
    for room in rooms:
        room["equipment_nearby"] = []
    equipment = indexing.equipment_from_inserts(inserts)
    indexing.assign_rooms([rec for rec in equipment if indexing.is_attachable(rec)], rooms)
    blocks = indexing.build_catalog(doc, inserts)
    done += BLOCKS
    assigned_keys = {
        (it.get("block") or it.get("name"), tuple(it.get("xy") or []), it.get("sheet"))
        for room in rooms
        for it in (room.get("equipment_nearby") or [])
    }
    unassigned = [
        it
        for it in equipment
        if (it.get("block") or it.get("name"), tuple(it.get("xy") or []), it.get("sheet")) not in assigned_keys
    ]

    index = {
        "schema": indexing.SCHEMA,

        "project": name,
        "source": {
            "dxf": str(dxf_path.name),
            "indexed_at": _now(),
            "note": "自动索引。房间名和官方块名取自图纸原文；可读名称来自块名、默认组件库、图例或已识别的符号类型，不改官方块名。",
        },
        "sheets": sheets,
        "layers": layers,
        "blocks": blocks,
        "rooms": rooms,
        "equipment": equipment,
        "equipment_unassigned": unassigned,
        "changelog": "sqlite:changelogs",
    }
    root.mkdir(parents=True, exist_ok=True)
    index = _sanitize(index)
    md = _index_markdown(index)
    done += WRITE
    _ping(done, "正在把索引写入数据库。")
    _store_index(project_id or Path(root).name, index, md)
    _ping(total, "索引已建立。")
    return index


def _collect_texts(msp, on_progress: Optional[Callable[[int, int], None]] = None) -> list[dict[str, Any]]:
    out = []
    try:
        total = len(msp)
    except Exception:
        total = 0
    for i, e in enumerate(msp, start=1):
        if on_progress and total and (i == 1 or i == total or i % max(total // 20, 1) == 0):
            on_progress(i, total)
        typ = e.dxftype()
        if typ not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            continue
        try:
            if typ == "MTEXT":
                raw = e.text or e.plain_text()
            else:
                raw = e.dxf.text
            text = re.sub(r"\{\\.*?\}", "", str(raw or ""))
            text = re.sub(r"[\\][A-Za-z].*?;", "", text)
            text = re.sub(r"[{}]", "", text).strip()
            if typ == "MTEXT":
                text = text.replace("\\P", " ").replace("\\p", " ")
            text = re.sub(r"\s+", " ", text).strip()
            if not text:
                continue
            loc = e.dxf.insert if typ != "MTEXT" else e.dxf.insert
            xy = [round(float(loc.x), 3), round(float(loc.y), 3)]
            layer = getattr(e.dxf, "layer", "") or ""
            height = float(getattr(e.dxf, "height", 0) or getattr(e.dxf, "char_height", 0) or 0)
            out.append({"text": text, "xy": xy, "layer": layer, "height": height})
        except Exception:
            continue
    return out


def _detect_sheets(doc, msp, inserts: list[dict[str, Any]], texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for ins in inserts:
        if not _FRAME_BLOCK.search(ins.get("name") or ""):
            continue
        bb = _insert_bbox(doc, msp, ins)
        if not bb:
            continue
        frames.append({"bbox": bb, "sbb_insert": {"block": ins["name"], "xy": ins["xy"]}, "source": f"插入块 {ins['name']}"})
    for e in msp.query("LWPOLYLINE"):
        try:
            layer = e.dxf.layer or ""
            if not e.is_closed and not _FRAME_LAYER.search(layer):
                continue
            pts = list(e.get_points("xy"))
            if len(pts) < 4:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            bb = [min(xs), min(ys), max(xs), max(ys)]
            w, h = bb[2] - bb[0], bb[3] - bb[1]
            if w < 15000 or h < 10000:
                continue
            if not e.is_closed and not _FRAME_LAYER.search(layer):
                continue
            frames.append({"bbox": [round(v, 3) for v in bb], "source": f"图层 {layer} 的封闭线框"})
        except Exception:
            continue
    frames = _unique_frames(frames)
    sheets = []
    for i, fr in enumerate(frames, start=1):
        title = _title_near(texts, fr["bbox"]) or f"图幅{i}"
        floor = _guess_floor(title)
        sid = _sheet_id(title, i)
        rec = {
            "id": sid,
            "title": title,
            "floor": floor,
            "kind": "",
            "frame": {"bbox": fr["bbox"], "source": fr.get("source") or "自动识别图框"},
        }
        if fr.get("sbb_insert"):
            rec["sbb_insert"] = fr["sbb_insert"]
        sheets.append(rec)
    if not sheets:
        ext = ezbbox.extents(msp, fast=True)
        if ext:
            bb = [round(float(ext.extmin.x), 3), round(float(ext.extmin.y), 3), round(float(ext.extmax.x), 3), round(float(ext.extmax.y), 3)]
            sheets.append({"id": "整图", "title": "整图", "floor": "", "kind": "", "frame": {"bbox": bb, "source": "模型空间范围"}})
    return sheets


def _insert_bbox(doc, msp, ins: dict[str, Any]) -> Optional[list[float]]:
    name = ins.get("name")
    xy = ins.get("xy") or [0, 0]
    try:
        block = doc.blocks.get(name)
        ext = ezbbox.extents(block, fast=True)
        if not ext:
            return None
        sx, sy = 1.0, 1.0
        if ins.get("scale"):
            sx, sy = float(ins["scale"][0] or 1), float(ins["scale"][1] or 1)
        bb = [
            xy[0] + float(ext.extmin.x) * sx,
            xy[1] + float(ext.extmin.y) * sy,
            xy[0] + float(ext.extmax.x) * sx,
            xy[1] + float(ext.extmax.y) * sy,
        ]
        return [round(v, 3) for v in bb]
    except Exception:
        return None


def _unique_frames(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    frames = sorted(frames, key=lambda f: _area(f["bbox"]), reverse=True)
    kept = []
    for fr in frames:
        bb = fr["bbox"]
        cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        dup = False
        for other in kept:
            ob = other["bbox"]
            ocx, ocy = (ob[0] + ob[2]) / 2, (ob[1] + ob[3]) / 2
            if abs(cx - ocx) < 2000 and abs(cy - ocy) < 2000:
                # Prefer the larger outer frame already kept, unless this one is clearly bigger.
                if _area(bb) > _area(ob) * 1.15:
                    kept.remove(other)
                    break
                dup = True
                break
        if not dup:
            kept.append(fr)
    return kept


def _area(bb: list[float]) -> float:
    return max(bb[2] - bb[0], 0) * max(bb[3] - bb[1], 0)


def _title_near(texts: list[dict[str, Any]], bbox: list[float]) -> str:
    # Prefer labels sitting in the title-block strip (right/bottom of the frame).
    xmin, ymin, xmax, ymax = bbox
    w, h = xmax - xmin, ymax - ymin
    zone = [xmax - w * 0.35, ymin, xmax, ymin + h * 0.28]
    candidates = []
    for t in texts:
        s = t["text"]
        if len(s) < 2 or len(s) > 40:
            continue
        if _SKIP_TEXT.search(s) or _BAD_TITLE.search(s):
            continue
        x, y = t["xy"]
        in_zone = zone[0] <= x <= zone[2] and zone[1] <= y <= zone[3]
        in_frame = xmin <= x <= xmax and ymin <= y <= ymax
        if not in_zone and not in_frame:
            continue
        score = min(len(s), 16)
        if _SHEET_TITLE.search(s):
            score += 20
        if in_zone:
            score += 6
        if in_frame and re.search(r"[0-9Ff层平面图详]", s):
            score += 8
        candidates.append((score, s))
    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else ""


def _guess_floor(title: str) -> str:
    m = re.search(r"([1-9]F|[1-9]层|B[1-9]|地下[1-9]|屋顶|RF)", title or "", re.I)
    return m.group(1) if m else ""


def _sheet_id(title: str, i: int) -> str:
    raw = re.sub(r"\s+", "", title or "")
    return raw or f"图幅{i}"


def _detect_rooms(texts: list[dict[str, Any]], sheets: list[dict[str, Any]], inserts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rooms = []
    for t in texts:
        name = t["text"]
        if not _looks_like_room(name, t.get("layer") or ""):
            continue
        sheet = _sheet_for_point(t["xy"], sheets)
        room = {
            "name": name,
            "world_xy": t["xy"],
            "sheet": (sheet or {}).get("id") or (sheet or {}).get("title") or "",
            "floor": (sheet or {}).get("floor") or _guess_floor(name),
            "layer": t.get("layer") or "",
            "source": t.get("source") or "text_label",
            "host_block": t.get("host_block"),
            "host_insert_xy": t.get("host_insert_xy"),
            "local_xy": t.get("local_xy"),
            "equipment_nearby": [],
            "labels_nearby": [],
        }
        rooms.append(room)
    # Attach nearby inserts (don't invent names).
    for room in rooms:
        rx, ry = room["world_xy"]
        nearby = []
        for ins in inserts:
            dx = ins["xy"][0] - rx
            dy = ins["xy"][1] - ry
            if dx * dx + dy * dy <= 8000 * 8000:
                nearby.append({**ins, "dist": round((dx * dx + dy * dy) ** 0.5, 1)})
        nearby.sort(key=lambda it: it.get("dist") or 0)
        room["equipment_nearby"] = nearby[:24]
    return rooms


def _looks_like_room(name: str, layer: str) -> bool:
    if not name or len(name) > 12:
        return False
    if _SKIP_TEXT.search(name):
        return False
    if re.fullmatch(r"[0-9A-Za-z\-_.]+", name):
        return False
    if not re.search(r"[\u4e00-\u9fff]", name):
        return False
    if re.fullmatch(r"[一二三四五六七八九十]+", name):
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", name))
    if cjk < 2:
        return False
    # Prefer room-ish layers, but also accept short CJK labels that look like room names.
    if _ROOMISH_LAYER.search(layer):
        return 2 <= cjk <= 8
    return 2 <= cjk <= 6 and not re.search(r"(轴|线|标高|详图|编号|毫米)", name)


def _sheet_for_point(xy: list[float], sheets: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    x, y = xy
    hits = []
    for sh in sheets:
        bb = (sh.get("frame") or {}).get("bbox") or sh.get("frame")
        if not (isinstance(bb, list) and len(bb) == 4):
            continue
        if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]:
            hits.append((_area(bb), sh))
    hits.sort(key=lambda t: t[0])
    return hits[0][1] if hits else None


def _index_markdown(index: dict[str, Any]) -> str:
    sheets = index.get("sheets") or []
    rooms = index.get("rooms") or []
    blocks = index.get("blocks") or []
    layers = index.get("layers") or []
    lines = [
        f"# {index.get('project') or '图纸'} 索引",
        "",
        "本记录只反映当前图纸状态。变更记录保存在数据库中。",
        "",
        f"- 图幅：{len(sheets)}",
        f"- 房间：{len(rooms)}",
        f"- 命名块：{len(blocks)}",
        f"- 图层：{len(layers)}",
        "",
        "## 图幅",
        "",
    ]
    for sh in sheets:
        bb = (sh.get("frame") or {}).get("bbox")
        bbox = ", ".join(f"{v:.1f}" for v in bb) if bb else "—"
        lines.append(f"- `{sh.get('id')}` {sh.get('title') or ''}  楼层={sh.get('floor') or '—'}  图框=({bbox})")
    lines += ["", "## 房间", ""]
    if not rooms:
        lines.append("（首次索引没有识别到房间名文字。）")
    for room in rooms:
        xy = room.get("world_xy") or [0, 0]
        n = len(room.get("equipment_nearby") or [])
        names = []
        for e in (room.get("equipment_nearby") or [])[:12]:
            lab = e.get("display_name") or e.get("block") or e.get("name")
            names.append(str(lab))
        extra = ("  " + "、".join(names)) if names else ""
        lines.append(f"- {room.get('name')}  图幅={room.get('sheet') or '—'}  坐标=({xy[0]:.1f}, {xy[1]:.1f})  邻近设备={n}{extra}")
    lines += ["", "## 使用较多的图块", ""]
    top = sorted(blocks, key=lambda b: int(b.get("insert_count") or 0), reverse=True)[:40]
    for b in top:
        if not b.get("insert_count"):
            continue
        label = b.get("display_name")
        extra = f"  {label}" if label and label != b.get("name") else ""
        lines.append(f"- `{b.get('name')}`  插入 {b.get('insert_count')} 次{extra}")
    lines.append("")
    return "\n".join(lines)


# ---------- upload / session creation ----------

def new_session_id() -> str:
    import secrets
    stamp = datetime.now(SHANGHAI).strftime("%Y%m%d")
    return f"s-{stamp}-{secrets.token_hex(3)}"


def create_session_from_upload(filename: str, data: bytes, name: Optional[str] = None) -> dict[str, Any]:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        raise ValueError("请上传 DWG 或 DXF 文件。")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("文件大于 80MB，请先精简后再上传。")
    if len(data) < 64:
        raise ValueError("这个文件太小，不像一份完整的图纸。")
    stem = Path(filename).stem.strip() or "未命名图纸"
    display = (name or stem).strip() or stem
    sid = new_session_id()
    subdir = f"sessions/{sid}"
    base = Path(config.CAD_DATA_DIR) if config.CAD_DATA_DIR else config.STUDIO_ROOT / "data"
    root = base / subdir
    (root / "source").mkdir(parents=True, exist_ok=True)
    (root / "out").mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", Path(filename).name).strip("._") or f"source{suffix}"
    source_path = root / "source" / safe_name
    source_path.write_bytes(data)
    deliverable = "out/source.dxf"
    spec = {
        "id": sid,
        "name": display,
        "kind": "session",
        "data_subdir": subdir,
        "deliverable": deliverable,
        "encoding": "utf-8",
        "dxf_version": "AC1021",
        "source_filename": safe_name,
        "created_at": _now(),
        "updated_at": _now(),
        "note": "由上传创建的会话。源文件在确认回写前不会被改动。",
    }
    projects.save_session(spec)
    proj = projects.resolve_project(sid)
    st = default_status("已收到图纸，开始准备。")
    st["state"] = "running"
    write_status(proj, st)
    start(proj)
    return projects.resolve_project(sid)
