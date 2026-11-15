"""改图坐标的校验与吸附：纯几何，无副作用。

只读 DXF 文档和索引字典，不写文件、不写数据库、不发请求。这里解决两件事：
一是模型给的坐标「看着偏」——块基点常与图形中心不在一处；二是明显的几何
冲突（跑到图外、压墙、跟别的符号叠死、同一方案里重复落点）先查出来，
由上层决定是回喂模型还是只提示。
"""

from __future__ import annotations

import math
from typing import Any, Optional

from ezdxf import bbox as ezbbox
from ezdxf.math import Matrix44, Vec3

# 吸附总开关。任何一处觉得「太聪明了」，把它置 False 就退回原坐标。
SNAP_ENABLED = True

REGION_PAD_MM = 2000.0      # 分区外扩范围，超出这里算跑飞，不动它
CLAMP_MARGIN_MM = 100.0     # 夹回房间时离墙留的余量
SAME_KIND_MM = 300.0        # 同类符号偏移吸附的判定距离
ALIGN_MM = 200.0            # 同轴对齐的判定距离
MIN_MOVE_MM = 1.0           # 小于这个距离的修正不算吸附，也不写备注
DUP_MM = 50.0               # 同一方案里同块名算重复的距离
OVERLAP_RATIO = 0.25        # 外框相交面积占较小者的比例
WALL_HIT_MM = 20.0          # 插入点离墙线多近算「落在墙线上」
WALL_TOUCH_MM = 30.0        # 外框要压进墙线这么多才算压墙，贴边不算
WALL_LAYER_HINTS = ("墙", "WALL", "结构", "柱")
MAX_WALL_SEGMENTS = 20000   # 墙线解析上限，防止超大图纸拖住对话

_SEVERITY_ERROR = "error"
_SEVERITY_WARN = "warn"


# ---------------------------------------------------------------- 基础小工具


def _rect(value) -> Optional[tuple[float, float, float, float]]:
    """把 [x0,y0,x1,y1] / {"bbox":…} 统一成四元组，拿不到返回 None。"""
    if isinstance(value, dict):
        value = value.get("bbox") or value.get("frame") or value.get("extent")
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            x0, y0, x1, y1 = (float(v) for v in value[:4])
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
            return None
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        return (x0, y0, x1, y1)
    return None


def _xy(value) -> Optional[list[float]]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            return None
    return None


def _scale_pair(value) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0] or 1.0), float(value[1] or 1.0)
        except (TypeError, ValueError):
            return 1.0, 1.0
    if isinstance(value, (int, float)) and value:
        return float(value), float(value)
    return 1.0, 1.0


def _inside(x: float, y: float, rect, pad: float = 0.0) -> bool:
    if not rect:
        return True
    return rect[0] - pad <= x <= rect[2] + pad and rect[1] - pad <= y <= rect[3] + pad


def _dist(a, b) -> float:
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


# ---------------------------------------------------------------- 块的几何


def _block_layout(doc, name: str):
    try:
        if not name or name not in doc.blocks:
            return None
        return doc.blocks.get(name)
    except Exception:
        return None


def _block_corners(doc, name: str, cache: Optional[dict] = None):
    """块定义在本地坐标系里的四个角点；算不出时返回 None。"""
    if cache is not None and name in cache:
        return cache[name]
    corners = None
    layout = _block_layout(doc, name)
    if layout is not None:
        try:
            ext = ezbbox.extents(layout, fast=True)
        except Exception:
            ext = None
        if ext is not None and getattr(ext, "has_data", False):
            corners = [
                (float(ext.extmin.x), float(ext.extmin.y)),
                (float(ext.extmax.x), float(ext.extmin.y)),
                (float(ext.extmax.x), float(ext.extmax.y)),
                (float(ext.extmin.x), float(ext.extmax.y)),
            ]
    if cache is not None:
        cache[name] = corners
    return corners


def insert_matrix(doc, block_name: str, xy, rot: float = 0.0, scale=(1.0, 1.0)) -> Matrix44:
    """按块基点、旋转、缩放求出「块坐标 → 世界坐标」的矩阵。

    与 ezdxf 的 Insert.matrix44() 同构，但不依赖图上已有 insert，
    可以对任意候选落点先算一遍。
    """
    sx, sy = _scale_pair(scale)
    m = Matrix44.scale(sx, sy, 1.0)
    angle = float(rot or 0.0)
    if angle:
        m = m @ Matrix44.z_rotate(math.radians(angle))
    base = (0.0, 0.0)
    layout = _block_layout(doc, block_name)
    if layout is not None:
        try:
            bp = layout.block.dxf.base_point
            base = (float(bp.x), float(bp.y))
        except Exception:
            base = (0.0, 0.0)
    offset = m.transform_direction(Vec3(base[0], base[1], 0))
    m.set_row(3, (float(xy[0]) - offset.x, float(xy[1]) - offset.y, 0.0))
    return m


def symbol_world_bbox(doc, block_name: str, xy, rot: float = 0, scale=(1, 1)):
    """块插入到 xy 后的世界外框 (x0,y0,x1,y1)；块不存在或没有图形时返回 None。"""
    point = _xy(xy)
    if not block_name or not point:
        return None
    corners = _block_corners(doc, block_name)
    if not corners:
        return None
    try:
        matrix = insert_matrix(doc, block_name, point, rot, scale)
        pts = [matrix.transform(Vec3(cx, cy, 0)) for cx, cy in corners]
    except Exception:
        return None
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def symbol_center_offset(doc, block_name: str, rot: float = 0, scale=(1, 1)):
    """图形中心相对插入点的偏移；块没有尺寸时返回 None。

    DXF 块的基点经常不在图形中心，这个偏移就是「看着偏」的来源。
    """
    corners = _block_corners(doc, block_name)
    if not corners:
        return None
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    local = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
    try:
        matrix = insert_matrix(doc, block_name, (0.0, 0.0), rot, scale)
        p = matrix.transform(Vec3(local[0], local[1], 0))
    except Exception:
        return None
    return (p.x, p.y)


# ---------------------------------------------------------------- 分区净空


def _index_room_rect(region: dict[str, Any], index: Optional[dict[str, Any]]):
    """在索引里按房间名找净空范围；找不到返回 None。"""
    rooms = index.get("rooms") if isinstance(index, dict) else None
    if not isinstance(rooms, list):
        return None
    want = str(region.get("title") or region.get("name") or "").strip()
    floor = region.get("floor")
    try:
        from . import projects
    except Exception:
        return None
    for room in rooms:
        if not isinstance(room, dict):
            continue
        name = str(room.get("name") or "").strip()
        if want and name != want:
            continue
        if floor and room.get("floor") and room.get("floor") != floor:
            continue
        try:
            rect = _rect(projects._room_bbox(room))
        except Exception:
            rect = None
        if rect:
            return rect
    return None


def room_clear_rect(region, index) -> Optional[tuple[float, float, float, float]]:
    """分区的净空范围。

    房间分区优先用索引里的房间包围盒（它贴着墙线），其次用分区自身 bbox；
    两者都没有时按 projects 的老规则退化成 world_xy ± 6000（大厅 20000）。
    """
    region = region if isinstance(region, dict) else {}
    if index and region.get("kind") == "room":
        rect = _index_room_rect(region, index)
        if rect:
            return rect
    rect = _rect(region.get("bbox"))
    if rect:
        return rect
    point = _xy(region.get("world_xy"))
    if point:
        name = str(region.get("name") or region.get("title") or "")
        half = 20000.0 if any(k in name for k in ("餐厅", "食堂", "大厅")) else 6000.0
        return (point[0] - half, point[1] - half, point[0] + half, point[1] + half)
    return None


# ---------------------------------------------------------------- 已有符号


def _existing_inserts(doc, rect, *, block: Optional[str] = None) -> list[dict[str, Any]]:
    """分区内的顶层 insert（不做嵌套展开），按块名可选过滤。"""
    out: list[dict[str, Any]] = []
    try:
        msp = doc.modelspace()
    except Exception:
        return out
    for ent in msp.query("INSERT"):
        try:
            name = str(ent.dxf.name or "")
            pos = ent.dxf.insert
            x, y = float(pos.x), float(pos.y)
        except Exception:
            continue
        if block and name != block:
            continue
        if rect and not _inside(x, y, rect):
            continue
        try:
            rot = float(ent.dxf.rotation or 0.0)
        except Exception:
            rot = 0.0
        out.append({"block": name, "xy": [x, y], "rot": rot, "entity": ent})
    return out


def _entity_world_bbox(ent, corners):
    """用块角点 + 实体自己的矩阵求世界外框。"""
    if not corners:
        return None
    try:
        matrix = ent.matrix44()
    except Exception:
        try:
            pos = ent.dxf.insert
            matrix = Matrix44.translate(float(pos.x), float(pos.y), 0)
        except Exception:
            return None
    try:
        pts = [matrix.transform(Vec3(cx, cy, 0)) for cx, cy in corners]
    except Exception:
        return None
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------- 吸附


def snap_xy(doc, item, *, block: str, xy, bbox, region=None, index=None, kind: str = "insert"):
    """给一个候选落点做兜底吸附，返回 (xy, notes)。

    三种兜底，命中才改，改了就写一句人话；都不命中原样返回。
      a) 夹取：点越出分区但在外扩 2000mm 以内 → 夹回净空（留 100mm 余量）。
         完全跑飞的不动，交给 check_plan 报 out_of_region。
      b) 同类偏移吸附：旁边 300mm 内有同块名 insert → 按那个符号的
         「插入点 ↔ 图形中心」偏移修正落笔，让同类符号用同一套基点。
      c) 同轴对齐：分区里已有 ≥2 个同块名符号，新点与它们在 x 或 y 上
         相差 <200mm → 对齐到该轴。
    """
    point = _xy(xy)
    if not SNAP_ENABLED or not point:
        return point, []
    notes: list[str] = []
    x, y = point[0], point[1]
    rect = _rect(bbox)
    clear = room_clear_rect(region, index) or rect

    # a) 夹取：先确认没跑飞，再收到净空里。
    if rect and clear and not _inside(x, y, rect):
        if not _inside(x, y, rect, REGION_PAD_MM):
            return [x, y], []
        # 净空可能比分区还大（房间没给 bbox 时退化），夹取目标取两者的交集。
        target = clear
        if clear[0] < rect[0] or clear[1] < rect[1] or clear[2] > rect[2] or clear[3] > rect[3]:
            target = (
                max(clear[0], rect[0]),
                max(clear[1], rect[1]),
                min(clear[2], rect[2]),
                min(clear[3], rect[3]),
            )
        left, right = target[0] + CLAMP_MARGIN_MM, target[2] - CLAMP_MARGIN_MM
        bottom, top = target[1] + CLAMP_MARGIN_MM, target[3] - CLAMP_MARGIN_MM
        if left > right:
            left = right = (target[0] + target[2]) / 2.0
        if bottom > top:
            bottom = top = (target[1] + target[3]) / 2.0
        nx = min(max(x, left), right)
        ny = min(max(y, bottom), top)
        if _dist((x, y), (nx, ny)) >= MIN_MOVE_MM:
            notes.append(f"坐标越出分区，已收回到房间内（x={round(nx)}，y={round(ny)}）")
            x, y = nx, ny

    if not block:
        return [x, y], notes

    symbols = _existing_inserts(doc, clear or rect, block=block)

    # b) 同类偏移吸附：只认 300mm 内最近的一个。
    if symbols:
        nearest = min(symbols, key=lambda rec: _dist(rec["xy"], (x, y)))
        distance = _dist(nearest["xy"], (x, y))
        if 0 < distance <= SAME_KIND_MM:
            corners = _block_corners(doc, block)
            bbox_near = _entity_world_bbox(nearest["entity"], corners)
            if bbox_near:
                offset = (
                    (bbox_near[0] + bbox_near[2]) / 2.0 - nearest["xy"][0],
                    (bbox_near[1] + bbox_near[3]) / 2.0 - nearest["xy"][1],
                )
                if abs(offset[0]) >= MIN_MOVE_MM or abs(offset[1]) >= MIN_MOVE_MM:
                    # 模型给的是「看上去该在的位置」，按旁边符号的基点↔图形中心
                    # 关系反推插入点，两个符号的落笔口径就一致了。
                    nx, ny = x - offset[0], y - offset[1]
                    if _dist((x, y), (nx, ny)) >= MIN_MOVE_MM:
                        notes.append(
                            f"按旁边同类符号的基点偏移校正落笔（x={round(nx)}，y={round(ny)}）"
                        )
                        return [nx, ny], notes

    # c) 同轴对齐：至少两个同类符号才有「一条线」可言。
    if len(symbols) >= 2:
        nx, ny = x, y
        near_x = [rec["xy"][0] for rec in symbols if abs(rec["xy"][0] - x) <= ALIGN_MM]
        near_y = [rec["xy"][1] for rec in symbols if abs(rec["xy"][1] - y) <= ALIGN_MM]
        if near_x:
            nx = min(near_x, key=lambda v: abs(v - x))
        if near_y:
            ny = min(near_y, key=lambda v: abs(v - y))
        if _dist((x, y), (nx, ny)) >= MIN_MOVE_MM:
            axis = []
            if abs(nx - x) >= MIN_MOVE_MM:
                axis.append(f"x={round(nx)}")
            if abs(ny - y) >= MIN_MOVE_MM:
                axis.append(f"y={round(ny)}")
            notes.append("已与已有同类符号对齐（" + "，".join(axis) + "）")
            x, y = nx, ny

    return [x, y], notes


# ---------------------------------------------------------------- 校验


def _is_wall_layer(layer: str) -> bool:
    text = (layer or "").upper()
    return any(hint.upper() in text for hint in WALL_LAYER_HINTS)


def _polyline_points(ent) -> list[tuple[float, float]]:
    try:
        if ent.dxftype() == "LINE":
            s, e = ent.dxf.start, ent.dxf.end
            return [(float(s.x), float(s.y)), (float(e.x), float(e.y))]
        pts = []
        for p in ent.get_points("xy"):
            pts.append((float(p[0]), float(p[1])))
        return pts
    except Exception:
        return []


def _closed_polyline(ent) -> bool:
    try:
        if ent.dxftype() == "LWPOLYLINE":
            return bool(ent.closed)
        if ent.dxftype() == "POLYLINE":
            return bool(ent.is_closed)
    except Exception:
        pass
    return False


def _wall_segments(doc, rect) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """图层带墙/结构/柱的线，近似成线段集合；只收落在分区附近的。"""
    segments: list[tuple[tuple[float, float], tuple[float, float]]] = []
    try:
        msp = doc.modelspace()
        candidates = msp.query("LINE LWPOLYLINE POLYLINE")
    except Exception:
        return segments
    for ent in candidates:
        try:
            kind = ent.dxftype()
            layer = str(getattr(ent.dxf, "layer", "") or "")
            if not _is_wall_layer(layer):
                continue
            pts = _polyline_points(ent)
            if len(pts) < 2:
                continue
            pairs = list(zip(pts, pts[1:]))
            if _closed_polyline(ent):
                pairs.append((pts[-1], pts[0]))
            for a, b in pairs:
                if rect and not _segment_near_rect(a, b, rect, pad=0.0):
                    continue
                segments.append((a, b))
                if len(segments) >= MAX_WALL_SEGMENTS:
                    return segments
        except Exception:
            continue
    return segments


def _segment_near_rect(a, b, rect, pad: float = 0.0) -> bool:
    x0, x1 = sorted((a[0], b[0]))
    y0, y1 = sorted((a[1], b[1]))
    return not (
        x1 < rect[0] - pad or x0 > rect[2] + pad or y1 < rect[1] - pad or y0 > rect[3] + pad
    )


def _segments_intersect(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_seg(p, q, r):
        return (
            min(p[0], q[0]) - 1e-9 <= r[0] <= max(p[0], q[0]) + 1e-9
            and min(p[1], q[1]) - 1e-9 <= r[1] <= max(p[1], q[1]) + 1e-9
        )

    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0):
        return True
    if abs(o1) < 1e-9 and on_seg(a, b, c):
        return True
    if abs(o2) < 1e-9 and on_seg(a, b, d):
        return True
    if abs(o3) < 1e-9 and on_seg(c, d, a):
        return True
    if abs(o4) < 1e-9 and on_seg(c, d, b):
        return True
    return False


def _segment_hits_rect(a, b, rect, *, inset: float = 0.0) -> bool:
    """线段是否穿进矩形；贴边不算，inset 让判断再收一点。"""
    if inset:
        x0, y0 = rect[0] + inset, rect[1] + inset
        x1, y1 = rect[2] - inset, rect[3] - inset
        if x1 <= x0 or y1 <= y0:
            cx, cy = (rect[0] + rect[2]) / 2.0, (rect[1] + rect[3]) / 2.0
            x0, x1, y0, y1 = cx, cx, cy, cy
        rect = (x0, y0, x1, y1)
    if _inside(a[0], a[1], rect) or _inside(b[0], b[1], rect):
        return True
    corners = [(rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])]
    edges = list(zip(corners, corners[1:] + corners[:1]))
    return any(_segments_intersect(a, b, c, d) for c, d in edges)


def _overlap_ratio(a, b) -> float:
    if not a or not b:
        return 0.0
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    area = w * h
    small = min(
        max((a[2] - a[0]) * (a[3] - a[1]), 0.0),
        max((b[2] - b[0]) * (b[3] - b[1]), 0.0),
    )
    if small <= 0:
        return 0.0
    return area / small


def _issue(code, severity, message, op, index, block, xy) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "op": op,
        "index": index,
        "block": block or "",
        "xy": list(xy) if xy else None,
    }


def check_plan(doc, plan, *, bbox=None, region=None, index=None) -> list[dict[str, Any]]:
    """校验方案里的坐标，返回 issue 列表；任何异常都不往外抛。

    每项：{"code","severity","message","op","index","block","xy"}。
    severity=error 表示几何上必然画错，warn 表示可疑但可以接受。
    """
    issues: list[dict[str, Any]] = []
    if not isinstance(plan, dict):
        return issues
    try:
        doc.modelspace()
    except Exception:
        return issues

    rect = _rect(bbox)
    if not rect:
        rect = _rect((region or {}).get("bbox")) if isinstance(region, dict) else None
    clear = room_clear_rect(region or {}, index)
    walls = _wall_segments(doc, rect)
    existing = _existing_inserts(doc, rect)
    corner_cache: dict[str, Any] = {}
    existing_boxes = []
    for rec in existing:
        corners = _block_corners(doc, rec["block"], corner_cache)
        existing_boxes.append(
            {"block": rec["block"], "xy": rec["xy"], "bbox": _entity_world_bbox(rec["entity"], corners)}
        )

    # 方案里会被移走/删掉的旧符号，不参与重叠判定。
    removed: list[list[float]] = []
    for item in plan.get("deletes") or []:
        if isinstance(item, dict):
            p = _xy(item.get("xy") or item.get("from"))
            if p:
                removed.append(p)
    for item in plan.get("replaces") or []:
        if isinstance(item, dict):
            p = _xy(item.get("from_xy") or item.get("from"))
            if p:
                removed.append(p)

    def _is_removed(point) -> bool:
        return any(_dist(point, r) <= SAME_KIND_MM for r in removed)

    planned: list[dict[str, Any]] = []

    def _check_point_only(op, idx, point, label=""):
        if not point:
            issues.append(_issue("missing_xy", _SEVERITY_WARN, f"{label}缺少可用坐标", op, idx, "", None))
            return
        if rect and not _inside(point[0], point[1], rect, REGION_PAD_MM):
            issues.append(
                _issue("out_of_region", _SEVERITY_ERROR, f"{label}坐标落在分区之外", op, idx, "", point)
            )
            return
        if clear and not _inside(point[0], point[1], clear):
            issues.append(
                _issue("outside_room", _SEVERITY_ERROR, f"{label}坐标落在房间净空之外", op, idx, "", point)
            )

    def _check_symbol(op, idx, item, *, block, point, rot, scale):
        if not block:
            issues.append(_issue("missing_block", _SEVERITY_WARN, "缺少块名，无法核对外框", op, idx, "", point))
            return
        if not point:
            issues.append(_issue("missing_xy", _SEVERITY_WARN, f"「{block}」缺少可用坐标", op, idx, block, None))
            return
        outside = False
        if rect and not _inside(point[0], point[1], rect, REGION_PAD_MM):
            issues.append(
                _issue("out_of_region", _SEVERITY_ERROR, f"「{block}」坐标落在分区之外", op, idx, block, point)
            )
            outside = True
        elif clear and not _inside(point[0], point[1], clear):
            issues.append(
                _issue("outside_room", _SEVERITY_ERROR, f"「{block}」坐标落在房间净空之外", op, idx, block, point)
            )
        world = symbol_world_bbox(doc, block, point, rot, scale)
        if not world:
            issues.append(
                _issue(
                    "missing_size",
                    _SEVERITY_WARN,
                    f"缺少块尺寸，无法核对「{block}」与墙、其他符号的关系",
                    op,
                    idx,
                    block,
                    point,
                )
            )
            planned.append({"op": op, "index": idx, "block": block, "xy": point})
            return
        # 压墙：插入点贴线，或外框明显穿进墙线（贴边不算）。
        for a, b in walls:
            if _dist(point, _closest_on_segment(point, a, b)) <= WALL_HIT_MM:
                issues.append(
                    _issue("over_wall", _SEVERITY_ERROR, f"「{block}」落在墙线上", op, idx, block, point)
                )
                break
            if _segment_hits_rect(a, b, world, inset=WALL_TOUCH_MM):
                issues.append(
                    _issue("over_wall", _SEVERITY_ERROR, f"「{block}」外框压到墙线", op, idx, block, point)
                )
                break
        # 重叠：只跟还没被本方案挪走的已有符号比。
        if not outside and not _is_removed(point):
            best = None
            for rec in existing_boxes:
                if not rec["bbox"] or _is_removed(rec["xy"]):
                    continue
                ratio = _overlap_ratio(world, rec["bbox"])
                if ratio > OVERLAP_RATIO and (best is None or ratio > best[0]):
                    best = (ratio, rec)
            if best:
                ratio, rec = best
                issues.append(
                    _issue(
                        "overlap_symbol",
                        _SEVERITY_ERROR,
                        f"「{block}」与已有「{rec['block']}」外框重叠约 {round(ratio * 100)}%",
                        op,
                        idx,
                        block,
                        point,
                    )
                )
        planned.append({"op": op, "index": idx, "block": block, "xy": point})

    for idx, item in enumerate(plan.get("inserts") or []):
        if not isinstance(item, dict):
            continue
        _check_symbol(
            "insert",
            idx,
            item,
            block=str(item.get("block") or "").strip(),
            point=_xy(item.get("xy") or item.get("to")),
            rot=item.get("rot") or 0,
            scale=item.get("scale"),
        )
    for idx, item in enumerate(plan.get("replaces") or []):
        if not isinstance(item, dict):
            continue
        _check_symbol(
            "replace",
            idx,
            item,
            block=str(item.get("block") or "").strip(),
            point=_xy(item.get("to") or item.get("xy") or item.get("from_xy")),
            rot=item.get("rot") or 0,
            scale=item.get("scale"),
        )
    for idx, item in enumerate(plan.get("moves") or []):
        if not isinstance(item, dict):
            continue
        _check_symbol(
            "move",
            idx,
            item,
            block=str(item.get("block") or "").strip(),
            point=_xy(item.get("to") or item.get("xy")),
            rot=item.get("rot") or 0,
            scale=item.get("scale"),
        )
    for idx, item in enumerate(plan.get("texts") or []):
        if not isinstance(item, dict):
            continue
        _check_point_only("text", idx, _xy(item.get("xy")), label="文字")
    for idx, item in enumerate(plan.get("pipes") or []):
        if not isinstance(item, dict):
            continue
        a = _xy(item.get("from"))
        b = _xy(item.get("to"))
        if not a or not b:
            issues.append(_issue("missing_xy", _SEVERITY_WARN, "管线缺少端点", "pipe", idx, "", a or b))
            continue
        for point in (a, b):
            if rect and not _inside(point[0], point[1], rect, REGION_PAD_MM):
                issues.append(
                    _issue("out_of_region", _SEVERITY_ERROR, "管线端点落在分区之外", "pipe", idx, "", point)
                )
                break
            if clear and not _inside(point[0], point[1], clear):
                issues.append(
                    _issue("outside_room", _SEVERITY_ERROR, "管线端点落在房间净空之外", "pipe", idx, "", point)
                )
                break

    # 同一方案里的重复落点。
    for i in range(len(planned)):
        for j in range(i + 1, len(planned)):
            a, b = planned[i], planned[j]
            if not a["block"] or a["block"] != b["block"] or not a["xy"] or not b["xy"]:
                continue
            if _dist(a["xy"], b["xy"]) < DUP_MM:
                issues.append(
                    _issue(
                        "duplicate",
                        _SEVERITY_ERROR,
                        f"同一方案里「{a['block']}」有两处几乎重合的坐标",
                        b["op"],
                        b["index"],
                        b["block"],
                        b["xy"],
                    )
                )
    return issues


def _closest_on_segment(p, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return a
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return (ax + t * dx, ay + t * dy)

# ------------------------------------------------------------ 预览取景窗口

CONTEXT_GROW = 0.5             # 窗口 = 改动框的 2 倍（每边再多半个改动框）
CONTEXT_MIN_SPAN = 8000.0      # 再小的改动也至少看到 8 米见方，否则没有参照
CONTEXT_MAX_RATIO = 0.60       # 每个轴最多占图幅 60%，再大就直接给整幅
CONTEXT_MAX_AREA_RATIO = 0.60  # 面积超过图幅 60% 就直接给整幅


def context_bbox(
    dirty,
    view=None,
    *,
    grow: float = CONTEXT_GROW,
    min_span: float = CONTEXT_MIN_SPAN,
    max_ratio: float = CONTEXT_MAX_RATIO,
    max_area_ratio: float = CONTEXT_MAX_AREA_RATIO,
) -> Optional[list[float]]:
    """把「改动框」扩成带周边环境的取景框。

    只裁改动框的话，图里除了那一两个符号什么都没有：既看不出原来长什么样，
    也没法判断位置对不对。这里按「改动约占窗口一半」取景，四周留出同类构件、
    墙体和轴网做参照；改动本身很大时最多取到图幅的 40%，再大就整幅。
    """
    box = _rect(dirty)
    if not box:
        return None
    x0, y0, x1, y1 = box
    dw = max(x1 - x0, 1.0)
    dh = max(y1 - y0, 1.0)
    tw = max(dw * (1.0 + 2.0 * grow), min_span)
    th = max(dh * (1.0 + 2.0 * grow), min_span)
    frame = _rect(view)
    if not frame:
        # 没有图幅信息时只能按改动自身外扩。
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        return [cx - tw / 2.0, cy - th / 2.0, cx + tw / 2.0, cy + th / 2.0]
    vx0, vy0, vx1, vy1 = frame
    vw = max(vx1 - vx0, 1.0)
    vh = max(vy1 - vy0, 1.0)
    tw = min(tw, vw * max_ratio if max_ratio < 1.0 else vw)
    th = min(th, vh * max_ratio if max_ratio < 1.0 else vh)
    # 窗口无论如何都要盖住改动本身，不能被上限压到比改动还小。
    tw = max(tw, dw)
    th = max(th, dh)
    tw = max(min(tw, vw), 1.0)
    th = max(min(th, vh), 1.0)
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    nx0, nx1 = cx - tw / 2.0, cx + tw / 2.0
    ny0, ny1 = cy - th / 2.0, cy + th / 2.0
    # 先整体平移回图幅内，避免窗口切掉改动的一角。
    if nx0 < vx0:
        nx1 += vx0 - nx0
        nx0 = vx0
    if nx1 > vx1:
        nx0 -= nx1 - vx1
        nx1 = vx1
    if ny0 < vy0:
        ny1 += vy0 - ny0
        ny0 = vy0
    if ny1 > vy1:
        ny0 -= ny1 - vy1
        ny1 = vy1
    nx0 = max(nx0, vx0)
    ny0 = max(ny0, vy0)
    nx1 = min(nx1, vx1)
    ny1 = min(ny1, vy1)
    if nx1 - nx0 <= 1.0 or ny1 - ny0 <= 1.0:
        return [vx0, vy0, vx1, vy1]
    if (nx1 - nx0) * (ny1 - ny0) >= max_area_ratio * vw * vh:
        return [vx0, vy0, vx1, vy1]
    return [nx0, ny0, nx1, ny1]
