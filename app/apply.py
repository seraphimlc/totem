"""Apply a confirmed plan onto a working copy and the session drawing.

The right-hand preview can render a temporary apply without writing any file
or creating an undo step. Confirm snapshots the pre-edit bytes, writes
.studio/work.dxf, and also writes the session source DXF so AutoCAD opens
the edited drawing. Undo restores the last snapshot, including the original
source. Shared underlay block definitions are never edited.
"""

from __future__ import annotations

import gzip
import json
import logging
import math
import os
import shutil
from pathlib import Path
from typing import Any, Optional

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf.math import Matrix44, Vec3

from . import config, library, placement, projects

log = logging.getLogger("cad-studio.apply")


def _with_project_write_lock(fn):
    """Serialize apply / undo / working-drawing writes for one session only."""
    def wrapped(proj: dict[str, Any], *args, **kwargs):
        from . import projects
        with projects.project_write_lock(proj):
            return fn(proj, *args, **kwargs)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


MAX_REVISIONS = 5
NEW_LAYER_PREFIX = "图腾-新增"
NEAR_MM = 400.0
STAMP_PREVIEW = "方案预览，尚未写入工作图"
DIRTY_POINT_EXTENT = 1200.0
DIRTY_PAD = 800.0


def studio_dir(proj: dict[str, Any]) -> Path:
    path = Path(proj["root"]) / ".studio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def work_path(proj: dict[str, Any]) -> Path:
    return studio_dir(proj) / "work.dxf"


def revisions_dir(proj: dict[str, Any]) -> Path:
    path = studio_dir(proj) / "revisions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def history_path(proj: dict[str, Any]) -> Path:
    return studio_dir(proj) / "revisions.json"


def official_dxf(proj: dict[str, Any]) -> Path:
    return Path(proj["dxf_path"])


def original_snapshot_path(proj: dict[str, Any]) -> Path:
    return revisions_dir(proj) / "original.dxf.gz"


def original_view_path(proj: dict[str, Any]) -> Path:
    return studio_dir(proj) / "original-view.dxf"


def current_dxf(proj: dict[str, Any]) -> Path:
    work = work_path(proj)
    if work.is_file() and work.stat().st_size > 0:
        return work
    return official_dxf(proj)


def is_dirty(proj: dict[str, Any]) -> bool:
    return work_path(proj).is_file() and work_path(proj).stat().st_size > 0


def load_history(proj: dict[str, Any]) -> dict[str, Any]:
    path = history_path(proj)
    if not path.is_file():
        return {"schema": "cad-nl-studio-revisions/v1", "entries": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema": "cad-nl-studio-revisions/v1", "entries": []}
    if not isinstance(data, dict):
        return {"schema": "cad-nl-studio-revisions/v1", "entries": []}
    data.setdefault("schema", "cad-nl-studio-revisions/v1")
    data.setdefault("entries", [])
    return data


def save_history(proj: dict[str, Any], data: dict[str, Any]) -> None:
    path = history_path(proj)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def status(proj: dict[str, Any]) -> dict[str, Any]:
    hist = load_history(proj)
    entries = hist.get("entries") or []
    pending = last_pending_plan(proj)
    source_written = bool(hist.get("source_written") or entries)
    return {
        "dirty": is_dirty(proj),
        "can_undo": bool(entries),
        "revision_count": len(entries),
        "last_revision": entries[-1] if entries else None,
        "pending_plan": pending,
        "official_untouched": not source_written and not is_dirty(proj),
        "source_written": source_written,
    }


def last_pending_plan(proj: dict[str, Any]) -> Optional[dict[str, Any]]:
    from .prompts import plan_has_edits

    applied = {
        e.get("message_id")
        for e in (load_history(proj).get("entries") or [])
        if e.get("message_id")
    }
    messages = (projects.read_chat(proj).get("messages") or [])
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        plan = msg.get("plan")
        if not plan_has_edits(plan):
            continue
        if msg.get("id") in applied:
            continue
        return {
            "message_id": msg.get("id"),
            "reply": msg.get("text") or "",
            "plan": plan,
            "region": msg.get("region"),
        }
    return None


def ensure_work_copy(proj: dict[str, Any]) -> Path:
    dest = work_path(proj)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    src = official_dxf(proj)
    tmp = dest.with_suffix(".tmp.dxf")
    shutil.copy2(src, tmp)
    tmp.replace(dest)
    return dest


def _new_work_copy(proj: dict[str, Any]) -> bool:
    """保证有工作副本；这次是新建的返回 True（写失败时要撤掉的就是它）。"""
    existed = is_dirty(proj)
    ensure_work_copy(proj)
    return not existed


def _snapshot_path(proj: dict[str, Any], rev: int) -> Path:
    return revisions_dir(proj) / f"rev-{rev:04d}.dxf.gz"


def _write_snapshot(src: Path, dest: Path) -> None:
    """把 src 快照到 dest：先完整写到 <stem>.tmp.gz，再原子替换，中途失败不留半个文件。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.gz")
    try:
        with src.open("rb") as fh, gzip.open(tmp, "wb") as out:
            shutil.copyfileobj(fh, out)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _restore_snapshot(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.dxf")
    with gzip.open(src, "rb") as fh, tmp.open("wb") as out:
        shutil.copyfileobj(fh, out)
    tmp.replace(dest)


def _ensure_original_snapshot(proj: dict[str, Any]) -> Path:
    dest = original_snapshot_path(proj)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    src = official_dxf(proj)
    _write_snapshot(src, dest)
    return dest


def original_preview_dxf(proj: dict[str, Any]) -> Path:
    """DXF used for the '看原图' preview after the session source has been written back."""
    snap = original_snapshot_path(proj)
    if snap.is_file() and snap.stat().st_size > 0:
        view = original_view_path(proj)
        if not view.is_file() or view.stat().st_mtime_ns < snap.stat().st_mtime_ns:
            _restore_snapshot(snap, view)
        return view
    return official_dxf(proj)


def _write_session_source(proj: dict[str, Any], work: Path) -> None:
    src = official_dxf(proj)
    if src.resolve() == work.resolve():
        return
    src.parent.mkdir(parents=True, exist_ok=True)
    tmp = src.with_name(src.name + ".write.tmp")
    shutil.copy2(work, tmp)
    tmp.replace(src)


def _prune_history(proj: dict[str, Any], hist: dict[str, Any]) -> None:
    entries = hist.get("entries") or []
    keep = original_snapshot_path(proj).name
    while len(entries) > MAX_REVISIONS:
        old = entries.pop(0)
        path = Path(old.get("snapshot") or "")
        if not path.is_absolute():
            path = revisions_dir(proj) / path.name
        if path.name == keep:
            continue
        if path.is_file():
            path.unlink()


def _drop_render_cache(*paths: Path) -> None:
    try:
        from . import cad_render
    except Exception:
        cad_render = None  # type: ignore
    try:
        from . import scene
    except Exception:
        scene = None  # type: ignore
    for path in paths:
        if not path:
            continue
        if cad_render is not None:
            try:
                cad_render.drop_doc(path)
            except Exception:
                pass
        if scene is not None:
            try:
                scene.drop_scenes_for_dxf(path)
            except Exception:
                pass


def _near(a, b, tol: float = NEAR_MM) -> bool:
    try:
        return abs(float(a[0]) - float(b[0])) <= tol and abs(float(a[1]) - float(b[1])) <= tol
    except Exception:
        return False


def _in_region(xy, bbox, pad: float = 2000.0) -> bool:
    if not bbox or len(bbox) != 4:
        return True
    try:
        x, y = float(xy[0]), float(xy[1])
    except Exception:
        return False
    return bbox[0] - pad <= x <= bbox[2] + pad and bbox[1] - pad <= y <= bbox[3] + pad


def _as_xy(val) -> Optional[list[float]]:
    if isinstance(val, (list, tuple)) and len(val) >= 2:
        return [float(val[0]), float(val[1])]
    return None


def _scale_tuple(val) -> tuple[float, float, float]:
    if isinstance(val, (list, tuple)) and len(val) >= 2:
        return float(val[0] or 1), float(val[1] or 1), 1.0
    if isinstance(val, (int, float)):
        return float(val), float(val), 1.0
    return 1.0, 1.0, 1.0


def _ensure_layer(doc, name: str) -> str:
    name = (name or "").strip() or f"{NEW_LAYER_PREFIX}-1"
    if name not in doc.layers:
        try:
            doc.layers.add(name, color=3)
        except Exception:
            pass
    return name


def _import_block(doc, block_name: str) -> bool:
    if block_name in doc.blocks:
        return True
    index = library.get_index()
    for item in index.get("blocks") or []:
        if item.get("name") != block_name:
            continue
        src = item.get("source") or {}
        dxf = src.get("dxf") or src.get("path")
        candidates = []
        if dxf:
            candidates.append(Path(dxf))
        stem = src.get("file") or src.get("stem") or item.get("library")
        bases = []
        # Optional overrides (Windows / local dev), then the Linux production
        # locations, then project-local var/ copies.
        env_dxf = (os.getenv("CAD_LIBRARY_DXF_DIR") or "").strip()
        if env_dxf:
            bases.append(Path(env_dxf))
        env_index = (os.getenv("CAD_LIBRARY_INDEX") or "").strip()
        if env_index:
            bases.append(Path(env_index).parent)
        index_path = library.index_path()
        bases.extend(
            [
                Path("/workspace/cad-nl/dxf"),
                Path("/opt/cad-studio/current/data/libraries/default"),
            ]
        )
        if index_path:
            bases.append(index_path.parent)
        bases.extend(
            [
                config.STUDIO_ROOT / "var" / "dxf",
                config.STUDIO_ROOT / "var" / "libraries" / "default",
            ]
        )
        for base in bases:
            if stem:
                candidates.append(base / f"{stem}-fixed.dxf")
                candidates.append(base / f"{stem}.dxf")
        for path in candidates:
            if not path or not path.is_file():
                continue
            try:
                src_doc = ezdxf.readfile(str(path))
                if block_name not in src_doc.blocks:
                    continue
                importer = None
                try:
                    from ezdxf.addons.importer import Importer
                    importer = Importer(src_doc, doc)
                except Exception:
                    try:
                        from ezdxf.xref import Importer as XImporter
                        importer = XImporter(src_doc, doc)
                    except Exception:
                        importer = None
                if importer is None:
                    continue
                importer.import_blocks([block_name])
                importer.finalize()
                return block_name in doc.blocks
            except Exception:
                log.exception("import block %s from %s failed", block_name, path)
        break
    return block_name in doc.blocks


def _block_extents(doc, name: str):
    try:
        block = doc.blocks.get(name)
        ext = ezbbox.extents(block, fast=True)
        if ext:
            return ext
    except Exception:
        return None
    return None


def _forward_axis(doc, name: str) -> Optional[tuple[float, float]]:
    """Unit-ish vector in block coords from the insert toward the far end (fixture front)."""
    ext = _block_extents(doc, name)
    if ext is None:
        return None
    minx, miny = float(ext.extmin.x), float(ext.extmin.y)
    maxx, maxy = float(ext.extmax.x), float(ext.extmax.y)
    dx = max(abs(minx), abs(maxx))
    dy = max(abs(miny), abs(maxy))
    if dx <= 1e-9 and dy <= 1e-9:
        return None
    if dx >= dy:
        return (1.0 if abs(maxx) >= abs(minx) else -1.0, 0.0)
    return (0.0, 1.0 if abs(maxy) >= abs(miny) else -1.0)


def _heading(vec) -> Optional[float]:
    try:
        x, y = float(vec[0]), float(vec[1])
    except Exception:
        return None
    if abs(x) < 1e-12 and abs(y) < 1e-12:
        return None
    return math.degrees(math.atan2(y, x))


def _normalize_deg(deg: float) -> float:
    deg = deg % 360.0
    if deg < 0:
        deg += 360.0
    return deg


def _aligned_rotation(doc, old_name: str, new_name: str, old_world_fwd) -> Optional[float]:
    new_fwd = _forward_axis(doc, new_name)
    if new_fwd is None:
        return None
    hw = _heading(old_world_fwd)
    hn = _heading(new_fwd)
    if hw is None or hn is None:
        return None
    return _normalize_deg(hw - hn)


def _world_forward(matrix: Matrix44, local_fwd: tuple[float, float]):
    p0 = matrix.transform(Vec3(0, 0, 0))
    p1 = matrix.transform(Vec3(local_fwd[0], local_fwd[1], 0))
    return (p1.x - p0.x, p1.y - p0.y)


AMBIGUITY_MM = 300.0   # 同名块第二近的一处离模型给的点没有明显更远（差 < 300mm）→ 不猜


def _find_symbol_hits(msp, block: str, xy, bbox, pad: float = 2000.0):
    """当前分区里块名匹配的全部顶层候选 [(dist, entity, pt)]（按距离排序）。"""
    hits = []
    for e in msp.query("INSERT"):
        try:
            name = e.dxf.name
            pos = e.dxf.insert
        except Exception:
            continue
        if block and name != block:
            continue
        pt = [float(pos.x), float(pos.y)]
        if not _in_region(pt, bbox, pad=pad):
            continue
        if xy is None or _near(pt, xy):
            dist = 0.0 if xy is None else abs(pt[0] - float(xy[0])) + abs(pt[1] - float(xy[1]))
            hits.append((dist, e, pt))
    hits.sort(key=lambda t: t[0])
    return hits


def _nearest_of(hits, ambiguous: Optional[dict[str, Any]] = None):
    """最近的候选；有第二处几乎一样近时返回 ambiguous（不猜），否则返回第一项。"""
    if not hits:
        return None
    if len(hits) >= 2 and abs(hits[1][0] - hits[0][0]) < AMBIGUITY_MM:
        return ambiguous if ambiguous is not None else None
    return hits[0]


def _find_insert(msp, block: str, xy, bbox, pad: float = 2000.0):
    """当前分区里的顶层同名 INSERT；等距/近似等距的多处视为歧义，返回 None。

    真正要区分「歧义」和「没找到」的调用方用 ``_find_symbol_hits`` / ``_locate_target``。
    """
    return _nearest_of(_find_symbol_hits(msp, block, xy, bbox, pad=pad))


def _checked_rotation(value) -> Optional[float]:
    """旋转角：None 表示不改；布尔/NaN/Infinity/字符串一律拒绝（先算再写，避免动了一半）。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"旋转角「{value}」不是数字")
    deg = float(value)
    if not math.isfinite(deg):
        raise ValueError("旋转角不是有限数字")
    return _normalize_deg(deg)


def _locate_target(doc, msp, block: str, xy, bbox, *, pad: float = 2000.0) -> dict[str, Any]:
    """找到要动的 INSERT 并做入口校验：返回 ``{"found": rec}`` 或 ``{"ok": False, ...}``。

    - 顶层多处同名块、模型给的点分不出哪一处（差 < AMBIGUITY_MM）→ 歧义，不猜；
    - 嵌套在共享图块内部的目标 → 拒绝（``nested_not_supported``）。嵌套区块里画在 0 层的
      内容一律显示在块**内部**中位置最低的那个非 0 图层，不是它自己的 INSERT 层；
    - 空图层 / 图层被关掉的块 → 拒绝（``hidden_not_supported``）；
    - 显示图层（``effective_layer``）在保护层，或者块里有任何落在保护层的内容
      （``analyze_insert``：嵌套插入、块内文字、属性，判不清也算）→ 拒绝。
    """
    from . import planschema

    hits = _find_symbol_hits(msp, block, xy, bbox, pad=pad)
    nearest = _nearest_of(hits)
    if nearest is None and hits:
        return _ambiguous(block, xy)
    if nearest is None:
        best = None
        for host in msp.query("INSERT"):
            try:
                name = host.dxf.name
                if name not in doc.blocks:
                    continue
                hip = host.dxf.insert
                if bbox and not _in_region([float(hip.x), float(hip.y)], bbox, pad=20000.0):
                    continue
                hm = host.matrix44()
            except Exception:
                continue
            found = _search_nested(doc, doc.blocks.get(name), hm, block, xy, bbox, depth=1, host=host)
            if not found:
                continue
            if best is not None and abs(best["dist"] - found["dist"]) < AMBIGUITY_MM:
                return _ambiguous(block, xy)
            if best is None or found["dist"] < best["dist"]:
                found["host_name"] = name
                best = found
        if best is None:
            return {"ok": False, "reason": f"当前分区里没有找到「{block}」", "block": block, "xy": None}
        return _nested_rejection(block, best)
    _, ent, pt = nearest
    try:
        matrix = ent.matrix44()
    except Exception:
        matrix = Matrix44.translate(pt[0], pt[1], 0)
    target = {
        "kind": "direct",
        "entity": ent,
        "host": None,
        "xy": pt,
        "rot": float(ent.dxf.rotation or 0),
        "matrix": matrix,
        "name": block,
        "dist": 0.0 if xy is None else abs(pt[0] - float(xy[0])) + abs(pt[1] - float(xy[1])),
    }
    layer = planschema.effective_layer(ent)
    try:
        raw = str(ent.dxf.layer or "").strip()
    except Exception:
        raw = ""
    if raw == "" or _layer_hidden(doc, layer):
        return {
            "ok": False,
            "reason": f"「{block}」所在的图层是空图层或被关掉了，看不出来它显示在哪里，这次没有动它",
            "block": block,
            "xy": pt,
            "issue_code": "hidden_not_supported",
            "layer": layer,
        }
    protected = planschema.protected_pattern(layer)
    if protected:
        return {
            "ok": False,
            "reason": f"「{block}」显示在保护图层「{layer}」（含「{protected}」）上，图框、图签、标题不改",
            "block": block,
            "xy": pt,
            "issue_code": "protected_layer",
            "layer": layer,
        }
    analysis = planschema.analyze_insert(ent)
    if analysis.get("protected"):
        return {
            "ok": False,
            "reason": _content_reason(block, analysis),
            "block": block,
            "xy": pt,
            "issue_code": "protected_layer",
            "layer": layer,
        }
    return {"found": target}


def _ambiguous(block: str, xy) -> dict[str, Any]:
    point = [float(xy[0]), float(xy[1])] if xy else None
    return {
        "ok": False,
        "reason": f"「{block}」在附近有多处，分不出是哪一处，请给出更准确的位置",
        "block": block,
        "xy": point,
        "issue_code": "ambiguous_target",
    }


def _layer_hidden(doc, name: str) -> bool:
    try:
        layer = doc.layers.get(name)
        return bool(layer is not None and (layer.is_off() or layer.is_frozen()))
    except Exception:
        return False


def _content_reason(name: str, analysis: dict[str, Any]) -> str:
    """说明为什么整块不能动：块里有保护图层的内容，或者这块的显示判不清。"""
    where = str(analysis.get("protected_where") or "")
    if where:
        return f"「{name}」块里有保护图层「{where}」上的内容（图框、图签、标题），删它、移它或换掉它会把这些一起带走，不改"
    if analysis.get("xref"):
        return f"「{name}」是外部参照，内容不在本图里，不改"
    if analysis.get("mirrored"):
        return f"「{name}」是镜像插入，块里 0 层的内容显示在哪层判不准，不改"
    if analysis.get("cycle") or analysis.get("depth"):
        return f"「{name}」的块引用成环或嵌套太深，判不清里面有没有图框、图签内容，不改"
    return f"「{name}」的显示图层判不清（块缺失、内容读不了或嵌套成环），不改"


def _nested_rejection(name: str, found: dict[str, Any]) -> dict[str, Any]:
    """嵌套在共享图块里的目标：内部约定不改块定义，模型空间也覆盖不住块内图元，一律不写。"""
    host = str(found.get("host_name") or "").strip()
    where = f"在共享图块「{host}」内部，" if host else "在别的图块内部，"
    return {
        "ok": False,
        "reason": f"「{name}」{where}这个位置不支持修改，这次没有动它",
        "block": name,
        "xy": found.get("xy"),
        "issue_code": "nested_not_supported",
    }


def _compose_child_matrix(entity, parent: Matrix44) -> Matrix44:
    # ezdxf Matrix44 uses a convention where child @ parent maps child-local to WCS.
    return entity.matrix44() @ parent


def _search_nested(doc, layout, parent_m: Matrix44, target: str, xy, bbox, *, depth: int, host):
    if depth > 8 or layout is None:
        return None
    best = None
    for ent in layout:
        if ent.dxftype() != "INSERT":
            continue
        try:
            name = ent.dxf.name
            m = _compose_child_matrix(ent, parent_m)
            pos = parent_m.transform(ent.dxf.insert)
            pt = [float(pos.x), float(pos.y)]
        except Exception:
            continue
        if name == target and (xy is None or _near(pt, xy)) and _in_region(pt, bbox):
            dist = 0.0 if xy is None else abs(pt[0] - float(xy[0])) + abs(pt[1] - float(xy[1]))
            rec = {
                "kind": "overlay",
                "entity": ent,
                "host": host,
                "xy": pt,
                "rot": float(ent.dxf.rotation or 0),
                "matrix": m,
                "name": name,
                "dist": dist,
            }
            if best is None or dist < best["dist"]:
                best = rec
        child_layout = doc.blocks.get(name) if name in doc.blocks else None
        if child_layout is not None and depth < 8:
            found = _search_nested(doc, child_layout, m, target, xy, bbox, depth=depth + 1, host=host)
            if found and (best is None or found["dist"] < best["dist"]):
                best = found
    return best


def find_symbol(doc, msp, block: str, xy, bbox) -> Optional[dict[str, Any]]:
    """Locate a top-level modelspace INSERT (kind="direct"), or look inside host blocks.

    A nested hit is returned with kind="overlay" so the old assertions can tell it apart,
    but nothing is ever written for it: writes go through ``_locate_target``, which rejects
    nested targets (see ``_nested_rejection``).
    """
    hit = _find_insert(msp, block, xy, bbox)
    if hit:
        _, ent, pt = hit
        try:
            matrix = ent.matrix44()
        except Exception:
            matrix = Matrix44.translate(pt[0], pt[1], 0)
        return {
            "kind": "direct",
            "entity": ent,
            "host": None,
            "xy": pt,
            "rot": float(ent.dxf.rotation or 0),
            "matrix": matrix,
            "name": block,
            "dist": 0.0 if xy is None else abs(pt[0] - float(xy[0])) + abs(pt[1] - float(xy[1])),
        }
    best = None
    for host in msp.query("INSERT"):
        try:
            name = host.dxf.name
            if name not in doc.blocks:
                continue
            hip = host.dxf.insert
            # Skip hosts whose insertion point is far from the current view.
            if bbox and not _in_region([float(hip.x), float(hip.y)], bbox, pad=20000.0):
                continue
            hm = host.matrix44()
        except Exception:
            continue
        found = _search_nested(doc, doc.blocks.get(name), hm, block, xy, bbox, depth=1, host=host)
        if found and (best is None or found["dist"] < best["dist"]):
            found["kind"] = "overlay"
            found["host_name"] = name
            best = found
    return best


def _choose_rotation(doc, item: dict[str, Any], found: Optional[dict[str, Any]], new_name: str) -> float:
    planned = item.get("rot")
    if found:
        old_name = found.get("name")
        local_fwd = _forward_axis(doc, old_name) if old_name else None
        if local_fwd is not None:
            world_fwd = _world_forward(found["matrix"], local_fwd)
        else:
            # Fall back to the nested entity's +X axis in WCS.
            world_fwd = _world_forward(found["matrix"], (1.0, 0.0))
        aligned = _aligned_rotation(doc, old_name or "", new_name, world_fwd)
        if aligned is not None:
            return aligned
        try:
            return _normalize_deg(float(found.get("rot") or 0))
        except Exception:
            pass
    if planned is not None and planned != "":
        return float(planned)
    return 0.0


def _add_insert(
    doc,
    msp,
    item: dict[str, Any],
    layer: str,
    bbox,
    *,
    found: Optional[dict[str, Any]] = None,
    region: Optional[dict[str, Any]] = None,
    index: Optional[dict[str, Any]] = None,
    snap_kind: str = "insert",
) -> dict[str, Any]:
    name = str(item.get("block") or "").strip()
    xy = _as_xy(item.get("xy") or item.get("to"))
    if (not xy) and found:
        xy = list(found["xy"])
    if not name or not xy:
        return {"ok": False, "reason": "缺少块名或坐标", "item": item}
    # 落笔前先吸附：越界夹回、同类偏移校正、同轴对齐。命中才改，改了就带原因。
    planned_xy = list(xy)
    xy, snap_notes = _snap_before_insert(
        doc, item, block=name, xy=xy, bbox=bbox, region=region, index=index, kind=snap_kind
    )
    snapped = bool(snap_notes) and xy != planned_xy
    if not _in_region(xy, bbox):
        return {"ok": False, "reason": "坐标不在当前分区内", "block": name, "xy": xy}
    layer_name = _ensure_layer(doc, layer)
    sx, sy, sz = _scale_tuple(item.get("scale"))
    rot = _choose_rotation(doc, item, found, name)
    insert_dxfattribs = {"layer": layer_name, "rotation": rot, "xscale": sx, "yscale": sy, "zscale": sz}
    # 探测一律在隔离副本里做：本图一个像素都不加，绝不让「新块」绕过保护判断，也不在调用方
    # 的图里留下试插的实体。库里的块（外加本图已有的定义 / 图层，让 0 层继承看出真实图层）
    # 一起放进副本，重复插同一个块也看得见。
    work = _import_isolated(name, layer_sources=doc, base=doc)
    if work is None:
        return {"ok": False, "reason": f"图纸和默认组件库里都没有块「{name}」", "block": name, "xy": xy}
    try:
        probe = work.modelspace().add_blockref(name, insert=xy, dxfattribs=dict(insert_dxfattribs))
    except Exception as exc:
        return {"ok": False, "reason": f"块「{name}」插入失败，这一处没有写：{exc}", "block": name, "xy": xy}
    check = _guard_new_block(work, probe, found)
    if check:
        return check
    # 守卫通过后才往本图里导入库块。INSERT 之后才可能让副本跨到本图：生产代码里任何一步
    # 抛错都不能留在调用方的图上——撤掉这次新建的实体，只有「这次刚导入、且再没有别的 INSERT
    # 引用」的定义才撤。已在本图的定义不碰。
    fresh_def = name not in doc.blocks
    # None = 没拍成：之后不会拿它当底数扫布局 / 认领新实体（见 _layout_snapshot）。
    before = _layout_snapshot(msp)
    if fresh_def:
        try:
            available = bool(_import_block(doc, name))
        except Exception as exc:
            # 导入这一步不可能造出 INSERT：只撤回这次导入了一半的块定义（没有别的引用时才撤）。
            _remove_insert_probe(doc, msp, [], name, before=before, fresh_def=True, allow_scan=False)
            return {"ok": False, "reason": f"块「{name}」插入失败，这一处没有写：{exc}", "block": name, "xy": xy}
        if not available:
            return {"ok": False, "reason": f"图纸和默认组件库里都没有块「{name}」", "block": name, "xy": xy}
    try:
        returned = msp.add_blockref(name, insert=xy, dxfattribs=dict(insert_dxfattribs))
    except Exception as exc:
        # 不管建到哪一步抛错：不在 before 快照里的新实体（含已建成、没返回的，和只绑了 handle
        # 没挂进布局的半成品）都撤掉；这次刚导入、又没人用的块定义也撤；本图原有的定义不碰。
        _remove_insert_probe(doc, msp, [], name, before=before, fresh_def=fresh_def)
        return {"ok": False, "reason": f"块「{name}」插入失败，这一处没有写：{exc}", "block": name, "xy": xy}
    # add_blockref 的返回值不能全信：记下来的实体可以是这次新建的，也可能不是。只认「不在
    # before 快照里、确实还挂在布局上」的实体；它建出来却没记（返回值不是它）的同样会被撤。
    # 注意别覆盖参数 found（替换目标）：下面 rot_adjusted 还要用它。
    new_ref, extra = _new_layout_entities(msp, before)
    ref = new_ref
    if returned is not None and not _gone(returned) and _handle_of(returned) in _layout_handles(msp):
        ref = returned  # 返回值确实是还挂着的实体：以它为准（不再去找别的）
    if ref is None:
        # add_blockref 返回了、却找不到它建出来的实体：不能胡乱删别人，只能如实报失败，这个块不认领。
        log.error("insert of %s returned but no new entity was found", name)
        return {
            "ok": False,
            "reason": f"块「{name}」插入后没能确认新实体，这一处没有写",
            "block": name,
            "xy": xy,
        }
    if extra:
        log.warning("insert of %s created %d unexpected entities", name, len(extra))
    out = {"ok": True, "action": "insert", "block": name, "xy": xy, "layer": layer_name, "rot": rot, "handle": ref.dxf.handle}
    if fresh_def:
        out["imported"] = True
    if snapped:
        out["snapped"] = True
        out["snap_notes"] = snap_notes
        out["xy_planned"] = planned_xy
    if found and item.get("rot") is not None:
        try:
            if abs(_normalize_deg(float(item.get("rot"))) - _normalize_deg(rot)) > 0.5:
                out["rot_adjusted"] = True
        except Exception:
            pass
    return out


def _import_isolated(name: str, *, layer_sources=None, base=None):
    """在副本里导入一个库块，用来在写本图之前查清它的内容；库/图纸里都没有返回 None。

    ``layer_sources`` 里的图层先抄进副本（块里的 0 层内容要看插入图层是否受保护，副本
    没有的图层会被当成假的层面），``base`` 是本图已有的块定义先抄进副本（同名的块以
    本图为准）。副本与原图互不影响：探测用的 INSERT 只加在副本里。
    """
    try:
        work = ezdxf.new("R2010", setup=True)
    except Exception:
        return None
    try:
        if layer_sources is not None:
            for layer in getattr(layer_sources, "layers", []):
                try:
                    if layer.dxf.name not in work.layers:
                        work.layers.add(layer.dxf.name, dxfattribs={"color": int(layer.dxf.color)})
                except Exception:
                    continue
        if base is not None:
            for block in getattr(base, "blocks", []):
                if block.name in work.blocks:
                    continue
                try:
                    from ezdxf.addons.importer import Importer

                    importer = Importer(base, work)
                    importer.import_blocks([block.name])
                    importer.finalize()
                except Exception:
                    continue
        if name in work.blocks or (name not in getattr(base, "blocks", {}) and _import_block(work, name)):
            return work
    except Exception:
        log.exception("isolated import of %s failed", name)
    return None


def _attrib_values(insert) -> dict[str, str]:
    vals: dict[str, str] = {}
    try:
        for att in insert.attribs:
            tag = str(att.dxf.tag or "").strip()
            if tag:
                vals[tag] = str(att.dxf.text or "")
    except Exception:
        return {}
    return vals


def _attributes_to_carry(doc, msp, item: dict[str, Any], bbox) -> dict[str, str]:
    """替换前先抄下旧块上要带过去的属性值（设备编号之类）。只认顶层 INSERT。"""
    if item.get("auto_attribs") is False:
        return {}
    try:
        old_block = str(item.get("from_block") or item.get("block") or "").strip()
        new_block = str(item.get("block") or "").strip()
        point = _as_xy(item.get("from_xy") or item.get("xy") or item.get("from"))
        if not old_block or not new_block or old_block == new_block or not point:
            return {}
        found = _find_insert(msp, old_block, point, bbox)
        if not found:
            return {}
        return _attrib_values(found[1])
    except Exception:
        log.exception("read replace attribs failed")
        return {}


def _apply_carried_attribs(doc, new_ref, values: dict[str, str], old_layer: str = "") -> dict[str, str]:
    """把同 tag 的属性值写到新块上；新块没有的 ATTDEF、新块已有的值都不动。"""
    if new_ref is None or not values:
        return {}
    try:
        block = doc.blocks.get(new_ref.dxf.name)
        if block is None:
            return {}
        out: dict[str, str] = {}
        existing = _attrib_values(new_ref)
        for attdef in block.query("ATTDEF"):
            tag = str(attdef.dxf.tag or "").strip()
            if not tag or tag not in values or existing.get(tag):
                continue
            attribs: dict[str, Any] = {
                "height": float(getattr(attdef.dxf, "height", 2.5) or 2.5),
                "flags": int(getattr(attdef.dxf, "flags", 0) or 0),
            }
            if old_layer:
                attribs["layer"] = old_layer
            new_ref.add_attrib(
                tag,
                values[tag],
                insert=(float(attdef.dxf.insert.x), float(attdef.dxf.insert.y)),
                dxfattribs=attribs,
            )
            out[tag] = values[tag]
        return out
    except Exception:
        log.exception("carry attribs failed")
        return {}


def _snap_before_insert(
    doc,
    item: dict[str, Any],
    *,
    block: str,
    xy,
    bbox,
    region: Optional[dict[str, Any]],
    index: Optional[dict[str, Any]],
    kind: str,
):
    """跑一轮 placement.snap_xy；任何异常都退回原坐标，不阻断写入。"""
    if not xy:
        return xy, []
    try:
        from . import placement

        new_xy, notes = placement.snap_xy(
            doc, item, block=block, xy=xy, bbox=bbox, region=region, index=index, kind=kind
        )
    except Exception:
        log.exception("snap_xy failed, keep planned xy")
        return xy, []
    if not notes or not new_xy:
        return xy, []
    try:
        return [float(new_xy[0]), float(new_xy[1])], [str(n) for n in notes if n]
    except Exception:
        return xy, []


def _owner_layout(msp, ent):
    """实体所在的布局；apply 只动模型空间里的顶层 INSERT，其他布局里的同句柄实体不动。"""
    try:
        if ent.get_layout() is msp:
            return msp
    except Exception:
        pass
    return None


def _gone(ent) -> bool:
    """实体是不是已经从图里删掉了：销毁了，或者已经不再挂在任何文档上。"""
    try:
        return (not ent.is_alive) or getattr(ent, "doc", None) is None
    except Exception:
        return True


def _delete_direct(msp, found: dict[str, Any]) -> dict[str, Any]:
    """删掉一个顶层 INSERT：``Modelspace.delete_entity(entity)``（ezdxf 的公开签名只收实体）。

    删掉了才报 ok；布局方法失败时再用 ``entity.destroy()`` 兜底（ezdxf 的 internal API，
    会同时摘掉实体库里的记录）。两样都没删掉就如实报失败，绝不当成删成功。
    """
    ent = found.get("entity")
    base = {"action": "delete", "block": found.get("name"), "xy": found.get("xy"), "found": found}
    if ent is None or _gone(ent):
        return {"ok": False, "reason": "要删的图块已经不在了", **base}
    if _owner_layout(msp, ent) is None:
        return {"ok": False, "reason": "这个图块不在模型空间里，没有删", **base}
    try:
        msp.delete_entity(ent)
        if _gone(ent):
            return {"ok": True, **base}
    except Exception:
        log.exception("delete_entity failed for %s, trying destroy()", found.get("name"))
    try:
        ent.destroy()
    except Exception as exc:
        return {"ok": False, "reason": f"删除失败，原图块还在：{exc}", **base}
    if _gone(ent):
        return {"ok": True, **base}
    return {"ok": False, "reason": "删除失败，原图块还在", **base}


def _hide_or_delete(doc, msp, item: dict[str, Any], bbox, layer: str) -> dict[str, Any]:
    """删除项 / 替换时的旧块：只删顶层、显示图层不保护的 INSERT。"""
    name = str(item.get("block") or item.get("from_block") or "").strip()
    xy = _as_xy(item.get("xy") or item.get("from_xy") or item.get("from"))
    if not name or not xy:
        return {"ok": False, "reason": "删除需要块名和坐标", "item": item}
    located = _locate_target(doc, msp, name, xy, bbox)
    if not located.get("found"):
        return {k: v for k, v in located.items() if k != "found"}
    found = located["found"]
    if found["kind"] != "direct" or _owner_layout(msp, found["entity"]) is None:
        return {**_nested_rejection(name, found), "found": found}
    return _delete_direct(msp, found)


def _preflight_replace(doc, msp, old_item: dict[str, Any], item: dict[str, Any], bbox, new_xy) -> dict[str, Any]:
    """替换写图前的检查：目标存在、不在保护层、不是共享块内部、新块插得进、坐标在分区内。

    返回 ``{"found": rec}`` 表示可以写；否则返回失败结果（``reason`` / ``issue_code``）。
    任何一项不成立都在动旧图块之前返回，保证「插不上也不丢旧块」。
    """
    name = str(old_item.get("block") or "").strip()
    xy = _as_xy(old_item.get("xy")) or _as_xy(new_xy)
    if not name or not xy:
        return {"ok": False, "reason": "替换需要旧块名和坐标", "block": name}
    located = _locate_target(doc, msp, name, xy, bbox)
    if not located.get("found"):
        return {k: v for k, v in located.items() if k != "found"}
    found = located["found"]
    if found["kind"] != "direct":
        return _nested_rejection(name, found)
    new_name = str(item.get("block") or "").strip()
    if not new_name:
        return {"ok": False, "reason": "替换缺少新块名", "block": name}
    if new_name not in doc.blocks and not _block_available(new_name):
        return {"ok": False, "reason": f"图纸和默认组件库里都没有块「{new_name}」，原图块保留", "block": new_name}
    if new_xy is None:
        new_xy = found["xy"]
    if not _in_region(new_xy, bbox):
        return {"ok": False, "reason": "新块坐标不在当前分区内，原图块保留", "block": new_name, "xy": new_xy, "issue_code": "out_of_region"}
    return {"found": found}


def _guard_new_block(doc, ref, found: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """新插 / 新导入的块是否有保护图层的内容、是否落进保护图层；有问题返回失败结果。"""
    from . import planschema

    name = str(ref.dxf.name)
    layer = str(ref.dxf.layer or "")
    hit = planschema.protected_pattern(layer)
    if hit:
        return {
            "ok": False,
            "reason": f"新块「{name}」不能放到保护图层「{layer}」（含「{hit}」）上",
            "block": name,
            "issue_code": "protected_layer",
            "layer": layer,
        }
    analysis = planschema.analyze_insert(ref)
    if analysis.get("protected_where"):
        return {
            "ok": False,
            "reason": _content_reason(name, analysis),
            "block": name,
            "issue_code": "protected_layer",
        }
    if found and found.get("kind") == "direct":
        old_layer = _layer_name(found.get("entity"))
        if old_layer and planschema.is_protected_layer(planschema.effective_layer(found["entity"])):
            return {
                "ok": False,
                "reason": f"被替换的「{found.get('name')}」在保护图层「{old_layer}」上，不换",
                "block": name,
                "issue_code": "protected_layer",
            }
    return None


def _remove_entity(msp, ent) -> bool:
    """删掉刚插上、还没写盘的一个实体（失败就 restore 兜底，不影响旧图）。

    ``delete_entity`` / ``destroy`` 都失败时（例如它们被换成了会抛错的实现），按 ezdxf 删除的
    两步手动摘掉（从布局、从实体库）：只有这一个刚建、还没写盘的实体走这条路，不碰旧图。
    """
    try:
        msp.delete_entity(ent)
        if _gone(ent):
            return True
    except Exception:
        log.exception("remove entity failed")
    try:
        ent.destroy()
        if _gone(ent):
            return True
    except Exception:
        log.exception("destroy entity failed")
    try:
        from ezdxf.layouts.base import BaseLayout

        layout = ent.get_layout()
        BaseLayout.unlink_entity(layout, ent)  # 1. 从布局里摘掉
        doc = getattr(ent, "doc", None)
        if doc is not None:
            doc.entitydb.discard(ent)  # 2. 从实体库删掉（不调 destroy）
            ent.dxf.discard("handle")  # 3. 摘掉句柄 → 这个实体不再「活着」
        if _gone(ent) or ent not in layout:
            return True  # 已经从图里摘掉了（哪怕实体对象还带着 dxf 属性）
    except Exception:
        log.exception("remove entity fallback failed")
    return _gone(ent)


def _layout_handles(layout) -> set[str]:
    """布局里现在挂着的实体句柄。``entity_space`` 是布局实际持有的实体列表（未销毁的）。"""
    handles: set[str] = set()
    space = getattr(layout, "entity_space", None)
    if space is None:
        space = layout
    try:
        for ent in space:
            handle = _handle_of(ent)
            if handle:
                handles.add(handle)
    except Exception:
        return set()
    return handles


def _layout_snapshot(msp) -> Optional[set[str]]:
    """写 INSERT 之前拍一下布局里已有的句柄。拍不出来返回 None——**不是空集**：
    空集会被当成「写之前布局是空的」，等于把所有已有实体都认成这次新建的（误删 / 误认领）。
    """
    try:
        return _layout_handles(msp)
    except Exception:
        log.exception("layout snapshot failed, rollback will only handle known entities")
        return None


def _new_layout_entities(msp, before) -> tuple[Any, list]:
    """写 INSERT 之后：返回这次新出现的 (第一个新实体, 其余新实体)。一个也没认出时返回 (None, [])。

    ``before`` 是 ``_layout_snapshot`` 的结果；为 None（快照没拍成）或拍完布局拿不出来时
    一律返回 (None, [])：没有底数就分不清新旧，宁可认不出、也不把原有实体当成本次新建的。
    """
    if before is None:
        return None, []
    try:
        after = list(msp.entity_space)
    except Exception:
        return None, []
    fresh = [ent for ent in after if _handle_of(ent) not in before]
    if not fresh:
        return None, []
    return fresh[0], fresh[1:]


def _referencing_layout(doc, ent) -> Any:
    try:
        layout = ent.get_layout()
        if layout is not None:
            return layout
    except Exception:
        pass
    doc_ = getattr(ent, "doc", None)
    return getattr(doc_, "modelspace", lambda: None)() if doc_ is not None else None


def _remove_insert_probe(
    doc, msp, probes, name: str, *, before=None, fresh_def: bool = False, allow_scan: bool = True
) -> None:
    """撤回这次新建的 INSERT，以及（若没有任何引用了）为它导入的块定义。

    - 要撤的实体 = ``probes`` + 「不在 ``before`` 快照里、或已经没有句柄还在实体库里的新实体」
      ——包括 add_blockref 抛错前已经建出来、根本没返回的实体，和只绑了 handle、没挂进布局
      的半成品。只撤这些，不碰调用方图里原有的同名插入。
    - ``allow_scan=False``：「插入前」那一步（导入库块）抛错时用。这一步不可能造出 INSERT；
      要撤的只剩这次尝试导入、却没有插上的块定义，绝不按句柄扫布局（扫描只凭「不在快照里」
      判断，万一有别的代码路径在这段时间往布局里加了实体，就会被误删）。
    - ``before`` 为 None（写之前没拍成快照，或拍完布局就取不出来了）：没有底数就分不清新旧，
      同样不扫，只撤 ``probes`` 里明确知道的实体，绝不把已有实体当成残留。
    - 块定义只在 ``fresh_def``（这次插入前本图没有它）、并且任何布局 / 块定义里都没有别的
      INSERT 引用它时才删。本来就在图里的定义一律不碰。都不成就如实记一条 error 日志：
      这时不能假装撤回成功。
    """
    doomed = [p for p in (probes or []) if p is not None]
    # 快照没拍成（None）或布局拿不出来：没有底数，扫一圈会把所有已有实体都当成残留误删，
    # 所以不扫，只撤明确知道的 probes。
    scan = allow_scan and before is not None
    after: list = []
    if scan:
        try:
            after = list(msp.entity_space)
        except Exception:
            log.exception("layout scan failed, rollback will only handle known entities")
            scan = False
    if scan:
        for ent in after:
            if ent in doomed:
                continue
            handle = None if _gone(ent) else _handle_of(ent)
            if handle and handle in before:
                continue  # 写之前就有的实体，不归这次管
            doomed.append(ent)  # 不在快照里（或已经销毁）→ 是这次造的残留
    asked_layout = False
    for ent in doomed:
        if _gone(ent):
            continue
        doc_ = getattr(ent, "doc", None)
        handle = _handle_of(ent)
        if doc_ is not None and handle:
            behind = doc_.entitydb.get(handle)
            if behind is None or behind is not ent:
                continue  # 实体库里已经不是它了（已经撤掉 / 被换掉），不要动别人的记录
        if not asked_layout:
            asked_layout = True
            # _owner_layout 会问 get_layout()：对「句柄已建、还没挂进布局」的半成品，ezdxf 会照
            # owner 去反查块记录、顺手修一遍指向，这时还不该修。等干净了再问。
            try:
                ent.get_layout()
            except Exception:
                pass
        layout = _referencing_layout(doc, ent) or msp
        linked = layout is not None and handle is not None and handle in _layout_handles(layout)
        if linked:
            if not _remove_entity(layout, ent):
                log.error("rollback could not remove INSERT %s of %s from its layout", handle, name)
            continue
        # 只绑了 handle、没挂进布局的半成品（或已经没有句柄的）：没有别处引用，从实体库里丢掉。
        # 光 destroy 不够：它占着句柄，导出时仍会被写成一个没有 owner 的孤儿实体。
        try:
            if doc_ is not None and handle:
                doc_.entitydb.discard(ent)
        except Exception:
            log.exception("rollback could not discard half-created INSERT %s of %s", handle, name)
        still_there = doc_ is not None and handle is not None and doc_.entitydb.get(handle) is not None
        if not still_there:
            try:
                if ent.is_alive:
                    ent.dxf.discard("handle")
            except Exception:
                pass
    try:
        if not fresh_def or doc.blocks.get(name) is None:
            return
        for layout in [doc.modelspace(), *doc.blocks]:
            try:
                own = _layout_handles(layout)
            except Exception:
                continue
            for ent in layout.query("INSERT"):
                handle = _handle_of(ent)
                if handle in own and str(ent.dxf.name) == name and not _gone(ent):
                    return  # 还有引用（包括没撤掉的这次插入），定义必须留着
        doc.blocks.delete_block(name, safe=True)
        if doc.blocks.get(name) is not None:
            log.error("rollback could not remove imported block definition %s", name)
    except Exception:
        log.exception("remove insert probe for %s failed", name)


def _block_available(name: str) -> bool:
    """库里能不能找到这个块（只查索引，不导入、不改文档）。"""
    try:
        index = library.get_index()
    except Exception:
        return False
    for item in index.get("blocks") or []:
        if item.get("name") == name:
            return True
    return False


def _rollback_created(doc, msp, created: dict[str, Any]) -> None:
    """旧图块没能删掉时，把刚插的新块撤掉，避免同一处出现两个符号。"""
    handle = created.get("handle")
    if not handle:
        return
    try:
        ent = doc.entitydb.get(handle)
    except Exception:
        ent = None
    if ent is None:
        return
    if _remove_entity(msp, ent):
        return
    log.error("rollback created insert failed")


def _ensure_sortents(doc) -> None:
    """Keep AutoCAD redraw-order sorting on (bit 16) so covers stay under pipes."""
    try:
        current = int(doc.header.get("$SORTENTS") or 0)
    except Exception:
        current = 0
    if current & 16:
        return
    doc.header["$SORTENTS"] = current | 16 if current else 127


def _apply_overlay_draw_order(doc, msp, results: list[dict[str, Any]]) -> None:
    """Underlay, then covers, then new inserts, then existing pipes and labels.

    WIPEOUT hides whatever is behind it. Drain pipes and text live in modelspace,
    so they must be drawn after the cover or the stall stack looks broken.
    """
    cover_handles = {r["handle"] for r in results if r.get("ok") and r.get("action") == "cover" and r.get("handle")}
    insert_handles = {r["handle"] for r in results if r.get("ok") and r.get("action") == "insert" and r.get("handle")}
    host_handles = set()
    for r in results:
        host = (r.get("found") or {}).get("host")
        if host is None:
            continue
        try:
            host_handles.add(host.dxf.handle)
        except Exception:
            pass
    if not cover_handles and not insert_handles:
        return
    _ensure_sortents(doc)
    try:
        existing = dict(msp.get_redraw_order() or [])
        ents = list(msp.entities_in_redraw_order()) if existing else list(msp)
    except Exception:
        ents = list(msp)
    def _h(ent):
        try:
            return ent.dxf.handle
        except Exception:
            return None
    hosts, covers, news, rest = [], [], [], []
    for ent in ents:
        handle = _h(ent)
        if handle in host_handles:
            hosts.append(ent)
        elif handle in cover_handles:
            covers.append(ent)
        elif handle in insert_handles:
            news.append(ent)
        else:
            rest.append(ent)
    order = hosts + covers + news + rest
    mapping = []
    for i, ent in enumerate(order, start=1):
        handle = _h(ent)
        if not handle:
            continue
        mapping.append((handle, f"{i:X}"))
    if not mapping:
        return
    try:
        msp.set_redraw_order(mapping)
    except Exception:
        log.exception("set overlay draw order failed")


def placement_issues(
    doc,
    plan: dict[str, Any],
    *,
    bbox=None,
    region: Optional[dict[str, Any]] = None,
    index: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """跑一轮坐标校验；出问题只记日志，不影响写入。"""
    try:
        from . import placement

        return placement.check_plan(doc, plan, bbox=bbox, region=region, index=index)
    except Exception:
        log.exception("check_plan failed")
        return []


class PlanBlockedError(ValueError):
    """方案（结构或保护图层）有阻断项：不写图、不建快照。``result`` 是 planschema 的结果。

    main 用 ``result.needs_clarification`` 区分「问用户」与「提示被拦下」。
    """

    def __init__(self, message: str, result: Any):
        super().__init__(message)
        self.result = result
        try:
            self.codes = [str(i.get("code")) for i, _ in result.blockers]
        except Exception:
            self.codes = []


def _plan_blocked(result: Any) -> PlanBlockedError:
    """把 planschema 的结果包成异常；文案取前三条中文说明。"""
    try:
        issues = result.issue_dicts(kind="blockers")
    except Exception:
        issues = []
    message = "方案没有执行：" + "；".join(
        str(i.get("message") or i.get("code")) for i in issues[:3]
    )
    return PlanBlockedError(message, result)


# 这些原因属于「不该动」：整条方案一项都没写上时，以结构化阻断报出（见 ``mutation_blocked``）。
BLOCKING_RESULT_CODES = frozenset({"protected_layer", "nested_not_supported", "ambiguous_target", "hidden_not_supported"})


def block_content_issues(doc, plan: dict[str, Any], *, bbox=None) -> list[dict[str, Any]]:
    """整条方案的预检：删除 / 移动 / 替换的旧块是不是落在保护层，或块里有保护内容。

    返回与逐项写图结果同形的失败项（``ok=False`` / ``reason`` / ``issue_code``），
    交给 ``mutation_blocked`` 保住 ``protected_layer`` / ``nested_not_supported`` 这些原因码
    和中文说明。只判明显的保护问题，没找到 / 歧义 / 坐标越界留给写图时逐项报告。
    """
    msp = doc.modelspace()
    out: list[dict[str, Any]] = []
    plan = plan or {}
    specs = (
        ("delete", "deletes", "block", ("xy", "from_xy", "from")),
        ("move", "moves", "block", ("from", "from_xy")),
        ("replace", "replaces", "from_block", ("from_xy", "from")),
    )
    for op, key, name_key, xy_keys in specs:
        for idx, item in enumerate(plan.get(key) or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get(name_key) or "").strip()
            xy = next((_as_xy(item.get(k)) for k in xy_keys if _as_xy(item.get(k))), None)
            if not name or not xy:
                continue
            located = _locate_target(doc, msp, name, xy, bbox)
            if located.get("found"):
                continue
            code = str(located.get("issue_code") or "")
            if code not in {"protected_layer", "nested_not_supported", "hidden_not_supported"}:
                continue
            out.append(
                {
                    "ok": False,
                    "action": op,
                    "block": name,
                    "reason": str(located.get("reason") or "这一项落在保护内容上，不能改"),
                    "issue_code": code,
                    "index": idx,
                    "xy": located.get("xy") or xy,
                }
            )
    return out


def mutation_blocked(results: list[dict[str, Any]]):
    """逐项结果 → ``PlanResult``：只有「全部没写上、且原因都是保护 / 嵌套 / 歧义」才算阻断。"""
    from . import planschema

    blockers = []
    for r in results:
        code = str(r.get("issue_code") or "not_applied")
        blockers.append(
            (
                planschema._issue(code, "error", str(r.get("reason") or "这一项没有写入"), "", None, r.get("block") or "", r.get("xy")),
                -1,
            )
        )
    return planschema.PlanResult(plan={}, blockers=blockers, needs_clarification=False, status="blocked")


def all_blocking(results: list[dict[str, Any]]) -> bool:
    """每一项都是失败、且失败原因都属于「不该动」（保护层 / 嵌套 / 歧义）。"""
    return bool(results) and all(
        (not r.get("ok")) and r.get("issue_code") in BLOCKING_RESULT_CODES for r in results
    )


def validate_for_apply(
    plan: Any,
    doc=None,
    *,
    bbox=None,
    region: Optional[dict[str, Any]] = None,
    index: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
    """预览与正式写入共用的写图前校验：结构（planschema）+ 坐标（placement）。

    返回 ``(归一化后的方案, issues, result)``。结构校验有阻断项就直接抛
    ``PlanBlockedError``；给了 doc 时把坐标问题也并进 issues（只作提示/回喂模型，
    写图时被拦下的项会各自失败）。调用方**不能**用「传空 issues」跳过校验：
    ``apply_mutations`` 自己也会先跑一遍结构校验。
    """
    from . import planschema

    result = planschema.normalize_plan(plan)
    if not result.valid:
        raise _plan_blocked(result)
    issues = placement_issues(doc, result.plan, bbox=bbox, region=region, index=index) if doc is not None else []
    if doc is not None:
        # 坐标校验之后再整条过一次「目标是不是图框 / 图签 / 标题」：整条都落在保护内容上的
        # 方案在建工作副本、出快照之前就拦下，不留任何文件。
        blocked = block_content_issues(doc, result.plan, bbox=bbox)
        if blocked:
            raise _plan_blocked(mutation_blocked(blocked))
    return result.plan, issues, result


def _issue_codes(issues: Optional[list[dict[str, Any]]]) -> dict[tuple[str, int], str]:
    """(op, index) → 第一个 error 级 issue code，用于给失败项补充原因。"""
    out: dict[tuple[str, int], str] = {}
    for item in issues or []:
        if not isinstance(item, dict) or item.get("severity") != "error":
            continue
        try:
            key = (str(item.get("op") or ""), int(item.get("index")))
        except (TypeError, ValueError):
            continue
        out.setdefault(key, str(item.get("code") or ""))
    return out


def _with_issue_code(result: dict[str, Any], codes: dict[tuple[str, int], str], op: str, idx: int) -> dict[str, Any]:
    """失败项若对应一条 error 级 issue，把 code 带出去。"""
    if result.get("ok") or not codes:
        return result
    code = codes.get((op, idx))
    if code and not result.get("issue_code"):
        result["issue_code"] = code
    return result


def apply_mutations(
    doc,
    plan: dict[str, Any],
    *,
    bbox,
    layer: str,
    region: Optional[dict[str, Any]] = None,
    index: Optional[dict[str, Any]] = None,
    issues: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Mutate an in-memory DXF document. Does not save, snapshot, or write history.

    直接调用也跑同一套结构校验（planschema）：有阻断项时抛 ``PlanBlockedError``，
    坏条目不会进图；``issues`` 只影响失败项的 issue_code，传什么都不能绕过校验。
    """
    from . import planschema

    check = planschema.normalize_plan(plan)
    if not check.valid:
        raise _plan_blocked(check)
    # 同角色别名冲突的条目已从归一化结果里剔除（什么都不写）。但直接把原方案交给这里、
    # 又没看 warnings 的调用方要能知道：有冲突就整体拒绝，而不是悄悄少做几项。
    conflicts = [i for i, _ in check.warnings if i.get("code") == "alias_conflict"]
    if conflicts:
        raise _plan_blocked(planschema.PlanResult(plan={}, blockers=[(c, -1) for c in conflicts], needs_clarification=True, status="needs_clarification"))
    plan = check.plan
    if issues is None:
        issues = placement_issues(doc, plan, bbox=bbox, region=region, index=index)
    msp = doc.modelspace()
    layer = _ensure_layer(doc, layer)
    results: list[dict[str, Any]] = []
    codes = _issue_codes(issues)

    for idx, item in enumerate(plan.get("deletes") or []):
        result = _hide_or_delete(doc, msp, item, bbox, layer) if isinstance(item, dict) else {
            "ok": False, "reason": "删除项格式不对", "item": item
        }
        results.append(_with_issue_code(result, codes, "delete", idx))
    # 替换的顺序是「先插新的，成了再动旧的」：新块插不上（库里没有、坐标越界、block 名错）
    # 时旧图块原样保留，绝不先删后失败、导致那一处什么都没了。
    for idx, item in enumerate(plan.get("replaces") or []):
        if not isinstance(item, dict):
            results.append({"ok": False, "reason": "替换项格式不对", "item": item})
            continue
        old_item = {
            "block": item.get("from_block") or item.get("block"),
            "xy": item.get("from_xy") or item.get("xy") or item.get("from"),
        }
        new_xy = _as_xy(item.get("xy") or item.get("to") or item.get("from_xy") or item.get("from"))
        check = _preflight_replace(doc, msp, old_item, item, bbox, new_xy)
        if not check.get("found"):
            reason = str(check.get("reason") or "这里不能安全替换")
            results.append(_with_issue_code({"ok": False, **check, "reason": reason}, codes, "replace", idx))
            continue
        found = check.get("found")
        # 整条替换一个异常都不往外逃：新块那一步抛错时旧块还没动（原样保留、新块撤回）；
        # 旧块删成功之后才出的错，如实报失败并说明「现状」。不把崩溃当成功。
        landed = False
        created: dict[str, Any] = {}
        removed: dict[str, Any] = {}
        carry: dict[str, str] = {}
        old_layer = ""
        try:
            # 旧块的一切都要在动旧块之前取好：delete_entity 会把实体和它的属性一起销毁。
            old_entity = found.get("entity")
            old_layer = _layer_name(old_entity)
            carry = _attributes_to_carry(doc, msp, item, bbox)
            ins = dict(item)
            ins.setdefault("xy", new_xy)
            created = _add_insert(
                doc, msp, ins, layer, bbox, found=found,
                region=region, index=index, snap_kind="replace",
            )
            if created.get("ok"):
                if found.get("kind") == "direct":
                    # 原位置本来就是普通顶层图块：就地删掉这一个，不做覆盖。
                    removed = _delete_direct(msp, found)
                else:
                    removed = _hide_or_delete(doc, msp, old_item, bbox, layer)
                if removed.get("ok"):
                    landed = True
                else:
                    _rollback_created(doc, msp, created)
        except Exception as exc:
            log.exception("replace crashed for %s", item.get("from_block") or item.get("block"))
            why = f"替换出错：{exc}"
            if landed:
                results.append(_with_issue_code(
                    {
                        "ok": False,
                        "reason": f"{why}；原图块已删除、新图块已写入，后续步骤没有完成，请检查这一处",
                        "block": item.get("block"),
                        "issue_code": "replace_error",
                    },
                    codes, "replace", idx,
                ))
            else:
                results.append(_with_issue_code(
                    {
                        "ok": False,
                        "reason": f"{why}；原图块保留，没有改动",
                        "block": item.get("block"),
                        "issue_code": "replace_error",
                    },
                    codes, "replace", idx,
                ))
            continue
        if landed:
            if carry:
                ref = created.get("handle") and doc.entitydb.get(created["handle"])
                try:
                    applied = _apply_carried_attribs(doc, ref, carry, old_layer)
                except Exception:
                    # 属性没抄过去不是「替换没做成」：旧块已经删了，如实标出来，不让整条替换报失败。
                    log.exception("carry attribs failed for %s", created.get("block"))
                    applied = {}
                    created["attribs_failed"] = True
                if applied:
                    created["attribs"] = applied
            results.append(removed)
            results.append(created)
        elif created.get("ok"):
            # 新块插上了、旧块没删掉：如实报失败，并说明新块已经撤回。
            results.append(_with_issue_code(
                {**removed, "reason": f"替换没有完成，原图块没能删掉，新图块已撤回：{removed.get('reason') or ''}"},
                codes, "replace", idx,
            ))
        else:
            # 新块没插上：旧块一个都还没动，保留原样，只报失败。
            results.append(_with_issue_code(
                {**created, "reason": f"替换没有执行，原图块保留：{created.get('reason') or '新图块没有写上'}"},
                codes, "replace", idx,
            ))
    for idx, item in enumerate(plan.get("moves") or []):
        if not isinstance(item, dict):
            results.append({"ok": False, "reason": "移动项格式不对", "item": item})
            continue
        name = str(item.get("block") or "").strip()
        src = _as_xy(item.get("from") or item.get("from_xy"))
        dst = _as_xy(item.get("to") or item.get("xy"))
        if not name or not src or not dst:
            results.append(_with_issue_code(
                {"ok": False, "reason": "移动需要块名、起点和终点", "item": item}, codes, "move", idx
            ))
            continue
        found = _locate_target(doc, msp, name, src, bbox)
        if not found.get("found"):
            results.append(_with_issue_code(
                {k: v for k, v in found.items() if k != "found"},
                codes, "move", idx,
            ))
            continue
        target = found["found"]
        if target["kind"] != "direct":
            results.append(_with_issue_code(
                _nested_rejection(name, target), codes, "move", idx
            ))
            continue
        if not _in_region(dst, bbox):
            results.append(_with_issue_code(
                {"ok": False, "reason": "目标坐标不在当前分区内", "block": name, "to": dst, "issue_code": "out_of_region"},
                codes, "move", idx,
            ))
            continue
        ent = target["entity"]
        # 写之前把取值都算出来：rot 是布尔/NaN/字符串时在动坐标之前就失败，不留「动了一半」。
        try:
            new_rot = _checked_rotation(item.get("rot"))
        except Exception as exc:
            results.append(_with_issue_code(
                {"ok": False, "reason": f"移动没有执行：{exc}", "block": name, "from": src}, codes, "move", idx
            ))
            continue
        try:
            old_insert = tuple(ent.dxf.insert)
            try:
                ent.dxf.insert = (dst[0], dst[1], float(getattr(ent.dxf.insert, "z", 0) or 0))
                if new_rot is not None:
                    ent.dxf.rotation = new_rot
            except Exception:
                ent.dxf.insert = old_insert
                raise
        except Exception as exc:
            results.append(_with_issue_code(
                {"ok": False, "reason": f"移动失败：{exc}", "block": name}, codes, "move", idx
            ))
            continue
        moved = {"ok": True, "action": "move", "block": name, "from": src, "to": dst}
        if new_rot is not None:
            moved["rot"] = new_rot
        results.append(moved)
    for idx, item in enumerate(plan.get("inserts") or []):
        if not isinstance(item, dict):
            results.append({"ok": False, "reason": "新增项格式不对", "item": item})
            continue
        results.append(
            _with_issue_code(
                _add_insert(doc, msp, item, layer, bbox, region=region, index=index, snap_kind="insert"),
                codes, "insert", idx,
            )
        )
    for idx, item in enumerate(plan.get("texts") or []):
        if not isinstance(item, dict):
            results.append({"ok": False, "reason": "文字项格式不对", "item": item})
            continue
        xy = _as_xy(item.get("xy"))
        txt = str(item.get("text") or "").strip()
        if not xy or not txt:
            results.append(_with_issue_code(
                {"ok": False, "reason": "文字缺少内容或坐标", "item": item}, codes, "text", idx
            ))
            continue
        if not _in_region(xy, bbox):
            results.append(_with_issue_code(
                {"ok": False, "reason": "文字坐标不在当前分区内", "xy": xy}, codes, "text", idx
            ))
            continue
        layer_name = _ensure_layer(doc, layer)
        height = float(item.get("height") or 200)
        attribs = {"layer": layer_name, "height": height}
        if item.get("style"):
            attribs["style"] = item["style"]
        try:
            msp.add_text(txt, dxfattribs=attribs).set_placement(xy)
            results.append({"ok": True, "action": "text", "text": txt, "xy": xy, "layer": layer_name})
        except Exception as exc:
            results.append({"ok": False, "reason": f"写文字失败：{exc}"})
    for idx, item in enumerate(plan.get("text_edits") or []):
        if not isinstance(item, dict):
            results.append({"ok": False, "reason": "改文字项格式不对", "item": item})
            continue
        results.append(_with_issue_code(_edit_text(doc, msp, item, bbox, layer), codes, "text_edit", idx))
    for idx, item in enumerate(plan.get("pipes") or []):
        if not isinstance(item, dict):
            results.append({"ok": False, "reason": "管线项格式不对", "item": item})
            continue
        a = _as_xy(item.get("from"))
        b = _as_xy(item.get("to"))
        if not a or not b:
            results.append(_with_issue_code(
                {"ok": False, "reason": "管线缺少端点", "item": item}, codes, "pipe", idx
            ))
            continue
        if not (_in_region(a, bbox) and _in_region(b, bbox)):
            results.append(_with_issue_code(
                {"ok": False, "reason": "管线端点不在当前分区内"}, codes, "pipe", idx
            ))
            continue
        layer_name = _ensure_layer(doc, layer)
        width = float(item.get("const_width") or 25)
        try:
            msp.add_lwpolyline([a, b], dxfattribs={"layer": layer_name, "const_width": width})
            results.append({"ok": True, "action": "pipe", "from": a, "to": b, "layer": layer_name})
        except Exception as exc:
            results.append({"ok": False, "reason": f"画管线失败：{exc}"})
    _apply_overlay_draw_order(doc, msp, results)
    return results


# ---------------------------------------------------------------- 改已有文字

TEXT_EDIT_RADIUS_MM = 1500.0     # 给了坐标时先在这一圈里找
TEXT_EDIT_TEXT_TYPES = {"TEXT", "MTEXT"}


def _text_edit_pool(msp) -> list[tuple[Any, Any]]:
    """可改的文字：(实体, 宿主)。宿主是 None（模型空间的 TEXT/MTEXT）或 (所属 INSERT, 矩阵)。"""
    pool: list[tuple[Any, Any]] = []
    for ent in msp:
        try:
            kind = ent.dxftype()
        except Exception:
            continue
        if kind in TEXT_EDIT_TEXT_TYPES:
            pool.append((ent, None))
            continue
        if kind != "INSERT":
            continue
        try:
            matrix = ent.matrix44()
            attribs = list(ent.attribs)
        except Exception:
            continue
        for attrib in attribs:
            pool.append((attrib, (ent, matrix)))
    return pool


OUT_OF_SCOPE_REASON = "当前分区里没有找到原文字「{text}」（别处图幅上的同名文字不改）"


def _matched_layer_protected(ent, host) -> Optional[str]:
    """改这条文字会落到保护图层上时返回这个图层名（显示图层，含 layer 0 继承），否则 None。

    host 是块内文字的 (宿主 INSERT, 矩阵)。宿主块里有保护内容（图框、图签）
    时，块里的字就是那些内容的一部分，同样不改。
    """
    from . import planschema

    names = [planschema.effective_layer(ent)]
    if host is not None:
        names.append(planschema.effective_layer(host[0]))
        where = planschema.analyze_insert(host[0]).get("protected_where")
        if where:
            names.append(str(where))
    for name in names:
        if planschema.is_protected_layer(name):
            return name
    return None


def _find_text_to_edit(msp, from_text: str, xy, bbox=None) -> dict[str, Any]:
    """在当前分区里按可见文字精确匹配（只规整空白），标点算数；失败再按全/半角放宽一次。

    - 范围：命中点必须落在 bbox（当前分区，``_in_region`` 同样的外扩）内；bbox 为空
      表示整张图。只有别的分区里有匹配 → 明确说「当前分区里没有找到」。
    - 精确匹配：多处取离 xy 最近的一处；一样近就是歧义，绝不猜。
    - 放宽（忽略全/半角与空白）：放宽口径命中的每一处，只要和另一处**精确**命中相距
      < AMBIGUITY_MM，就说明「差一个全/半角」分不清该改哪一处 → 报歧义，不按距离挑；
      否则只在放宽口径**恰好一处**时采用，标 width-normalized。
    - 子串、去标点、近似字一律不算。
    """
    from . import dxftext

    want = dxftext.normalize_whitespace(from_text)
    pool = _text_edit_pool(msp)
    cleaned = [(ent, host, dxftext.clean_entity_text(ent)) for ent, host in pool]
    outside = {
        "ok": False,
        "reason": OUT_OF_SCOPE_REASON.format(text=from_text),
        "from_text": from_text,
        "xy": xy,
    }
    missing = {"ok": False, "reason": f"没有找到原文字「{from_text}」", "from_text": from_text, "xy": xy}
    ambiguous = {
        "ok": False,
        "reason": f"原文字有多处，请给出更准确的位置（「{from_text}」）",
        "from_text": from_text,
        "xy": xy,
    }

    def _scan(key):
        inside: list[tuple] = []
        outside_hits = 0
        for ent, host, visible in cleaned:
            if not visible or key(visible) != key(want):
                continue
            loc = _text_edit_distance(ent, host, xy)
            if loc is None:
                continue
            dist, point = loc
            if bbox is not None and not _in_region(point, bbox):
                outside_hits += 1
                continue
            inside.append((dist, ent, host, visible, point))
        return inside, outside_hits

    def _dedupe(hits):
        seen = set()
        out = []
        for h in hits:
            hid = _handle_of(h[1])
            if hid in seen:
                continue
            seen.add(hid)
            out.append(h)
        return out

    exact, exact_outside = _scan(lambda v: v)
    if exact:
        found = _resolve(_dedupe(exact), ambiguous)
        if found.get("ok"):
            found["match"] = "exact"
        return found
    wide, wide_outside = _scan(dxftext.width_fold)
    if wide:
        unique = _dedupe(wide)
        if len(unique) > 1:
            return ambiguous
        wide_dist = unique[0][0]
        for other in _dedupe(exact):
            if abs(other[0] - wide_dist) < AMBIGUITY_MM:
                return ambiguous
        dist, ent, host, visible, point = unique[0]
        return {
            "ok": True,
            "entity": ent,
            "host": host,
            "from": visible,
            "hit_xy": point,
            "dist": dist,
            "match": "width-normalized",
        }
    if exact_outside or wide_outside:
        return outside
    return missing


def _resolve(hits: list[tuple], ambiguous: dict[str, Any]) -> dict[str, Any]:
    """精确匹配的多处候选：取最近的；一样近（差 < AMBIGUITY_MM）就是歧义。"""
    hits = sorted(hits, key=lambda h: h[0])
    if len(hits) >= 2 and abs(hits[1][0] - hits[0][0]) < AMBIGUITY_MM:
        return ambiguous
    dist, ent, host, visible, point = hits[0]
    return {"ok": True, "entity": ent, "host": host, "from": visible, "hit_xy": point, "dist": dist}


def _text_edit_distance(ent, host, xy):
    """文字到 xy 的距离与最近点；指向文字框内按 0 算。"""
    from . import dxftext

    matrix = host[1] if host else None
    points = dxftext.text_points(ent)
    if not points:
        return None
    if matrix is not None:
        world = []
        for x, y in points:
            p = matrix.transform(Vec3(x, y, 0))
            world.append((float(p.x), float(p.y)))
        points = world
    if xy is None:
        return 0.0, [float(points[0][0]), float(points[0][1])]
    dist = min(math.hypot(px - xy[0], py - xy[1]) for px, py in points)
    nearest = min(points, key=lambda p: math.hypot(p[0] - xy[0], p[1] - xy[1]))
    if host is None:
        extent = dxftext.text_extent(ent)
        if extent:
            pad = 0.5 * max(extent[2] - extent[0], extent[3] - extent[1], 1.0)
            if extent[0] - pad <= xy[0] <= extent[2] + pad and extent[1] - pad <= xy[1] <= extent[3] + pad:
                dist = 0.0
    return dist, [float(nearest[0]), float(nearest[1])]


def _set_text_value(ent, new_text: str) -> None:
    """原地改写可见文字；样式、字高、图层、旋转、宽度因子、对齐点、颜色一律不碰。

    写之前先用同一套口径（``dxftext.edit_visible``）复核：可见字不一致就不落笔。
    """
    from . import dxftext

    if ent.dxftype() == "MTEXT":
        new_raw = dxftext.rewrite_mtext_body(str(ent.text or ""), new_text)
        dxftext.edit_visible_matches(new_raw, new_text)
        ent.text = new_raw
        return
    new_raw = dxftext.encode_text_control(new_text)
    dxftext.edit_visible_matches(new_raw, new_text, kind="txt")
    ent.dxf.text = new_raw


def _edit_text(doc, msp, item: dict[str, Any], bbox, layer: str) -> dict[str, Any]:
    from . import dxftext

    from_text = str(item.get("from_text") or "")
    new_text = str(item.get("text") if item.get("text") is not None else "")
    xy = _as_xy(item.get("xy"))
    if not from_text.strip():
        return {"ok": False, "reason": "改文字缺少原文字（from_text）", "item": item}
    if not new_text.strip():
        return {"ok": False, "reason": "改文字缺少新文字（text）", "item": item}
    if not xy:
        return {"ok": False, "reason": "改文字缺少坐标（xy）", "item": item}
    if not _in_region(xy, bbox):
        return {"ok": False, "reason": "改文字的坐标不在当前分区内", "from_text": from_text, "xy": xy}
    found = _find_text_to_edit(msp, from_text, xy, bbox)
    if not found.get("ok"):
        return found
    ent = found["entity"]
    host = found["host"]
    protected_layer = _matched_layer_protected(ent, host)
    if protected_layer:
        # 图框、图签、标题上的字（含块定义继承来的图层）一律不改。
        return {
            "ok": False,
            "reason": f"原文字「{found.get('from') or from_text}」在保护图层「{protected_layer}」上，图框、图签、标题的文字不改",
            "from_text": from_text,
            "xy": found.get("hit_xy") or xy,
            "handle": _handle_of(ent),
            "issue_code": "protected_layer",
        }
    handle = _handle_of(ent)
    layer_before = _layer_name(ent)
    try:
        _set_text_value(ent, new_text)
    except Exception as exc:
        log.exception("text edit failed")
        return {"ok": False, "reason": f"改文字失败：{exc}", "from_text": from_text, "handle": handle}
    # 回读：与 _set_text_value 同样比「落笔的原始串」，用同一套空白口径
    # （dxftext.edit_visible），标点逐字比。MTEXT 检查时逐行去行尾空白，与 check_visible
    # 一致；不用 clean_entity_text 的整段折叠，免得把 "a \\n b" 误当成 "a b"。
    kind = "mtext" if ent.dxftype() == "MTEXT" else "txt"
    try:
        visible = dxftext.edit_visible_matches(dxftext.entity_raw(ent), new_text, kind=kind)
    except ValueError as exc:
        return {"ok": False, "reason": f"改文字后回读不一致：{exc}", "from_text": from_text, "handle": handle}
    visible = " ".join(visible.split("\n"))
    result: dict[str, Any] = {
        "ok": True,
        "action": "text_edit",
        "from": found["from"],
        "text": visible,
        "requested": new_text,
        "xy": found["hit_xy"] or xy,
        "handle": handle,
        "layer": layer_before,
        "kind": ent.dxftype(),
    }
    if found.get("match") == "width-normalized":
        result["match"] = "width-normalized"
    else:
        result["match"] = "exact"
    if host is not None:
        result["host_handle"] = _handle_of(host[0])
    return result


def _repair_material_table(doc) -> int:
    """摘掉材质表里指向已丢失对象的空引用，缺的按需重建。

    DWG 转 DXF 的产物常把 OBJECTS 段的 MATERIAL 丢掉，只留下 ACAD_MATERIAL
    字典里的 handle。ezdxf 保存时会在 _update_header_vars 里取
    materials.get("ByLayer").dxf，拿到的是字符串就抛 AttributeError，
    整次写入全废，用户看到的就是「确认后报 500、图没变」。
    """
    fixed = 0
    try:
        table = doc.rootdict.get("ACAD_MATERIAL")
    except Exception:
        table = None
    if table is None:
        return 0
    for name in ("ByBlock", "ByLayer", "Global"):
        try:
            entry = table.get(name)
        except Exception:
            entry = None
        # 正常是 Material 实体；拿到 str 说明引用已经悬空。
        if entry is not None and not hasattr(entry, "dxf"):
            try:
                table.discard(name)
                fixed += 1
            except Exception:
                log.exception("摘除坏材质条目失败：%s", name)
    for name in ("ByBlock", "ByLayer", "Global"):
        try:
            if table.get(name) is None:
                doc.materials.new(name)
                fixed += 1
        except Exception:
            log.exception("重建材质 %s 失败", name)
    if fixed:
        log.info("材质表空引用已修复 %s 处", fixed)
    return fixed


def _read_doc(path: Path, encoding: str = "utf-8"):
    try:
        doc = ezdxf.readfile(str(path), encoding=encoding)
    except Exception:
        doc = ezdxf.readfile(str(path))
    _repair_material_table(doc)
    return doc


def _save_doc(doc, dest: Path) -> None:
    tmp = dest.with_suffix(".write.dxf")
    try:
        try:
            doc.saveas(str(tmp))
        except Exception:
            # 内存里的文档偶尔又冒出空引用（比如替换块时带进来的）；
            # 再修一次材质表后重试，修不动就把原错误抛给调用方。
            if not _repair_material_table(doc):
                raise
            log.warning("保存失败，修复材质表后重试一次")
            doc.saveas(str(tmp))
        tmp.replace(dest)
    except Exception:
        if tmp.is_file():
            tmp.unlink()
        raise


def next_revision_number(proj: dict[str, Any]) -> int:
    hist = load_history(proj)
    return (hist["entries"][-1]["rev"] + 1) if hist["entries"] else 1


@_with_project_write_lock
def apply_plan(
    proj: dict[str, Any],
    plan: dict[str, Any],
    *,
    region: Optional[dict[str, Any]] = None,
    message_id: Optional[str] = None,
    user_text: str = "",
) -> dict[str, Any]:
    if not plan or not isinstance(plan, dict):
        raise ValueError("没有可写入的方案。")
    bbox = (region or {}).get("bbox")
    encoding = proj.get("encoding") or "utf-8"
    # 先在当前图上跑一遍（只读）：目标在保护层/共享块里这类问题在写任何文件之前就拦下。
    source_doc = _read_doc(current_dxf(proj), encoding)
    plan, issues, _result = validate_for_apply(plan, source_doc, bbox=bbox, region=region)
    new_work = _new_work_copy(proj)
    dest = work_path(proj)
    official = official_dxf(proj)
    hist = load_history(proj)
    next_rev = (hist["entries"][-1]["rev"] + 1) if hist["entries"] else 1
    original_snap = original_snapshot_path(proj)
    had_original = original_snap.is_file() and original_snap.stat().st_size > 0
    # 原图快照也等到这一步真的写成了再补：这一次没写成就什么都不留。
    snap = _snapshot_path(proj, next_rev)
    _write_snapshot(dest, snap)

    def _abandon(reason: str) -> None:
        """这一步没写成：不保存写了一半的内存图，撤掉刚建的快照；
        若这一步本来会是第一次写入（还没有工作副本），也不留下新工作副本。"""
        if snap.is_file():
            snap.unlink(missing_ok=True)
        if not had_original:
            try:
                original_snap.unlink(missing_ok=True)
            except OSError:
                log.exception("remove original snapshot after failed first apply")
        if new_work:
            # 还没有任何一步写进去过（load_history 读不到历史），这次新建的工作副本没人认领。
            try:
                work_path(proj).unlink(missing_ok=True)
            except OSError:
                log.exception("remove work copy after failed apply")
        if reason:
            log.warning("apply aborted: %s", reason)

    # 与预览同一条路径（同一函数读文件），在快照后、保存前写内存副本。
    doc = _read_doc(dest, encoding)
    layer = _ensure_layer(doc, f"{NEW_LAYER_PREFIX}-{next_rev}")
    try:
        results = apply_mutations(doc, plan, bbox=bbox, layer=layer, region=region, issues=issues)
    except BaseException as exc:
        # 写内存副本时抛错（包括被拦下的方案）：dest 没被覆盖，但快照 / 新工作副本要撤掉。
        # 打断（CancelledError 等）原样再抛，不吞。
        _abandon(f"apply_mutations raised {type(exc).__name__}")
        raise
    snapped_count = sum(1 for r in results if r.get("snapped"))

    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    if ok_n == 0:
        reasons = [r.get("reason") for r in results if r.get("reason")]
        _abandon(reasons[0] if reasons else "方案里没有能写进工作图的改动。")
        if all_blocking(results):
            raise _plan_blocked(mutation_blocked(results))
        raise ValueError(reasons[0] if reasons else "方案里没有能写进工作图的改动。")
    crashed = [r for r in results if not r.get("ok") and r.get("issue_code") == "replace_error"]
    if crashed:
        # 有一项替换炸在半路上（见 apply_mutations 的替换循环）：这一次不写盘，如实报失败，
        # 不让写入端把「替换出错」当成已完成的这一步（哪怕别的改动本来能写）。
        reasons = [r.get("reason") for r in crashed if r.get("reason")]
        _abandon("replace crashed")
        raise ValueError(
            (reasons[0] if reasons else "替换出错")
            + "；这一步没有写入工作图，其他改动也没有保存，请重试或拆开执行"
        )

    old_base = None
    if bbox:
        try:
            from . import cad_render
            old_base = cad_render.base_cache_info(dest, list(bbox))
        except Exception:
            old_base = None

    try:
        _ensure_original_snapshot(proj)
        _save_doc(doc, dest)
        _write_session_source(proj, dest)
    except BaseException as exc:
        # 保存中途失败：dest 是原子替换的（失败时原文件不动），快照 / 新工作副本照样撤掉。
        _abandon(f"save failed: {type(exc).__name__}")
        raise

    if old_base and bbox:
        try:
            from . import cad_render
            dirty = dirty_bbox_from_results(results, view_bbox=list(bbox))
            if dirty and not cad_render.nearly_full_dirty(dirty, list(bbox)):
                seed_png = dest.with_name(dest.stem + ".cache-seed.png")
                ents = plan_overlay_entities(doc, results)
                cad_render.composite_dirty_preview(
                    doc, list(bbox), dirty, old_base, seed_png, stamp=None, entities=ents,
                )
                cad_render.seed_base_cache(dest, list(bbox), seed_png, dpi=old_base["dpi"])
                official_path = official_dxf(proj)
                if official_path.resolve() != Path(dest).resolve():
                    cad_render.seed_base_cache(official_path, list(bbox), seed_png, dpi=old_base["dpi"])
                try:
                    seed_png.unlink()
                except Exception:
                    pass
        except Exception:
            log.exception("seed preview cache after confirm failed")

    entry = {
        "rev": next_rev,
        "ts": projects.now_iso(),
        "message_id": message_id,
        "user_text": user_text,
        "region": (region or {}).get("id"),
        "layer": layer,
        "snapshot": snap.name,
        "ok": ok_n,
        "failed": fail_n,
        "results": _public_results(results),
        "summary": (plan.get("reply") or "")[:200],
    }
    hist["entries"].append(entry)
    hist["source_written"] = True
    _prune_history(proj, hist)
    save_history(proj, hist)
    _drop_render_cache(dest, official, original_view_path(proj))

    return {
        "ok": True,
        "rev": next_rev,
        "layer": layer,
        "ok_count": ok_n,
        "failed_count": fail_n,
        "results": _public_results(results),
        "issues": issues,
        "snapped_count": snapped_count,
        "dirty": True,
        "can_undo": True,
        "official_untouched": False,
        "source_written": True,
    }


def _public_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for res in results:
        item = {k: v for k, v in res.items() if k not in {"found", "entity"}}
        out.append(item)
    return out



PIPE_LAYER_HINTS = ("PIPE", "污水", "给水", "雨水", "废水", "通气", "排水", "水管")
OVERLAY_LOCAL_SPAN = 8000.0


def _handle_of(ent) -> Optional[str]:
    try:
        return ent.dxf.handle
    except Exception:
        return None


def _layer_name(ent) -> str:
    try:
        return str(getattr(ent.dxf, "layer", "") or "")
    except Exception:
        return ""


def _is_pipeish(ent) -> bool:
    layer = _layer_name(ent)
    return any(hint in layer for hint in PIPE_LAYER_HINTS)


def _boxes_overlap(a, b, pad: float = 0.0) -> bool:
    return not (
        a[2] + pad < b[0] or a[0] - pad > b[2] or a[3] + pad < b[1] or a[1] - pad > b[3]
    )


def _clean_bbox(ent):
    """Bounding box that ignores unset (0,0) TEXT alignment points."""
    from . import cad_render

    pts = cad_render.entity_points(ent)
    if not pts:
        return None
    real = [p for p in pts if abs(p[0]) > 1.0 or abs(p[1]) > 1.0]
    use = real or pts
    xs = [p[0] for p in use]
    ys = [p[1] for p in use]
    return (min(xs), min(ys), max(xs), max(ys))


def tight_mutation_boxes(
    results: list[dict[str, Any]],
    *,
    point_extent: float = 800.0,
) -> list[list[float]]:
    boxes: list[list[float]] = []
    for res in results or []:
        if not res.get("ok"):
            continue
        if res.get("bbox") and len(res["bbox"]) >= 4:
            bb = [float(v) for v in res["bbox"][:4]]
            boxes.append(bb)
        for key in ("xy", "to", "from", "from_xy"):
            xy = res.get(key)
            if isinstance(xy, (list, tuple)) and len(xy) >= 2:
                try:
                    x, y = float(xy[0]), float(xy[1])
                except (TypeError, ValueError):
                    continue
                boxes.append([x - point_extent, y - point_extent, x + point_extent, y + point_extent])
    return boxes


def plan_overlay_entities(doc, results: list[dict[str, Any]], *, pad: float = 400.0):
    """Covers, new inserts, and nearby pipes/labels — not the rest of the sheet."""
    from . import cad_render

    cover_handles = {r.get("handle") for r in results if r.get("ok") and r.get("action") == "cover" and r.get("handle")}
    insert_handles = {r.get("handle") for r in results if r.get("ok") and r.get("action") == "insert" and r.get("handle")}
    host_handles = set()
    for res in results or []:
        host = (res.get("found") or {}).get("host")
        if host is None:
            continue
        h = _handle_of(host)
        if h:
            host_handles.add(h)
    boxes = tight_mutation_boxes(results)
    cover_boxes = [
        [float(v) for v in r["bbox"][:4]]
        for r in (results or [])
        if r.get("ok") and r.get("action") == "cover" and r.get("bbox") and len(r["bbox"]) >= 4
    ]
    if not cover_handles and not insert_handles:
        return None
    msp = doc.modelspace()
    covers, inserts, rest = [], [], []
    text_types = {"TEXT", "MTEXT", "ATTDEF", "ATTRIB", "MULTILEADER", "DIMENSION"}
    for ent in msp:
        handle = _handle_of(ent)
        if handle in cover_handles:
            covers.append(ent)
            continue
        if handle in insert_handles:
            inserts.append(ent)
            continue
        if handle in host_handles:
            continue
        bb = _clean_bbox(ent)
        if not bb:
            continue
        kind = ent.dxftype()
        # Labels already live in the cached base; only redraw ones a wipeout might hide.
        if kind in text_types:
            if any(_boxes_overlap(bb, box, pad=min(pad, 80)) for box in cover_boxes):
                rest.append(ent)
            continue
        if not any(_boxes_overlap(bb, box, pad=pad) for box in boxes):
            continue
        if _is_pipeish(ent):
            rest.append(ent)
            continue
        span_x = float(bb[2]) - float(bb[0])
        span_y = float(bb[3]) - float(bb[1])
        if span_x < OVERLAY_LOCAL_SPAN and span_y < OVERLAY_LOCAL_SPAN:
            rest.append(ent)
    # Keep pipes/labels after covers and new fixtures, matching SORTENTS order.
    try:
        mapping = dict(msp.get_redraw_order() or [])
        if mapping:
            from ezdxf import reorder
            rest = list(reorder.ascending(rest, mapping))
    except Exception:
        pass
    return covers + inserts + rest


def dirty_bbox_from_results(
    results: list[dict[str, Any]],
    *,
    pad: float = DIRTY_PAD,
    point_extent: float = DIRTY_POINT_EXTENT,
    view_bbox=None,
) -> Optional[list[float]]:
    """Union of cover boxes, insert/move/pipe points, plus a generous pad."""
    xs: list[float] = []
    ys: list[float] = []

    def _add_box(bb):
        if not bb or len(bb) < 4:
            return
        xs.extend([float(bb[0]), float(bb[2])])
        ys.extend([float(bb[1]), float(bb[3])])

    def _add_xy(xy, extent=point_extent):
        # 改文字的结果里 from 是原文字（字符串），不是坐标。
        if not isinstance(xy, (list, tuple)) or len(xy) < 2:
            return
        try:
            x, y = float(xy[0]), float(xy[1])
        except (TypeError, ValueError):
            return
        xs.extend([x - extent, x + extent])
        ys.extend([y - extent, y + extent])

    for res in results or []:
        if not res.get("ok"):
            continue
        if res.get("bbox"):
            _add_box(res["bbox"])
        _add_xy(res.get("xy"))
        _add_xy(res.get("from"))
        _add_xy(res.get("to"))
        _add_xy(res.get("from_xy"))
        found = res.get("found") or {}
        if found.get("xy"):
            _add_xy(found["xy"])
    if not xs or not ys:
        return None
    box = [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]
    if view_bbox and len(view_bbox) == 4:
        box = [
            max(float(view_bbox[0]), box[0]),
            max(float(view_bbox[1]), box[1]),
            min(float(view_bbox[2]), box[2]),
            min(float(view_bbox[3]), box[3]),
        ]
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
    return box


def _preview_doc(proj: dict[str, Any], plan: dict[str, Any], region: dict[str, Any]):
    src = current_dxf(proj)
    encoding = proj.get("encoding") or "utf-8"
    doc = _read_doc(src, encoding)
    next_rev = next_revision_number(proj)
    layer = _ensure_layer(doc, f"{NEW_LAYER_PREFIX}-{next_rev}")
    bbox = (region or {}).get("bbox")
    # 结构与坐标校验在这里先跑一遍（与 apply_plan 同一函数）：被拦下的方案预览直接失败，
    # 不画一张「看起来没问题」的图。之后预览与正式写入走同一套吸附/保护判断，所见即所得。
    plan, _issues, _result = validate_for_apply(plan, doc, bbox=bbox, region=region)
    results = apply_mutations(doc, plan, bbox=bbox, layer=layer, region=region)
    ok_n = sum(1 for r in results if r.get("ok"))
    if ok_n == 0:
        reasons = [r.get("reason") for r in results if r.get("reason")]
        # 目标落在保护层 / 共享块里：和 apply_plan 一样给出结构化阻断（只含这类原因时）。
        if all_blocking(results):
            raise _plan_blocked(mutation_blocked(results))
        raise ValueError(reasons[0] if reasons else "方案里没有能预览的改动。")
    return doc, results, ok_n, src, encoding, bbox


def render_plan_preview(
    proj: dict[str, Any],
    plan: dict[str, Any],
    region: dict[str, Any],
    dest: Path,
    *,
    incremental: bool = True,
    crop: Optional[bool] = None,
) -> dict[str, Any]:
    """Apply the plan to a temporary in-memory copy and render it. No history, no writes.

    Default: 渲染「改动附近的取景窗口」——改动框按 placement.context_bbox 扩到
    带周边环境（墙体、轴网、同类构件都在），窗口大小以改动约占一半为准。只裁
    改动框会得到一张看不出原貌的小图；整幅又看不清细节。图上再用 annotate_changes
    把改动范围圈出来。传 incremental=False 或 crop=False 可强制整幅渲染。
    """
    from . import cad_render

    dest = Path(dest)
    # _preview_doc 里先跑与 apply_plan 相同的校验：阻断项抛 PlanBlockedError，连预览文件都不建。
    plan, _issues, _result = validate_for_apply(plan)
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc, results, ok_n, src, encoding, bbox = _preview_doc(proj, plan, region)
    view = list(bbox) if bbox and len(bbox) == 4 else []
    dirty = dirty_bbox_from_results(results, view_bbox=view or None)
    want_crop = True if crop is None else bool(crop)
    force_full = (incremental is False) or (want_crop is False)
    used_crop = False
    crop_bbox = None
    if (
        not force_full
        and view
        and dirty
        and not cad_render.nearly_full_dirty(dirty, view)
    ):
        try:
            # 只裁改动框会得到一张一两百像素的小图，又没有周边墙体轴网可比对。
            # 先把改动框扩成带环境的窗口，再按窗口重新渲染：既看得清，也知道位置。
            ctx = placement.context_bbox(dirty, view) or list(dirty)
            cad_render.render_doc_preview(doc, ctx, dest, stamp=STAMP_PREVIEW)
            cad_render.annotate_changes(dest, ctx, dirty, results)
            # world_limits 返回的是 (x0, x1, y0, y1)，别按 bbox 的顺序接。
            lx0, lx1, ly0, ly1 = cad_render.world_limits(ctx)
            crop_bbox = [lx0, ly0, lx1, ly1]
            used_crop = True
        except Exception:
            log.exception("crop plan preview failed, falling back to full render")
            used_crop = False
            crop_bbox = None
    if not used_crop:
        cad_render.render_doc_preview(doc, view, dest, stamp=STAMP_PREVIEW)
    # Preview must not leave an undo step or a work/source write.
    return {
        "ok": True,
        "preview": True,
        "incremental": False,
        "crop": used_crop,
        "crop_bbox": list(crop_bbox) if used_crop and crop_bbox else None,
        "dirty_bbox": dirty,
        "focus_bbox": dirty,
        "ok_count": ok_n,
        "failed_count": len(results) - ok_n,
        "results": _public_results(results),
        "path": str(dest),
        "can_undo": bool((load_history(proj).get("entries") or [])),
    }


def export_sheet_pdf(
    proj: dict[str, Any],
    region: dict[str, Any],
    dest: Path,
    *,
    source: Optional[str] = None,
) -> dict[str, Any]:
    """Export the current sheet. A pending plan for this region yields the post-apply drawing."""
    from . import cad_render
    from .prompts import plan_has_edits

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    bbox = list((region or {}).get("bbox") or [])
    if len(bbox) != 4:
        raise ValueError("没有可导出的图幅范围。")
    official = (source or "").strip().lower() in {"official", "source", "original"}
    encoding = proj.get("encoding") or "utf-8"
    pending = None if official else last_pending_plan(proj)
    if pending and plan_has_edits(pending.get("plan")):
        pregion = pending.get("region")
        if not pregion or pregion == (region or {}).get("id"):
            # Always export the full current sheet (post-apply). Do not reuse a
            # plan-preview overlay, which may be a tight dirty crop.
            doc, results, _ok_n, src, _encoding, _bbox = _preview_doc(proj, pending["plan"], region)
            dirty = dirty_bbox_from_results(results, view_bbox=bbox)
            base_info = cad_render.base_cache_info(src, bbox)
            ents = plan_overlay_entities(doc, results) if base_info and dirty else None
            cad_render.export_doc_pdf(
                doc,
                bbox,
                dest,
                stamp=STAMP_PREVIEW,
                base_info=base_info,
                dirty_bbox=dirty,
                entities=ents,
            )
            return {
                "ok": True,
                "pending": True,
                "stamp": True,
                "path": str(dest),
            }
    dxf = original_preview_dxf(proj) if official else current_dxf(proj)
    cad_render.export_region_pdf(Path(dxf), bbox, dest, encoding=encoding)
    return {"ok": True, "pending": False, "stamp": False, "path": str(dest)}


@_with_project_write_lock
def undo_last(proj: dict[str, Any]) -> dict[str, Any]:
    hist = load_history(proj)
    if not hist["entries"]:
        raise ValueError("没有可撤回的改动。")
    entry = hist["entries"].pop()
    snap = revisions_dir(proj) / Path(entry.get("snapshot") or f"rev-{entry['rev']:04d}.dxf.gz").name
    dest = work_path(proj)
    official = official_dxf(proj)
    if not snap.is_file():
        hist["entries"].append(entry)
        save_history(proj, hist)
        raise ValueError("找不到这一步的快照，无法撤回。")
    if not hist["entries"]:
        # Restored to the pre-edit source: drop the working copy and restore session DXF.
        _restore_to_official = True
        restore_from = original_snapshot_path(proj)
        if not (restore_from.is_file() and restore_from.stat().st_size > 0):
            restore_from = snap
        _restore_snapshot(restore_from, official)
        dest.unlink(missing_ok=True)
        snap.unlink(missing_ok=True)
        hist["source_written"] = False
    else:
        _restore_to_official = False
        _restore_snapshot(snap, dest)
        _write_session_source(proj, dest)
        snap.unlink(missing_ok=True)
        hist["source_written"] = True
    save_history(proj, hist)
    _drop_render_cache(dest, official, original_view_path(proj))
    return {
        "ok": True,
        "undone": entry,
        "dirty": is_dirty(proj),
        "can_undo": bool(hist["entries"]),
        "restored_official": _restore_to_official,
        "official_untouched": _restore_to_official,
        "source_written": bool(hist.get("source_written")),
    }


def excerpt_working(proj: dict[str, Any], *, max_chars: int = 600) -> str:
    hist = load_history(proj)
    if not hist["entries"]:
        return ""
    lines = ["【工作图已确认改动】"]
    for entry in hist["entries"][-5:]:
        ok_n = entry.get("ok")
        fail_n = entry.get("failed")
        line = f"- 第 {entry.get('rev')} 步，图层 {entry.get('layer')}：成功 {ok_n}"
        if fail_n:
            line += f"，未写入 {fail_n}"
        covers = sum(1 for r in (entry.get("results") or []) if r.get("action") == "cover")
        inserts = sum(1 for r in (entry.get("results") or []) if r.get("action") == "insert")
        deletes = sum(1 for r in (entry.get("results") or []) if r.get("action") == "delete")
        edits = [r for r in (entry.get("results") or []) if r.get("action") == "text_edit" and r.get("ok")]
        bits = []
        if covers:
            bits.append(f"覆盖{covers}处")
        if inserts:
            bits.append(f"新增{inserts}处")
        if deletes:
            bits.append(f"删除{deletes}处")
        if edits:
            pairs = "；".join(f"「{e.get('from')}」→「{e.get('text')}」" for e in edits[:4])
            bits.append(f"改文字{len(edits)}处（{pairs}）")
        if bits:
            line += "，" + "、".join(bits)
        lines.append(line)
    text = "\n".join(lines)
    return text[:max_chars]
