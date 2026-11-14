"""Drawing index: flatten nested inserts, label symbols, attach them to rooms.

Index quality is the ceiling of the workbench. The official block name is never
invented; a human-readable display_name is added only when there is evidence
(name tokens, the default library, the drawing legend, a known typology, or a
conservative geometry profile). Nested fixtures inside a shared underlay are
recorded with host_block and edit_hint=overlay.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import ezdxf
from ezdxf import bbox as ezbbox
from ezdxf.math import Vec3

SCHEMA = "cad-nl-drawing-index/v1.1"

# Recurring typologies (default library / Tianzheng fixture blocks).
# Official block names stay as-is; these are display labels only.
KNOWN_TYPOLOGIES: dict[str, dict[str, Any]] = {
    "做成ACAD TuKuai0155": {
        "display_name": "蹲便器",
        "aliases": ["蹲便", "大便器"],
        "category": "洁具",
        "source": "typology",
    },
    "A$C17825FF6": {
        "display_name": "小便器",
        "aliases": ["小便斗"],
        "category": "洁具",
        "source": "typology",
    },
    "A$C5D954283": {
        "display_name": "台盆",
        "aliases": ["方形台盆", "洗手盆"],
        "category": "洁具",
        "source": "typology",
    },
    "A$C2A8643E5": {
        "display_name": "坐便器",
        "aliases": ["坐便", "马桶"],
        "category": "洁具",
        "source": "typology",
    },
    "M-WZAHW-1": {
        "display_name": "淋浴器",
        "aliases": ["淋浴"],
        "category": "洁具",
        "source": "typology",
    },
    "地漏": {"display_name": "地漏", "aliases": [], "category": "地漏", "source": "block_name"},
    "马桶A": {"display_name": "马桶", "aliases": ["坐便器", "坐便"], "category": "洁具", "source": "block_name"},
    # Student-cafeteria dining set (table + chairs) on P-FURN. Official name unchanged.
    "A$C783832BF": {
        "display_name": "餐桌",
        "aliases": ["四人桌", "四人桌组", "桌椅", "餐椅", "桌子"],
        "category": "家具",
        "source": "typology",
    },
}

NAME_TOKEN_LABELS: list[tuple[re.Pattern[str], str, str, list[str]]] = [
    (re.compile(r"蹲便"), "蹲便器", "洁具", ["蹲便", "大便器"]),
    (re.compile(r"坐便|座便"), "坐便器", "洁具", ["坐便", "座便", "马桶"]),
    (re.compile(r"马桶"), "马桶", "洁具", ["坐便器", "坐便"]),
    (re.compile(r"小便"), "小便器", "洁具", ["小便斗"]),
    (re.compile(r"地漏"), "地漏", "地漏", []),
    (re.compile(r"洗手盆|洗脸盆|台盆"), "洗手盆", "洁具", ["台盆", "洗脸盆"]),
    (re.compile(r"淋浴"), "淋浴器", "洁具", ["淋浴"]),
    (re.compile(r"消火栓"), "消火栓", "消防", []),
    (re.compile(r"清扫口"), "清扫口", "管件", []),
    (re.compile(r"拖布池"), "拖布池", "洁具", []),
    (re.compile(r"阀门|VALVE"), "阀门", "阀门", []),
    (re.compile(r"餐桌"), "餐桌", "家具", ["桌子", "桌椅", "餐椅"]),
    (re.compile(r"餐椅"), "餐椅", "家具", ["椅子"]),
    (re.compile(r"会议桌"), "会议桌", "家具", ["桌子"]),
]

FIXTURE_LAYER = re.compile(
    r"FIXT|卫浴|洁具|EQUIP|SB_ZF|FURN|WP_F_PJ|VALVE|0标注|给排水|设备|家具|厨房|RAWEN",
    re.I,
)
PIPE_LAYER = re.compile(r"EQUIP_|PIPE|管|TWTSYS|TwtSys|ATTACHMENT", re.I)
WINDOW_LAYER = re.compile(r"WINDOW|WINDOW|门窗", re.I)
UNDERLAY_HINT = re.compile(r"^A\$C", re.I)
SKIP_BLOCK = re.compile(r"^\*")

# Equipment that should be attached to rooms (not every sprinkler or pipe tee).
ROOM_EQUIPMENT_CATEGORIES = {
    "洁具",
    "地漏",
    "消防",
    "阀门",
    "清扫口",
    "设备",
    "家具",
    "厨房",
}

ProgressFn = Optional[Callable[[int, int, str], None]]


def _safe_name(name: str) -> str:
    if name is None:
        return ""
    try:
        name.encode("utf-8")
        return name
    except UnicodeEncodeError:
        try:
            return name.encode("latin-1", "surrogateescape").decode("gbk")
        except Exception:
            return name.encode("utf-8", "replace").decode("utf-8")


def _load_library_map() -> dict[str, dict[str, Any]]:
    try:
        from . import library

        blocks = library.get_index().get("blocks") or []
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for b in blocks:
        name = b.get("name")
        if name:
            out[name] = b
    return out


def _matrix_pose(matrix) -> tuple[list[float], float, list[float]]:
    origin = matrix.transform(Vec3(0, 0, 0))
    x_axis = matrix.transform_direction(Vec3(1, 0, 0))
    y_axis = matrix.transform_direction(Vec3(0, 1, 0))
    rot = math.degrees(math.atan2(x_axis.y, x_axis.x))
    scale = [round(x_axis.magnitude, 4), round(y_axis.magnitude, 4)]
    return [round(float(origin.x), 3), round(float(origin.y), 3)], round(rot, 3), scale


def _block_profile(doc, name: str) -> dict[str, Any]:
    info: dict[str, Any] = {"size_mm": None, "types": {}, "has_ellipse": False, "has_hatch": False, "entity_count": 0}
    try:
        blk = doc.blocks.get(name)
    except Exception:
        return info
    if blk is None:
        return info
    types: Counter[str] = Counter()
    n = 0
    for e in blk:
        typ = e.dxftype()
        types[typ] += 1
        n += 1
    info["types"] = dict(types)
    info["entity_count"] = n
    info["has_ellipse"] = types.get("ELLIPSE", 0) > 0
    info["has_hatch"] = types.get("HATCH", 0) > 0
    try:
        ext = ezbbox.extents(blk, fast=True)
        if ext:
            info["size_mm"] = [round(float(ext.size.x), 1), round(float(ext.size.y), 1)]
            info["local_bbox"] = {
                "min": [round(float(ext.extmin.x), 2), round(float(ext.extmin.y), 2)],
                "max": [round(float(ext.extmax.x), 2), round(float(ext.extmax.y), 2)],
            }
    except Exception:
        pass
    return info


def collect_inserts(doc, layout, on_progress: Optional[Callable[[int, int], None]] = None) -> list[dict[str, Any]]:
    """Flatten INSERT entities to world coordinates, including nested ones."""
    out: list[dict[str, Any]] = []
    host_count: Counter[str] = Counter()
    # First pass: how many times each top-level block is inserted.
    try:
        for e in layout.query("INSERT"):
            host_count[_safe_name(e.dxf.name)] += 1
    except Exception:
        pass

    def walk(container, matrix, chain: list[dict[str, Any]], depth: int) -> None:
        try:
            found = list(container.query("INSERT"))
        except Exception:
            return
        for e in found:
            try:
                name = _safe_name(e.dxf.name)
                if not name or SKIP_BLOCK.match(name):
                    continue
                local = e.matrix44()
                world = local if matrix is None else local @ matrix
                xy, rot, scale = _matrix_pose(world)
                layer = _safe_name(getattr(e.dxf, "layer", "") or "")
                rec: dict[str, Any] = {
                    "name": name,
                    "block": name,
                    "xy": xy,
                    "rot": rot,
                    "scale": scale,
                    "layer": layer,
                    "nested": depth > 0,
                    "depth": depth,
                }
                if chain:
                    host = chain[0]
                    rec["host_block"] = host["block"]
                    rec["host_insert_xy"] = host["xy"]
                    rec["host_insert_count"] = int(host_count.get(host["block"] or "", 0))
                    rec["shared_host"] = rec["host_insert_count"] > 1
                    rec["edit_hint"] = "overlay" if rec["shared_host"] else "host"
                    # local point in the top-level host
                    try:
                        host_m = None
                        # reconstruct by walking: local in host = inverse(host_world) * world
                    except Exception:
                        pass
                    hx, hy = host["xy"]
                    rec["local_xy"] = [round(xy[0] - hx, 3), round(xy[1] - hy, 3)]
                else:
                    rec["host_block"] = None
                    rec["host_insert_xy"] = None
                    rec["host_insert_count"] = int(host_count.get(name, 0))
                    rec["shared_host"] = False
                    rec["edit_hint"] = "direct"
                    rec["local_xy"] = [0.0, 0.0]
                out.append(rec)
                try:
                    blk = doc.blocks.get(e.dxf.name)
                except Exception:
                    blk = None
                if blk is not None and depth < 8:
                    walk(blk, world, chain + [rec], depth + 1)
            except Exception:
                continue

    walk(layout, None, [], 0)
    if on_progress:
        on_progress(len(out), max(len(out), 1))
    return out


def _clean_dxf_text(raw: Any) -> str:
    """Keep visible wording; drop MTEXT format codes without swallowing the text."""
    text = str(raw or "")
    if not text.strip():
        return ""
    # {\\C50; 餐厅} -> 餐厅; font-wrapped equipment labels keep their wording.
    text = re.sub(r"\{\\([^;]*);([^{}]*)\}", r"\2", text)
    text = re.sub(r"\\[A-Za-z][^;]*;", "", text)
    text = text.replace("\\P", " ").replace("\\p", " ").replace("\\~", " ")
    text = re.sub(r"[{}]", "", text)
    # Leftover font preamble if braces were already stripped: SimHei|b0|i0|c134|p49;汉字
    text = re.sub(r"^[A-Za-z][^;\n]*\|[^;\n]*;", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def collect_texts(doc, layout, on_progress: Optional[Callable[[int, int], None]] = None) -> list[dict[str, Any]]:
    """Collect TEXT/MTEXT in modelspace and inside inserted blocks, in world coordinates."""
    out: list[dict[str, Any]] = []

    def take_text(e, matrix, source: str, host: Optional[dict[str, Any]]) -> None:
        typ = e.dxftype()
        if typ not in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            return
        try:
            if typ == "MTEXT":
                try:
                    raw = e.plain_text() or ""
                except Exception:
                    raw = e.text or ""
                if not str(raw).strip():
                    raw = e.text or ""
            else:
                raw = e.dxf.text
            text = _clean_dxf_text(raw)
            if not text:
                return
            loc = e.dxf.insert
            pt = Vec3(float(loc.x), float(loc.y), 0)
            if matrix is not None:
                pt = matrix.transform(pt)
            xy = [round(float(pt.x), 3), round(float(pt.y), 3)]
            rec = {
                "text": _safe_name(text),
                "xy": xy,
                "layer": _safe_name(getattr(e.dxf, "layer", "") or ""),
                "height": float(getattr(e.dxf, "height", 0) or getattr(e.dxf, "char_height", 0) or 0),
                "source": source,
            }
            if host:
                rec["host_block"] = host.get("block")
                rec["host_insert_xy"] = host.get("xy")
                rec["local_xy"] = [round(float(loc.x), 3), round(float(loc.y), 3)]
            out.append(rec)
        except Exception:
            return

    def walk(container, matrix, host: Optional[dict[str, Any]], depth: int) -> None:
        try:
            entities = list(container)
        except Exception:
            return
        for e in entities:
            typ = e.dxftype()
            if typ in {"TEXT", "MTEXT", "ATTRIB"}:
                take_text(e, matrix, "block_text" if host else "modelspace_text", host)
            elif typ == "ATTDEF":
                # Prefer the INSERT's ATTRIB value; keep a loose ATTDEF only
                # when it is sitting in the layout itself (not inside a block walk).
                if host is None:
                    take_text(e, matrix, "modelspace_text", host)
            elif typ == "INSERT":
                try:
                    name = _safe_name(e.dxf.name)
                    if not name:
                        continue
                    # Equipment collection skips *anonymous* blocks; text still
                    # walks them so dimension / nested labels are not dropped.
                    local = e.matrix44()
                    world = local if matrix is None else local @ matrix
                    xy, rot, scale = _matrix_pose(world)
                    rec = {"block": name, "xy": xy, "rot": rot, "scale": scale}
                    try:
                        attribs = list(e.attribs)
                    except Exception:
                        attribs = []
                    for attrib in attribs:
                        take_text(attrib, world, "insert_attrib", host or rec)
                    blk = doc.blocks.get(e.dxf.name)
                    if blk is not None and depth < 8:
                        walk(blk, world, host or rec, depth + 1)
                except Exception:
                    continue

    walk(layout, None, None, 0)
    if on_progress:
        on_progress(len(out), max(len(out), 1))
    return out


def classify_symbol(rec: dict[str, Any], profile: dict[str, Any], library_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Add display_name / category / aliases without changing the official block name."""
    name = rec.get("block") or rec.get("name") or ""
    layer = rec.get("layer") or ""
    display = None
    aliases: list[str] = []
    category = None
    source = None

    typo = KNOWN_TYPOLOGIES.get(name)
    if typo:
        display = typo["display_name"]
        aliases = list(typo.get("aliases") or [])
        category = typo.get("category")
        source = typo.get("source") or "typology"

    if not display:
        for pat, label, cat, ali in NAME_TOKEN_LABELS:
            if pat.search(name):
                display = label
                aliases = ali
                category = cat
                source = "block_name"
                break

    lib = library_map.get(name)
    if lib:
        rec["library"] = lib.get("library")
        rec["library_category"] = lib.get("category")
        lib_disp = lib.get("display_name") or ""
        if not category and lib.get("category"):
            category = lib.get("category")
        if not display and lib_disp and lib_disp != name and len(lib_disp) <= 16:
            # Nearby-label library names like 蹲便器(2)
            cleaned = re.sub(r"[\(（]\d+[\)）]$", "", lib_disp).strip()
            if cleaned and cleaned != name:
                display = cleaned
                source = source or "library"
                if not category:
                    category = lib.get("category") or "洁具"

    if not display and re.search(r"[\u4e00-\u9fff]", name):
        # Official name is already readable; do not invent a different Chinese name.
        display = name
        source = "block_name"

    if not display:
        display, category, source = _geometry_guess(rec, profile, category)

    if not category:
        if re.search(r"地漏", name) or (display == "地漏"):
            category = "地漏"
        elif FIXTURE_LAYER.search(layer) and not PIPE_LAYER.search(name):
            if "VALVE" in layer.upper() or "阀" in name:
                category = "阀门"
            elif "WP_F_PJ" in layer or "消火栓" in name:
                category = "消防"
            elif WINDOW_LAYER.search(layer):
                category = "门窗"
            elif layer in {"0标注"} and name != "地漏":
                category = None
            elif re.search(r"设备|厨房|RAWEN", layer, re.I):
                category = category or "设备"
            elif re.search(r"FURN|家具", layer, re.I):
                category = category or "家具"
            else:
                category = category or "洁具"
        elif PIPE_LAYER.search(name) or PIPE_LAYER.search(layer):
            category = "管件"

    if display == name:
        # Keep official name; only treat as a label when it is actually readable.
        if not re.search(r"[\u4e00-\u9fff]", display or ""):
            display = None

    rec["display_name"] = display
    rec["aliases"] = aliases
    rec["category"] = category
    rec["label_source"] = source
    rec["size_mm"] = (profile or {}).get("size_mm")
    if rec.get("shared_host") and rec.get("nested") and category in ROOM_EQUIPMENT_CATEGORIES:
        rec["edit_hint"] = "overlay"
    return rec


def _geometry_guess(
    rec: dict[str, Any], profile: dict[str, Any], category: Optional[str]
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Conservative squat / seated / urinal / basin profiles. Returns (display, category, source)."""
    size = (profile or {}).get("size_mm") or rec.get("size_mm")
    if not size or min(size) <= 0:
        return None, category, None
    w, h = sorted((abs(size[0]), abs(size[1])))  # short, long
    types = (profile or {}).get("types") or {}
    arcs = int(types.get("ARC", 0)) + int(types.get("CIRCLE", 0))
    ellipses = int(types.get("ELLIPSE", 0))
    layer = rec.get("layer") or ""
    # Geometry profiles are only for sanitary fixtures, not kitchen or furniture blocks
    # that happen to have a similar bounding box.
    geom_layer = re.compile(r"FIXT|卫浴|洁具", re.I)
    lib_cat = rec.get("library_category") or ""
    if not geom_layer.search(layer) and lib_cat not in {"洁具", "卫生洁具"}:
        return None, category, None
    # Seated WC: tall bowl+tank, many arcs, ~350-500 × 600-800
    if 300 <= w <= 550 and 580 <= h <= 850 and arcs >= 8 and ellipses <= 1:
        return "坐便器", "洁具", "geometry"
    # Squat pan: ~350-550 × 350-550, ellipses or few arcs
    if 330 <= w <= 550 and 330 <= h <= 560 and (ellipses >= 2 or (ellipses >= 1 and arcs <= 4)):
        return "蹲便器", "洁具", "geometry"
    # Urinal: mid size, many arcs, not as tall as a seated WC
    if 300 <= w <= 450 and 300 <= h <= 500 and arcs >= 10 and ellipses <= 1:
        return "小便器", "洁具", "geometry"
    return None, category, None


def _sheet_for_point(xy: list[float], sheets: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    x, y = xy
    hits: list[tuple[float, dict[str, Any], Optional[dict[str, Any]]]] = []
    for sh in sheets:
        frame = sh.get("frame") or {}
        bb = frame.get("bbox") if isinstance(frame, dict) else frame
        if isinstance(bb, list) and len(bb) == 4 and bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]:
            area = max(bb[2] - bb[0], 0) * max(bb[3] - bb[1], 0)
            sub_hit = None
            sub_area = area
            for sub in sh.get("subviews") or []:
                sb = sub.get("frame") or sub.get("bbox")
                if isinstance(sb, dict):
                    sb = sb.get("bbox")
                if isinstance(sb, list) and len(sb) == 4 and sb[0] <= x <= sb[2] and sb[1] <= y <= sb[3]:
                    a = max(sb[2] - sb[0], 0) * max(sb[3] - sb[1], 0)
                    if sub_hit is None or a < sub_area:
                        sub_hit = sub
                        sub_area = a
            hits.append((sub_area if sub_hit else area, sh, sub_hit))
    if not hits:
        return None
    hits.sort(key=lambda t: t[0])
    sh, sub = hits[0][1], hits[0][2]
    rec = dict(sh)
    if sub:
        rec["_subview"] = sub
    return rec


def assign_locations(items: list[dict[str, Any]], sheets: list[dict[str, Any]]) -> None:
    for rec in items:
        sh = _sheet_for_point(rec.get("xy") or [0, 0], sheets)
        if not sh:
            rec["sheet"] = rec.get("sheet") or ""
            rec["floor"] = rec.get("floor") or ""
            continue
        sub = sh.get("_subview") or {}
        sub_name = sub.get("name") or ""
        title = sh.get("title") or ""
        sid = sh.get("id") or title
        if sub_name:
            head = sid
            if title and title not in sid:
                head = f"{sid} {title}".strip()
            rec["sheet"] = f"{head} / {sub_name}"
        else:
            rec["sheet"] = sid or title
        rec["sheet_id"] = sid
        rec["subview"] = sub_name
        rec["floor"] = sh.get("floor") or rec.get("floor") or ""


def assign_rooms(equipment: list[dict[str, Any]], rooms: list[dict[str, Any]], *, max_dist: float = 15000.0) -> None:
    """Attach each fixture to the nearest room on the same sheet/subview. No count cap."""
    by_sheet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for room in rooms:
        room.setdefault("equipment_nearby", [])
        by_sheet[room.get("sheet") or ""].append(room)

    for rec in equipment:
        sheet = rec.get("sheet") or ""
        candidates = by_sheet.get(sheet) or []
        # Also allow prefix match: "水施-007 / 二层卫排水详图" vs "水施-007 卫生间详图 / 二层卫排水详图"
        if not candidates:
            sub = rec.get("subview") or ""
            for key, group in by_sheet.items():
                if sub and sub in key:
                    candidates.extend(group)
                elif rec.get("sheet_id") and rec["sheet_id"] in key:
                    candidates.extend(group)
        best = None
        best_d = max_dist
        xy = rec.get("xy") or [0, 0]
        for room in candidates:
            # Prefer same host block when both are nested in it.
            rx, ry = (room.get("world_xy") or [0, 0])[:2]
            d = math.hypot(xy[0] - rx, xy[1] - ry)
            if rec.get("host_block") and room.get("host_block") and rec["host_block"] != room["host_block"]:
                d += 4000  # soft penalty, still allowed
            if d < best_d:
                best_d = d
                best = room
        # Accessible WC / shower often sit between room labels; prefer 无障碍卫生间
        # when the fixture type matches and that room is on the same host/sheet.
        label = rec.get("display_name") or ""
        if label in {"坐便器", "淋浴器"}:
            for room in candidates:
                name = room.get("name") or ""
                if "无障碍" not in name:
                    continue
                if rec.get("host_block") and room.get("host_block") and rec["host_block"] != room["host_block"]:
                    continue
                rx, ry = (room.get("world_xy") or [0, 0])[:2]
                d = math.hypot(xy[0] - rx, xy[1] - ry)
                if d <= max(max_dist, 1) and d <= max(best_d * 2.0, 5000):
                    best, best_d = room, d
                    break
        if best is None:
            rec["room"] = None
            rec["dist"] = None
            continue
        rec["room"] = best.get("name")
        rec["dist"] = round(best_d, 1)
        item = {k: rec[k] for k in rec}
        best.setdefault("equipment_nearby", []).append(item)

    for room in rooms:
        items = room.get("equipment_nearby") or []
        items.sort(key=lambda it: (it.get("dist") is None, it.get("dist") or 0))
        room["equipment_nearby"] = items



def is_attachable(rec: dict[str, Any]) -> bool:
    """Room nearby lists keep fixtures and kitchen equipment, not every nearby dining table."""
    cat = rec.get("category")
    if cat in {"管件", "门窗", "喷头", "家具"}:
        return False
    if cat in (ROOM_EQUIPMENT_CATEGORIES - {"家具"}):
        return True
    if rec.get("display_name") and cat != "家具":
        return True
    name = rec.get("block") or rec.get("name") or ""
    if name in KNOWN_TYPOLOGIES:
        return True
    layer = rec.get("layer") or ""
    if re.search(r"FURN|家具", layer, re.I):
        return False
    if re.search(r"设备|厨房|RAWEN|FIXT|卫浴|洁具", layer, re.I):
        return True
    if re.search(r"[\u4e00-\u9fff]", name):
        return True
    return False


def is_room_equipment(rec: dict[str, Any]) -> bool:
    cat = rec.get("category")
    if cat in {"管件", "门窗", "喷头"}:
        return False
    if cat in ROOM_EQUIPMENT_CATEGORIES:
        return True
    if rec.get("display_name"):
        return True
    name = rec.get("block") or rec.get("name") or ""
    if name in KNOWN_TYPOLOGIES:
        return True
    layer = rec.get("layer") or ""
    if re.search(r"设备|家具|厨房|RAWEN|FURN|FIXT|卫浴|洁具", layer, re.I):
        return True
    if re.search(r"[\u4e00-\u9fff]", name):
        return True
    return False



DINING_ROOM_RE = re.compile(r"餐厅|食堂|就餐|用餐|餐饮")
_FURNITURE_QUERY_RE = re.compile(r"餐桌|餐椅|桌椅|家具|桌子|椅子|四人桌|密度")
_QUERY_STAPLES = (
    "餐桌",
    "餐椅",
    "桌子",
    "椅子",
    "桌椅",
    "家具",
    "四人桌",
    "四人桌组",
    "密度",
    "餐厅",
    "食堂",
    "蹲便器",
    "蹲便",
    "坐便器",
    "坐便",
    "座便",
    "马桶",
    "小便器",
    "小便斗",
    "洗手盆",
    "洗脸盆",
    "台盆",
    "淋浴器",
    "淋浴",
    "地漏",
    "消火栓",
    "清扫口",
    "洁具",
    "便器",
    "小便",
)


def _vocab_terms() -> list[str]:
    terms = set(_QUERY_STAPLES)
    for typo in KNOWN_TYPOLOGIES.values():
        if typo.get("display_name"):
            terms.add(str(typo["display_name"]))
        for alias in typo.get("aliases") or []:
            if alias:
                terms.add(str(alias))
    return sorted(terms, key=len, reverse=True)


def is_furniture(rec: dict[str, Any]) -> bool:
    if (rec.get("category") or "") == "家具":
        return True
    layer = rec.get("layer") or rec.get("typical_layer") or ""
    if re.search(r"FURN|家具", layer, re.I):
        return True
    label = rec.get("display_name") or ""
    if re.search(r"桌|椅|家具", label):
        return True
    return False


def is_furniture_query(text: str) -> bool:
    t = text or ""
    if _FURNITURE_QUERY_RE.search(t) or "桌" in t or "椅" in t:
        return True
    return False


def is_dining_room_name(name: Optional[str]) -> bool:
    return bool(DINING_ROOM_RE.search(name or ""))


def apply_known_labels(rec: dict[str, Any]) -> bool:
    """Add typology display_name / aliases. Official block name is never changed."""
    name = rec.get("block") or rec.get("name") or ""
    typo = KNOWN_TYPOLOGIES.get(name)
    if not typo:
        return False
    changed = False
    if not rec.get("display_name"):
        rec["display_name"] = typo["display_name"]
        changed = True
    aliases = list(rec.get("aliases") or [])
    for alias in typo.get("aliases") or []:
        if alias and alias not in aliases:
            aliases.append(alias)
            changed = True
    if aliases != list(rec.get("aliases") or []):
        rec["aliases"] = aliases
    if not rec.get("category") and typo.get("category"):
        rec["category"] = typo["category"]
        changed = True
    if not rec.get("label_source"):
        rec["label_source"] = typo.get("source") or "typology"
        changed = True
    return changed


def _dining_zone_contains(xy: list[float], dining: dict[str, Any], others: list[dict[str, Any]], *, margin: float = 1000.0, max_dist: float = 40000.0) -> bool:
    """True when a furniture insert sits in the dining hall, not the kitchen band.

    Rooms have name labels, not polygons. School-cafeteria plans put 餐厅 on one
    side and service rooms on the other; a gap along X or Y is used as the edge.
    """
    try:
        px, py = float(xy[0]), float(xy[1])
        dx, dy = (float(v) for v in (dining.get("world_xy") or [0, 0])[:2])
    except (TypeError, ValueError, IndexError):
        return False
    if math.hypot(px - dx, py - dy) > max_dist:
        return False
    if not others:
        return True
    oxs: list[float] = []
    oys: list[float] = []
    for room in others:
        try:
            oxs.append(float(room["world_xy"][0]))
            oys.append(float(room["world_xy"][1]))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    if not oxs:
        return True
    if min(oxs) > dx and (min(oxs) - dx) > margin:
        return px < (min(oxs) - margin)
    if max(oxs) < dx and (dx - max(oxs)) > margin:
        return px > (max(oxs) + margin)
    if min(oys) > dy and (min(oys) - dy) > margin:
        return py < (min(oys) - margin)
    if max(oys) < dy and (dy - max(oys)) > margin:
        return py > (max(oys) + margin)
    # No clear hall-vs-service gap: do not guess.
    return False


def assign_dining_furniture(equipment: list[dict[str, Any]], rooms: list[dict[str, Any]]) -> None:
    """Attach dining-hall furniture to 餐厅 etc. Never dump it into 卫生间 or 工具间."""
    by_sheet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for room in rooms:
        room.setdefault("equipment_nearby", [])
        by_sheet[room.get("sheet") or ""].append(room)

    def _sheet_rooms(sheet: str) -> list[dict[str, Any]]:
        hits = list(by_sheet.get(sheet) or [])
        if hits:
            return hits
        sid = sheet
        extra: list[dict[str, Any]] = []
        for key, group in by_sheet.items():
            if sid and sid in key:
                extra.extend(group)
        return extra

    attached_keys: set[tuple[Any, ...]] = set()
    for rec in equipment:
        if not is_furniture(rec):
            continue
        if is_dining_room_name(rec.get("room")):
            continue
        sheet = rec.get("sheet") or ""
        candidates = _sheet_rooms(sheet)
        dining = [r for r in candidates if is_dining_room_name(r.get("name"))]
        others = [r for r in candidates if not is_dining_room_name(r.get("name"))]
        if not dining:
            continue
        xy = rec.get("xy") or [0, 0]
        best = None
        best_d = None
        for room in dining:
            try:
                rx, ry = (room.get("world_xy") or [0, 0])[:2]
                d = math.hypot(float(xy[0]) - float(rx), float(xy[1]) - float(ry))
            except (TypeError, ValueError, IndexError):
                continue
            if best is None or d < best_d:
                best, best_d = room, d
        if best is None or not _dining_zone_contains(xy, best, others):
            continue
        rec["room"] = best.get("name")
        rec["dist"] = round(best_d, 1)
        item = {k: rec[k] for k in rec}
        key = (item.get("block") or item.get("name"), tuple(item.get("xy") or []), item.get("sheet"))
        existing_keys = {
            (it.get("block") or it.get("name"), tuple(it.get("xy") or []), it.get("sheet"))
            for it in (best.get("equipment_nearby") or [])
        }
        if key not in existing_keys:
            best.setdefault("equipment_nearby", []).append(item)
        attached_keys.add(key)

    for room in rooms:
        items = room.get("equipment_nearby") or []
        items.sort(key=lambda it: (it.get("dist") is None, it.get("dist") or 0))
        room["equipment_nearby"] = items


def _equipment_key(rec: dict[str, Any]) -> tuple[Any, ...]:
    return (rec.get("block") or rec.get("name"), tuple(rec.get("xy") or []), rec.get("sheet"))


def refresh_unassigned(index: dict[str, Any]) -> None:
    rooms = index.get("rooms") or []
    equipment = index.get("equipment") or []
    assigned = {_equipment_key(it) for room in rooms for it in (room.get("equipment_nearby") or [])}
    index["equipment_unassigned"] = [it for it in equipment if _equipment_key(it) not in assigned]


def relabel_index(index: dict[str, Any]) -> dict[str, Any]:
    """Apply typologies and dining-furniture attachment to an already-built index.

    Official block names stay as they are. Safe to call on a loaded copy; idempotent.
    """
    if not index or not isinstance(index, dict):
        return index
    for rec in index.get("equipment") or []:
        apply_known_labels(rec)
    for rec in index.get("equipment_unassigned") or []:
        apply_known_labels(rec)
    for room in index.get("rooms") or []:
        for rec in room.get("equipment_nearby") or []:
            apply_known_labels(rec)
    for block in index.get("blocks") or []:
        apply_known_labels(block)
    assign_dining_furniture(index.get("equipment") or [], index.get("rooms") or [])
    refresh_unassigned(index)
    return index


def furniture_on_sheet(
    index: dict[str, Any],
    region: Optional[dict[str, Any]] = None,
    *,
    prefer_dining: bool = True,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Furniture inserts on the current sheet, dining-hall first, spatially sorted."""
    region = region or {}
    sid = region.get("sheet_id") or ""
    title = region.get("title") or ""
    floor = region.get("floor") or ""
    items = [rec for rec in (index.get("equipment") or []) if is_furniture(rec)]
    here = []
    for rec in items:
        sheet = rec.get("sheet") or ""
        rec_floor = rec.get("floor") or ""
        on_sheet = bool((sid and sid in sheet) or (title and title in sheet))
        on_floor = bool(floor and rec_floor and rec_floor == floor)
        if on_sheet or (not sid and not title and on_floor):
            here.append(rec)
    if not here and (sid or title):
        here = list(items)
    dining, rest = [], []
    for rec in here:
        if prefer_dining and is_dining_room_name(rec.get("room")):
            dining.append(rec)
        else:
            rest.append(rec)
    picked = dining or here

    def _xy_key(rec: dict[str, Any]) -> tuple[float, float]:
        xy = rec.get("xy") or [0, 0]
        try:
            return (round(float(xy[1]), 3), round(float(xy[0]), 3))
        except (TypeError, ValueError, IndexError):
            return (0.0, 0.0)

    picked.sort(key=_xy_key)
    # Unique by insert
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for rec in picked:
        key = _equipment_key(rec)
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def _fmt_mm(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(v) >= 100:
        return f"{v:.1f}"
    return f"{round(v, 3):g}"


def format_furniture_inventory(
    items: list[dict[str, Any]],
    *,
    total: Optional[int] = None,
    y_tol: float = 80.0,
) -> list[str]:
    """Compact, row-grouped furniture list with official names and all coordinates."""
    if not items:
        return []
    total = int(total) if total is not None else len(items)
    counts: Counter[str] = Counter()
    for rec in items:
        name = rec.get("block") or rec.get("name") or "设备"
        label = rec.get("display_name")
        shown = f"{label}（块名 `{name}`）" if label and label != name else f"`{name}`"
        counts[shown] += 1
    summary = "、".join(f"{n}×{c}" for n, c in counts.most_common(8))
    extra = (
        f"，本图幅共 {total} 套，下列为其中 {len(items)} 套"
        if total > len(items)
        else f"，本图幅 {total} 套"
    )
    lines = [f"本图幅相关家具{extra}（只列当前图幅；世界坐标 mm）：{summary}"]

    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for rec in items:
        key = (rec.get("block") or rec.get("name"), rec.get("display_name") or "", rec.get("layer") or "")
        groups[key].append(rec)

    for (block, display, layer), recs in groups.items():
        def _xy(rec: dict[str, Any]) -> tuple[float, float]:
            xy = rec.get("xy") or [0, 0]
            try:
                return (float(xy[1]), float(xy[0]))
            except (TypeError, ValueError, IndexError):
                return (0.0, 0.0)

        recs = sorted(recs, key=_xy)
        clusters: list[dict[str, Any]] = []
        for rec in recs:
            try:
                x = float((rec.get("xy") or [0, 0])[0])
                y = float((rec.get("xy") or [0, 0])[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not clusters or abs(y - clusters[-1]["y"]) > y_tol:
                clusters.append({"y": y, "xs": [x], "n": 1})
            else:
                c = clusters[-1]
                c["y"] = (c["y"] * c["n"] + y) / (c["n"] + 1)
                c["xs"].append(x)
                c["n"] += 1
        shown = f"{display}（块名 `{block}`）" if display and display != block else f"`{block}`"
        layer_bit = f"  图层={layer}" if layer else ""
        for c in clusters:
            xs = ", ".join(_fmt_mm(x) for x in c["xs"])
            lines.append(f"  {shown}  y={_fmt_mm(c['y'])}  {c['n']}套  x={xs}{layer_bit}")
    return lines



_TITLE_BLOCK_LAYER = re.compile(r"(BORDER|图框|图签|TITLE|FRAME|C-Sheet)", re.I)


def locate_texts(texts: list[dict[str, Any]], sheets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy sheet / floor onto each text record. Official wording is unchanged."""
    assign_locations(texts, sheets)
    return texts


def attach_labels_nearby(
    rooms: list[dict[str, Any]],
    texts: list[dict[str, Any]],
    *,
    max_dist: float = 12000.0,
    per_room: int = 24,
) -> None:
    """Hang nearby annotations on rooms. Does not invent names or drop existing labels."""
    if not rooms or not texts:
        return
    by_sheet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for room in rooms:
        room.setdefault("labels_nearby", [])
        room["_labels_preexisting"] = list(room.get("labels_nearby") or [])
        by_sheet[room.get("sheet") or ""].append(room)

    def _sheet_rooms(sheet: str) -> list[dict[str, Any]]:
        hits = list(by_sheet.get(sheet) or [])
        if hits:
            return hits
        extra: list[dict[str, Any]] = []
        for key, group in by_sheet.items():
            if sheet and sheet in key:
                extra.extend(group)
        return extra

    for rec in texts:
        raw = str(rec.get("text") or "").strip()
        if not raw:
            continue
        candidates = _sheet_rooms(rec.get("sheet") or "")
        if not candidates:
            continue
        xy = rec.get("xy") or [0, 0]
        try:
            x, y = float(xy[0]), float(xy[1])
        except (TypeError, ValueError, IndexError):
            continue
        best = None
        best_d = None
        for room in candidates:
            if raw == (room.get("name") or ""):
                continue
            try:
                rx, ry = (room.get("world_xy") or [0, 0])[:2]
                d = math.hypot(x - float(rx), y - float(ry))
            except (TypeError, ValueError, IndexError):
                continue
            if d <= max_dist and (best is None or d < best_d):
                best, best_d = room, d
        if best is None:
            continue
        item = {
            "text": raw,
            "xy": rec.get("xy"),
            "layer": rec.get("layer") or "",
            "height": rec.get("height") or 0,
            "source": rec.get("source") or "",
            "dist": round(best_d, 1),
        }
        if rec.get("host_block"):
            item["host_block"] = rec.get("host_block")
        existing = best.get("labels_nearby") or []
        key = (item["text"], tuple(item.get("xy") or []))
        seen = set()
        for it in existing:
            if isinstance(it, dict):
                seen.add((str(it.get("text") or ""), tuple(it.get("xy") or [])))
            else:
                seen.add((str(it), ()))
        if key in seen:
            continue
        existing.append(item)
        best["labels_nearby"] = existing

    for room in rooms:
        original = list(room.get("labels_nearby") or [])
        # Never drop labels that were already on the room (curated notes stay).
        preexisting_n = getattr(room, "_labels_preexisting_n", None)
        preexisting = room.get("_labels_preexisting")
        items = original
        def _is_dict(it: Any) -> bool:
            return isinstance(it, dict)

        def _lab_sort(it: Any) -> tuple:
            if not isinstance(it, dict):
                return (0, 0.0, str(it))
            return (0 if it.get("dist") is None else 1, float(it.get("dist") or 0), str(it.get("text") or ""))

        kept = list(preexisting) if isinstance(preexisting, list) else None
        if kept is None:
            # First pass: items now include both old and new; preserve order of
            # already-present entries, then append new ones by distance.
            # We cannot perfectly recover "old" after in-place append, so cap
            # only the newly added tail: if longer than per_room, keep every
            # item that has no dist (curated) plus nearest others, but never
            # drop curated (no dist or note field) or shrink below original
            # unique texts recorded before this call.
            curated = [it for it in items if not isinstance(it, dict) or it.get("dist") is None or it.get("note")]
            fresh = [it for it in items if it not in curated]
            fresh.sort(key=_lab_sort)
            cap = max(per_room, len(curated))
            room["labels_nearby"] = (curated + fresh)[:cap] if len(curated) + len(fresh) > cap else curated + fresh
        else:
            fresh = [it for it in items if it not in kept]
            fresh.sort(key=_lab_sort)
            room["labels_nearby"] = kept + fresh[: max(0, per_room)]
            room.pop("_labels_preexisting", None)


def _text_has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value or ""))


def _cjk_runs(value: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]+", value or "")


def _text_query_score(raw: str, query: str, tokens: list[str]) -> int:
    """Score wording overlap even when the whole user sentence is one CJK token."""
    score = 0
    raw = raw or ""
    q = query or ""
    if not raw:
        return 0
    if raw == q or (raw in q):
        score += 24
    elif q and q in raw:
        score += 16
    raw_cjk = "".join(_cjk_runs(raw))
    q_cjk = "".join(_cjk_runs(q))
    if raw_cjk and q_cjk:
        if raw_cjk == q_cjk or raw_cjk in q_cjk:
            score += 20
        elif q_cjk in raw_cjk:
            score += 16
        else:
            maxn = min(len(q_cjk), len(raw_cjk), 12)
            best = 0
            for n in range(maxn, 1, -1):
                windows = {q_cjk[k:k + n] for k in range(0, len(q_cjk) - n + 1)}
                if any(w in raw_cjk for w in windows):
                    best = n
                    break
            if best:
                score += 4 + best
    for tok in tokens:
        if not tok:
            continue
        if tok == raw:
            score += 20
        elif tok in raw:
            score += 14
    return score


def search_texts(
    index: dict[str, Any],
    query: str,
    *,
    region: Optional[dict[str, Any]] = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    """Find stored drawing texts by wording, layer, or current sheet."""
    items = list(index.get("texts") or [])
    if not items:
        return []
    tokens = _query_tokens(query)
    region = region or {}
    sid = region.get("sheet_id") or ""
    title = region.get("title") or ""
    floor = region.get("floor") or ""
    bbox = region.get("bbox") if isinstance(region.get("bbox"), list) and len(region.get("bbox") or []) == 4 else None
    scored: list[tuple[int, dict[str, Any]]] = []
    for rec in items:
        raw = str(rec.get("text") or "")
        if not raw:
            continue
        blob = " ".join(str(rec.get(k) or "") for k in ("text", "layer", "sheet", "room"))
        score = 0
        sheet = rec.get("sheet") or ""
        if sid and sid in sheet:
            score += 4
        if title and title in sheet:
            score += 6
        if floor and rec.get("floor") == floor:
            score += 3
        if bbox:
            try:
                x, y = float((rec.get("xy") or [0, 0])[0]), float((rec.get("xy") or [0, 0])[1])
                if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]:
                    score += 6
            except (TypeError, ValueError, IndexError):
                pass
        word_score = _text_query_score(raw, query, tokens)
        score += word_score
        # Location bonuses alone must not keep every axis letter on the sheet.
        if tokens and word_score < 6:
            continue
        if tokens or (sid and sid in sheet) or (title and title in sheet) or bbox:
            scored.append((score, rec))
    scored.sort(key=lambda t: -t[0])
    if title or sid or bbox:
        here = []
        for s, r in scored:
            sheet = r.get("sheet") or ""
            on = (title and title in sheet) or (sid and sid in sheet)
            if not on and bbox:
                try:
                    x, y = float((r.get("xy") or [0, 0])[0]), float((r.get("xy") or [0, 0])[1])
                    on = bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]
                except (TypeError, ValueError, IndexError):
                    on = False
            if on:
                here.append((s, r))
        if here:
            here_ids = {id(r) for _, r in here}
            scored = here + [(s, r) for s, r in scored if id(r) not in here_ids]
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for _, rec in scored:
        key = (rec.get("text"), tuple(rec.get("xy") or []), rec.get("sheet"))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def texts_on_sheet(
    index: dict[str, Any],
    region: Optional[dict[str, Any]] = None,
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """CJK / coded annotations on the current sheet, skipping title-block chrome when possible."""
    region = region or {}
    sid = region.get("sheet_id") or ""
    title = region.get("title") or ""
    floor = region.get("floor") or ""
    bbox = region.get("bbox") if isinstance(region.get("bbox"), list) and len(region.get("bbox") or []) == 4 else None
    items = list(index.get("texts") or [])
    here: list[dict[str, Any]] = []
    for rec in items:
        raw = str(rec.get("text") or "").strip()
        if not raw:
            continue
        sheet = rec.get("sheet") or ""
        on = bool((sid and sid in sheet) or (title and title in sheet) or (floor and rec.get("floor") == floor))
        if bbox:
            try:
                x, y = float((rec.get("xy") or [0, 0])[0]), float((rec.get("xy") or [0, 0])[1])
                if bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]:
                    on = True
            except (TypeError, ValueError, IndexError):
                pass
        if not on:
            continue
        here.append(rec)

    def _rank(rec: dict[str, Any]) -> tuple[int, int, int]:
        raw = str(rec.get("text") or "").strip()
        layer = rec.get("layer") or ""
        title_block = 1 if _TITLE_BLOCK_LAYER.search(layer) else 0
        if re.fullmatch(r"[A-Za-z0-9]|[上下左右东南西北]", raw):
            useful = 8
        elif _text_has_cjk(raw):
            useful = -min(len(_cjk_runs(raw) and "".join(_cjk_runs(raw)) or raw), 12)
        else:
            useful = 4
        return (title_block, useful, len(raw) * -1)

    here = [rec for rec in here if not re.fullmatch(r"[A-Za-z0-9]|[上下左右东南西北]", str(rec.get("text") or "").strip())] or here
    here.sort(key=_rank)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rec in here:
        key = str(rec.get("text") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def format_text_line(rec: dict[str, Any]) -> str:
    raw = str(rec.get("text") or "").replace("\n", " / ")
    line = f"- {raw}  坐标={rec.get('xy')}  图层={rec.get('layer') or ''}"
    if rec.get("sheet"):
        line += f"  所在={rec.get('sheet')}"
    if rec.get("host_block"):
        line += f"  所在底图块=`{rec.get('host_block')}`"
    return line


def apply_texts_to_index(index: dict[str, Any], texts: list[dict[str, Any]]) -> dict[str, Any]:
    """Store texts and fill room labels. Idempotent; does not drop existing index fields."""
    if not index or not isinstance(index, dict):
        return index
    sheets = index.get("sheets") or []
    rooms = index.get("rooms") or []
    located = locate_texts(list(texts or []), sheets)
    index["texts"] = located
    attach_labels_nearby(rooms, located)
    return index


def search_equipment(
    index: dict[str, Any],
    query: str,
    *,
    region: Optional[dict[str, Any]] = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Find equipment by official name, display_name, alias, category, or layer."""
    tokens = _query_tokens(query)
    if not tokens:
        tokens = []
    items = list(index.get("equipment") or [])
    if not items:
        for room in index.get("rooms") or []:
            items.extend(room.get("equipment_nearby") or [])
    region = region or {}
    sid = region.get("sheet_id") or ""
    title = region.get("title") or ""
    scored: list[tuple[int, dict[str, Any]]] = []
    for rec in items:
        blob = " ".join(
            str(rec.get(k) or "")
            for k in ("block", "name", "display_name", "category", "layer", "room", "sheet", "aliases")
        )
        if rec.get("aliases"):
            blob += " " + " ".join(str(a) for a in rec["aliases"])
        score = 0
        sheet = rec.get("sheet") or ""
        if sid and sid in sheet:
            score += 4
        if title and title in sheet:
            score += 6
        floor = region.get("floor") or ""
        if floor and rec.get("floor") == floor:
            score += 3
        if is_furniture_query(query) and is_furniture(rec):
            if is_dining_room_name(rec.get("room")):
                score += 4
        for tok in tokens:
            if not tok:
                continue
            if tok == (rec.get("display_name") or "") or tok == (rec.get("block") or ""):
                score += 20
            elif tok in (rec.get("display_name") or "") or tok in (rec.get("block") or ""):
                score += 14
            elif any(tok in str(a) or str(a) in tok for a in rec.get("aliases") or []):
                score += 14
            elif tok in blob:
                score += 6
        if tokens and score < 6:
            continue
        if tokens or (sid and sid in sheet) or (title and title in sheet):
            scored.append((score, rec))
    scored.sort(key=lambda t: -t[0])
    # Prefer current view when the query is about fixtures.
    if title or sid:
        here = [(s, r) for s, r in scored if (title and title in (r.get("sheet") or "")) or (sid and sid in (r.get("sheet") or ""))]
        if here:
            here_ids = {id(r) for _, r in here}
            scored = here + [(s, r) for s, r in scored if id(r) not in here_ids]
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for _, rec in scored:
        key = (rec.get("block") or rec.get("name"), tuple(rec.get("xy") or []), rec.get("sheet"))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def _query_tokens(text: str) -> list[str]:
    raw = text or ""
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_$-]{2,}", raw)
    stop = {"一个", "一下", "我们", "这个", "那个", "以及", "或者", "还有", "把", "改成", "都", "这里", "的"}
    # Expand common short forms so 「蹲便」hits「蹲便器」.
    extra: list[str] = []
    vocab = _vocab_terms()
    for p in parts:
        if p in stop:
            continue
        extra.append(p)
        if p.endswith("器") and len(p) >= 3:
            extra.append(p[:-1])
        for term in vocab:
            if term in p and term != p and term not in stop:
                extra.append(term)
    for term in vocab:
        if term in raw and term not in stop:
            extra.append(term)
    if "桌" in raw:
        extra.append("桌")
    if "椅" in raw:
        extra.append("椅")
    return list(dict.fromkeys(t for t in extra if t not in stop))


def format_equipment_line(rec: dict[str, Any]) -> str:
    name = rec.get("block") or rec.get("name") or "设备"
    label = rec.get("display_name")
    if label and label != name:
        shown = f"{label}（块名 `{name}`）"
    else:
        shown = f"`{name}`"
    bits = [
        f"- {shown}  坐标={rec.get('xy')}  旋转={rec.get('rot')}  图层={rec.get('layer') or ''}",
    ]
    line = bits[0]
    if rec.get("room"):
        line += f"  房间={rec['room']}"
    if rec.get("dist") is not None:
        line += f"  距房间名约={rec['dist']}mm"
    if rec.get("host_block"):
        line += f"  所在底图块=`{rec['host_block']}`"
    if rec.get("edit_hint") == "overlay":
        line += "  改法=在模型空间覆盖，勿改底图块定义"
    return line


def build_catalog(doc, inserts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    nested_counts: Counter[str] = Counter()
    layers: dict[str, Counter[str]] = defaultdict(Counter)
    for rec in inserts:
        name = rec.get("block") or rec.get("name")
        if not name:
            continue
        counts[name] += 1
        if rec.get("nested"):
            nested_counts[name] += 1
        if rec.get("layer"):
            layers[name][rec["layer"]] += 1
    blocks = []
    seen = set()
    names = [b.name for b in doc.blocks if not str(b.name).startswith("*")]
    for name in list(dict.fromkeys([_safe_name(n) for n in names] + list(counts))):
        if not name or name in seen:
            continue
        seen.add(name)
        profile = _block_profile(doc, name)
        typo = KNOWN_TYPOLOGIES.get(name)
        typ = layers[name].most_common(1)
        rec = {
            "name": name,
            "insert_count": int(counts.get(name, 0)),
            "nested_count": int(nested_counts.get(name, 0)),
            "size_mm": profile.get("size_mm"),
            "local_bbox": profile.get("local_bbox"),
            "typical_layer": typ[0][0] if typ else "",
        }
        if typo:
            rec["display_name"] = typo["display_name"]
            rec["aliases"] = typo.get("aliases") or []
            rec["category"] = typo.get("category")
        blocks.append(rec)
    return blocks


def enrich_inserts(doc, inserts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    library_map = _load_library_map()
    profiles: dict[str, dict[str, Any]] = {}
    for rec in inserts:
        name = rec.get("block") or rec.get("name") or ""
        if name not in profiles:
            profiles[name] = _block_profile(doc, name)
        classify_symbol(rec, profiles[name], library_map)
    return inserts


def equipment_from_inserts(inserts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [rec for rec in inserts if is_room_equipment(rec)]


def apply_to_index(
    index: dict[str, Any],
    *,
    inserts: list[dict[str, Any]],
    texts: Optional[list[dict[str, Any]]] = None,
    doc=None,
) -> dict[str, Any]:
    """Update an existing (possibly curated) index with flattened, labeled equipment."""
    sheets = index.get("sheets") or []
    rooms = index.get("rooms") or []
    enrich_inserts(doc, inserts) if doc is not None else inserts
    assign_locations(inserts, sheets)
    # Reset room equipment; keep room metadata.
    for room in rooms:
        room["equipment_nearby"] = []
    equipment = equipment_from_inserts(inserts)
    assign_rooms([rec for rec in equipment if is_attachable(rec)], rooms)
    assign_dining_furniture(equipment, rooms)
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
    index["schema"] = SCHEMA
    index["equipment"] = equipment
    index["equipment_unassigned"] = unassigned
    if doc is not None:
        # Merge catalog counts / labels into existing blocks when present.
        fresh = {b["name"]: b for b in build_catalog(doc, inserts)}
        existing = index.get("blocks") or []
        by_name = {b.get("name"): b for b in existing if b.get("name")}
        for name, b in fresh.items():
            if name in by_name:
                by_name[name]["insert_count"] = b.get("insert_count")
                by_name[name]["nested_count"] = b.get("nested_count")
                if b.get("display_name"):
                    by_name[name]["display_name"] = b["display_name"]
                if b.get("aliases"):
                    by_name[name]["aliases"] = b["aliases"]
                if b.get("category"):
                    by_name[name]["category"] = b["category"]
                if b.get("size_mm") and not by_name[name].get("size_mm") and not by_name[name].get("local_bbox"):
                    by_name[name]["size_mm"] = b.get("size_mm")
            else:
                existing.append(b)
        index["blocks"] = existing
    return index


def scan_drawing(doc, msp, sheets: list[dict[str, Any]], rooms: list[dict[str, Any]]) -> dict[str, Any]:
    inserts = collect_inserts(doc, msp)
    enrich_inserts(doc, inserts)
    assign_locations(inserts, sheets)
    for room in rooms:
        room["equipment_nearby"] = []
    equipment = equipment_from_inserts(inserts)
    assign_rooms([rec for rec in equipment if is_attachable(rec)], rooms)
    assign_dining_furniture(equipment, rooms)
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
    return {
        "inserts": inserts,
        "equipment": equipment,
        "equipment_unassigned": unassigned,
        "blocks": build_catalog(doc, inserts),
    }
