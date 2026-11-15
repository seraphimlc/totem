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
COVER_PAD_MM = 20.0
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


def _snapshot_path(proj: dict[str, Any], rev: int) -> Path:
    return revisions_dir(proj) / f"rev-{rev:04d}.dxf.gz"


def _write_snapshot(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.gz")
    with src.open("rb") as fh, gzip.open(tmp, "wb") as out:
        shutil.copyfileobj(fh, out)
    tmp.replace(dest)


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


def _world_bbox(doc, name: str, matrix: Matrix44) -> Optional[tuple[float, float, float, float]]:
    ext = _block_extents(doc, name)
    if ext is None:
        origin = matrix.transform(Vec3(0, 0, 0))
        return (origin.x - 50, origin.y - 50, origin.x + 50, origin.y + 50)
    corners = [
        (ext.extmin.x, ext.extmin.y),
        (ext.extmax.x, ext.extmin.y),
        (ext.extmax.x, ext.extmax.y),
        (ext.extmin.x, ext.extmax.y),
    ]
    pts = [matrix.transform(Vec3(x, y, 0)) for x, y in corners]
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _find_insert(msp, block: str, xy, bbox, pad: float = 2000.0):
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
    return hits[0] if hits else None


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
    """Locate a top-level modelspace INSERT, or a nested INSERT in this view only."""
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
            best = found
    return best


def _add_cover(msp, world_bb: tuple[float, float, float, float], layer: str) -> dict[str, Any]:
    x0, y0, x1, y1 = world_bb
    pad = COVER_PAD_MM
    verts = [
        (x0 - pad, y0 - pad),
        (x1 + pad, y0 - pad),
        (x1 + pad, y1 + pad),
        (x0 - pad, y1 + pad),
    ]
    try:
        wipe = msp.add_wipeout(verts, dxfattribs={"layer": layer})
        # ezdxf's add_wipeout ignores layer in dxfattribs and leaves it on "0".
        try:
            wipe.dxf.layer = layer
        except Exception:
            pass
        return {"ok": True, "action": "cover", "kind": "wipeout", "bbox": [x0, y0, x1, y1], "layer": layer, "handle": wipe.dxf.handle}
    except Exception:
        log.exception("wipeout cover failed, falling back")
    try:
        hatch = msp.add_hatch(color=7, dxfattribs={"layer": layer, "true_color": 0xFFFFFF})
        hatch.set_solid_fill(color=7)
        hatch.paths.add_polyline_path(verts, is_closed=True)
        return {"ok": True, "action": "cover", "kind": "hatch", "bbox": [x0, y0, x1, y1], "layer": layer, "handle": hatch.dxf.handle}
    except Exception:
        log.exception("hatch cover failed, falling back")
    try:
        solid = msp.add_solid(verts, dxfattribs={"layer": layer, "color": 7, "true_color": 0xFFFFFF})
        return {"ok": True, "action": "cover", "kind": "solid", "bbox": [x0, y0, x1, y1], "layer": layer, "handle": solid.dxf.handle}
    except Exception as exc:
        return {"ok": False, "reason": f"无法覆盖旧符号：{exc}"}


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
    if name not in doc.blocks and not _import_block(doc, name):
        return {"ok": False, "reason": f"图纸和默认组件库里都没有块「{name}」", "block": name}
    layer_name = _ensure_layer(doc, layer)
    sx, sy, sz = _scale_tuple(item.get("scale"))
    rot = _choose_rotation(doc, item, found, name)
    ref = msp.add_blockref(
        name,
        insert=xy,
        dxfattribs={"layer": layer_name, "rotation": rot, "xscale": sx, "yscale": sy, "zscale": sz},
    )
    out = {"ok": True, "action": "insert", "block": name, "xy": xy, "layer": layer_name, "rot": rot, "handle": ref.dxf.handle}
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


def _hide_or_delete(doc, msp, item: dict[str, Any], bbox, layer: str) -> dict[str, Any]:
    name = str(item.get("block") or item.get("from_block") or "").strip()
    xy = _as_xy(item.get("xy") or item.get("from_xy") or item.get("from"))
    if not name or not xy:
        return {"ok": False, "reason": "删除需要块名和坐标", "item": item}
    found = find_symbol(doc, msp, name, xy, bbox)
    if not found:
        return {"ok": False, "reason": f"当前分区里没有找到「{name}」", "block": name, "xy": xy}
    if found["kind"] == "direct":
        ent = found["entity"]
        try:
            msp.delete_entity(ent)
        except Exception:
            ent.destroy()
        return {"ok": True, "action": "delete", "block": name, "xy": found["xy"], "found": found}
    # Nested in a shared underlay: cover in this view's modelspace only.
    bb = _world_bbox(doc, name, found["matrix"])
    if not bb:
        return {"ok": False, "reason": f"无法计算「{name}」的覆盖范围", "block": name, "xy": xy}
    cover = _add_cover(msp, bb, layer)
    if not cover.get("ok"):
        return cover
    cover["block"] = name
    cover["xy"] = found["xy"]
    cover["found"] = found
    return cover


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
    """Mutate an in-memory DXF document. Does not save, snapshot, or write history."""
    msp = doc.modelspace()
    layer = _ensure_layer(doc, layer)
    results: list[dict[str, Any]] = []
    if issues is None:
        issues = placement_issues(doc, plan, bbox=bbox, region=region, index=index)
    codes = _issue_codes(issues)

    for idx, item in enumerate(plan.get("deletes") or []):
        result = _hide_or_delete(doc, msp, item, bbox, layer) if isinstance(item, dict) else {
            "ok": False, "reason": "删除项格式不对", "item": item
        }
        results.append(_with_issue_code(result, codes, "delete", idx))
    for idx, item in enumerate(plan.get("replaces") or []):
        if not isinstance(item, dict):
            results.append({"ok": False, "reason": "替换项格式不对", "item": item})
            continue
        d = _hide_or_delete(
            doc,
            msp,
            {
                "block": item.get("from_block") or item.get("block"),
                "xy": item.get("from_xy") or item.get("xy") or item.get("from"),
            },
            bbox,
            layer,
        )
        results.append(_with_issue_code(d, codes, "replace", idx))
        if d.get("ok"):
            ins = dict(item)
            ins.setdefault("xy", item.get("to") or item.get("xy") or (d.get("xy")))
            results.append(
                _add_insert(
                    doc, msp, ins, layer, bbox, found=d.get("found"),
                    region=region, index=index, snap_kind="replace",
                )
            )
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
        found = find_symbol(doc, msp, name, src, bbox)
        if not found or found["kind"] != "direct":
            results.append(_with_issue_code(
                {"ok": False, "reason": f"没有找到要移动的「{name}」", "block": name, "from": src},
                codes, "move", idx,
            ))
            continue
        if not _in_region(dst, bbox):
            results.append(_with_issue_code(
                {"ok": False, "reason": "目标坐标不在当前分区内", "block": name, "to": dst},
                codes, "move", idx,
            ))
            continue
        ent = found["entity"]
        try:
            ent.dxf.insert = (dst[0], dst[1], float(getattr(ent.dxf.insert, "z", 0) or 0))
            if item.get("rot") is not None:
                ent.dxf.rotation = float(item["rot"])
        except Exception as exc:
            results.append(_with_issue_code(
                {"ok": False, "reason": f"移动失败：{exc}", "block": name}, codes, "move", idx
            ))
            continue
        results.append({"ok": True, "action": "move", "block": name, "from": src, "to": dst})
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
    dest = ensure_work_copy(proj)
    official = official_dxf(proj)
    hist = load_history(proj)
    next_rev = (hist["entries"][-1]["rev"] + 1) if hist["entries"] else 1
    _ensure_original_snapshot(proj)
    snap = _snapshot_path(proj, next_rev)
    _write_snapshot(dest, snap)

    encoding = proj.get("encoding") or "utf-8"
    doc = _read_doc(dest, encoding)
    layer = _ensure_layer(doc, f"{NEW_LAYER_PREFIX}-{next_rev}")
    issues = placement_issues(doc, plan, bbox=bbox, region=region)
    results = apply_mutations(doc, plan, bbox=bbox, layer=layer, region=region, issues=issues)
    snapped_count = sum(1 for r in results if r.get("snapped"))

    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    if ok_n == 0:
        if snap.is_file():
            snap.unlink()
        reasons = [r.get("reason") for r in results if r.get("reason")]
        raise ValueError(reasons[0] if reasons else "方案里没有能写进工作图的改动。")

    old_base = None
    if bbox:
        try:
            from . import cad_render
            old_base = cad_render.base_cache_info(dest, list(bbox))
        except Exception:
            old_base = None

    _save_doc(doc, dest)
    _write_session_source(proj, dest)

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
            if xy and len(xy) >= 2:
                x, y = float(xy[0]), float(xy[1])
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
        if not xy or len(xy) < 2:
            return
        x, y = float(xy[0]), float(xy[1])
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
    # 预览与正式写入走同一套吸附，所见即所得。
    results = apply_mutations(doc, plan, bbox=bbox, layer=layer, region=region)
    ok_n = sum(1 for r in results if r.get("ok"))
    if ok_n == 0:
        reasons = [r.get("reason") for r in results if r.get("reason")]
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
        bits = []
        if covers:
            bits.append(f"覆盖{covers}处")
        if inserts:
            bits.append(f"新增{inserts}处")
        if deletes:
            bits.append(f"删除{deletes}处")
        if bits:
            line += "，" + "、".join(bits)
        lines.append(line)
    text = "\n".join(lines)
    return text[:max_chars]
