"""Vector scene for the right-hand drawing view.

Builds a lightweight JSON scene for the current subview and incremental
patches after an in-memory apply. The official / work DXF is not written.
Shared underlay block definitions are never edited or exploded on disk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.backend import BackendInterface, BkPath2d, BkPoints2d, ImageData
from ezdxf.addons.drawing.config import (
    BackgroundPolicy,
    ColorPolicy,
    Configuration,
    HatchPolicy,
    TextPolicy,
)
from ezdxf.addons.drawing.properties import Filling
from ezdxf.addons.drawing.type_hints import Color
from ezdxf.math import Matrix44, Vec2, Vec3
from ezdxf.path import Command

from . import apply, config, placement

log = logging.getLogger("tutan.scene")

SCENE_VERSION = 1
PAPER = "#FFFFFF"
INK = "#1C2B33"
TEXT_TYPES = {"TEXT", "MTEXT", "ATTDEF", "ATTRIB"}
# Skip these when flattening the modelspace pass; inserts become symbol instances.
INSTANCE_TYPES = {"INSERT"}
MARGIN = 400.0

_SCENE_LOCK = threading.Lock()
_SCENE_CACHE: dict[str, tuple[float, int, dict[str, Any]]] = {}


def _r(v: float, nd: int = 2) -> float | int:
    n = round(float(v), nd)
    if n == int(n):
        return int(n)
    return n


def _pt(x: float, y: float, nd: int = 2) -> list[float | int]:
    return [_r(x, nd), _r(y, nd)]


def _handle(ent) -> Optional[str]:
    try:
        h = ent.dxf.handle
        return str(h) if h is not None else None
    except Exception:
        return None


def _read_doc(path: Path, encoding: str = "utf-8"):
    try:
        return ezdxf.readfile(str(path), encoding=encoding)
    except Exception:
        return ezdxf.readfile(str(path))


def _load_readonly_doc(path: Path, encoding: str = "utf-8"):
    """Prefer the shared renderer cache, but never mutate the returned doc."""
    try:
        from . import cad_render

        return cad_render.load_doc(Path(path), encoding=encoding)
    except Exception:
        return _read_doc(Path(path), encoding)


def _sym_key(name: str) -> str:
    return "s-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def _contrast(color: Optional[str], default: str = INK) -> str:
    if not color:
        return default
    c = color.strip()
    if c.startswith("#") and len(c) >= 7:
        try:
            r = int(c[1:3], 16)
            g = int(c[3:5], 16)
            b = int(c[5:7], 16)
        except ValueError:
            return default
        # ACI 7 (white) on a paper background would vanish.
        if (0.2126 * r + 0.7152 * g + 0.0722 * b) > 230:
            return default
        if len(c) >= 9 and c[7:9].lower() == "ff":
            return c[:7]
        return c[:7] if len(c) >= 7 else default
    return c


def _layer_off(doc, name: str) -> bool:
    try:
        layer = doc.layers.get(name)
    except Exception:
        return False
    if layer is None:
        return False
    try:
        if layer.is_off() or layer.is_frozen():
            return True
    except Exception:
        pass
    if str(name).lower() == "defpoints":
        return True
    return False


def _entity_points(e) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    d = e.dxf
    t = e.dxftype()
    try:
        if t == "INSERT":
            p = d.insert
            pts.append((float(p.x), float(p.y)))
            return pts
        if t in TEXT_TYPES:
            for attr in ("insert", "align_point"):
                if d.hasattr(attr):
                    p = getattr(d, attr)
                    pts.append((float(p.x), float(p.y)))
            return pts
        if t == "LWPOLYLINE":
            return [(float(p[0]), float(p[1])) for p in e.get_points("xy")]
        if t == "LINE":
            return [(float(d.start.x), float(d.start.y)), (float(d.end.x), float(d.end.y))]
        if t in {"CIRCLE", "ARC"}:
            r = float(d.radius)
            c = d.center
            return [(c.x + r, c.y), (c.x - r, c.y), (c.x, c.y + r), (c.x, c.y - r)]
        if t == "POLYLINE":
            return [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in e.vertices]
        if t == "HATCH":
            for p in e.paths:
                try:
                    for v in getattr(p, "vertices", []):
                        if hasattr(v, "x"):
                            pts.append((float(v.x), float(v.y)))
                        elif isinstance(v, (list, tuple)):
                            pts.append((float(v[0]), float(v[1])))
                except Exception:
                    pass
            return pts
        if t == "DIMENSION":
            for attr in ("defpoint", "defpoint2", "defpoint3", "text_midpoint", "insert"):
                try:
                    if d.hasattr(attr):
                        p = getattr(d, attr)
                        pts.append((float(p.x), float(p.y)))
                except Exception:
                    pass
            return pts
        if t == "WIPEOUT":
            try:
                return [(float(v.x), float(v.y)) for v in e.boundary_path_wcs()]
            except Exception:
                return pts
        for attr in ("insert", "start", "end", "center", "align_point"):
            if d.hasattr(attr):
                p = getattr(d, attr)
                pts.append((float(p.x), float(p.y)))
    except Exception:
        return pts
    return pts


def _entity_bbox(e) -> Optional[tuple[float, float, float, float]]:
    pts = _entity_points(e)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _overlaps(bb, bbox, margin: float = 0.0) -> bool:
    if not bb or not bbox or len(bbox) < 4:
        return False
    xmin = float(bbox[0]) - margin
    ymin = float(bbox[1]) - margin
    xmax = float(bbox[2]) + margin
    ymax = float(bbox[3]) + margin
    return not (bb[2] < xmin or bb[0] > xmax or bb[3] < ymin or bb[1] > ymax)


def filter_region_entities(msp, bbox, *, margin: float = MARGIN) -> list[Any]:
    out = []
    for e in msp:
        try:
            if _layer_off(msp.doc, e.dxf.layer):
                continue
        except Exception:
            pass
        bb = _entity_bbox(e)
        if not bb:
            # Keep DIMENSION / VIEWPORT-less types that may still draw.
            kind = e.dxftype()
            if kind in {"DIMENSION", "MULTILEADER"}:
                out.append(e)
            continue
        if _overlaps(bb, bbox, margin=margin):
            out.append(e)
    return out


def _path_d(path: BkPath2d, close: bool = False) -> str:
    if len(path) == 0:
        return ""
    parts: list[str] = [f"M{_r(path.start.x)} {_r(path.start.y)}"]
    for cmd in path.commands():
        end = cmd.end
        if cmd.type == Command.MOVE_TO:
            parts.append(f"M{_r(end.x)} {_r(end.y)}")
        elif cmd.type == Command.LINE_TO:
            parts.append(f"L{_r(end.x)} {_r(end.y)}")
        elif cmd.type == Command.CURVE3_TO:
            c1 = cmd.ctrl
            parts.append(f"Q{_r(c1.x)} {_r(c1.y)} {_r(end.x)} {_r(end.y)}")
        elif cmd.type == Command.CURVE4_TO:
            c1 = cmd.ctrl1
            c2 = cmd.ctrl2
            parts.append(
                f"C{_r(c1.x)} {_r(c1.y)} {_r(c2.x)} {_r(c2.y)} {_r(end.x)} {_r(end.y)}"
            )
    if close:
        parts.append("Z")
    return "".join(parts)


class SceneBackend(BackendInterface):
    """Collect compact path / fill primitives for a web scene."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._stack: list[Optional[str]] = []
        self.min_lineweight = 0.05
        self.lineweight_scaling = 1.0
        self.fixed_lineweight = 0.0

    def configure(self, config: Configuration) -> None:
        if config.min_lineweight:
            self.min_lineweight = max(0.05, config.min_lineweight * 25.4 / 300)
        self.lineweight_scaling = config.lineweight_scaling
        if self.lineweight_scaling == 0.0:
            self.fixed_lineweight = self.min_lineweight

    def clear(self) -> None:
        self.items.clear()
        self._stack.clear()

    def set_background(self, color: Color) -> None:
        return

    def finalize(self) -> None:
        return

    def draw_image(self, image_data: ImageData, properties) -> None:
        return

    def enter_entity(self, entity, properties) -> None:
        self._stack.append(_handle(entity))

    def exit_entity(self, entity) -> None:
        if self._stack:
            self._stack.pop()

    def _props(self, properties) -> dict[str, Any]:
        if self.fixed_lineweight:
            w = self.fixed_lineweight
        else:
            try:
                w = max(self.min_lineweight, float(properties.lineweight) * self.lineweight_scaling)
            except Exception:
                w = self.min_lineweight
        out: dict[str, Any] = {"c": _contrast(getattr(properties, "color", None))}
        if w and w > 0:
            out["w"] = _r(w, 2)
        layer = getattr(properties, "layer", None)
        if layer:
            out["l"] = str(layer)
        hid = self._stack[0] if self._stack else None
        if hid:
            out["id"] = hid
        return out

    def _add(self, item: dict[str, Any]) -> None:
        self.items.append(item)

    def draw_point(self, pos: Vec2, properties) -> None:
        item = self._props(properties)
        item.update({"t": "pt", "x": _r(pos.x), "y": _r(pos.y)})
        self._add(item)

    def draw_line(self, start: Vec2, end: Vec2, properties) -> None:
        item = self._props(properties)
        item.update({"t": "lines", "p": [_r(start.x), _r(start.y), _r(end.x), _r(end.y)]})
        self._add(item)

    def draw_solid_lines(self, lines: Iterable[tuple[Vec2, Vec2]], properties) -> None:
        pts: list[float | int] = []
        for s, e in lines:
            pts.extend([_r(s.x), _r(s.y), _r(e.x), _r(e.y)])
        if not pts:
            return
        item = self._props(properties)
        item.update({"t": "lines", "p": pts})
        self._add(item)

    def draw_path(self, path: BkPath2d, properties) -> None:
        d = _path_d(path)
        if not d:
            return
        item = self._props(properties)
        item.update({"t": "path", "d": d})
        self._add(item)

    def draw_filled_paths(self, paths: Iterable[BkPath2d], properties) -> None:
        ds = []
        for path in paths:
            d = _path_d(path, close=True)
            if d:
                ds.append(d)
        if not ds:
            return
        item = self._props(properties)
        item.update({"t": "path", "d": "".join(ds), "fill": 1})
        self._add(item)

    def draw_filled_polygon(self, points: BkPoints2d, properties) -> None:
        verts = list(points.vertices())
        if len(verts) < 3:
            return
        pts: list[float | int] = []
        for v in verts:
            pts.extend([_r(v.x), _r(v.y)])
        if not (verts[0].isclose(verts[-1])):
            pts.extend([_r(verts[0].x), _r(verts[0].y)])
        item = self._props(properties)
        item.update({"t": "poly", "p": pts, "fill": 1})
        self._add(item)


class _PaperWipeoutFrontend(Frontend):
    def draw_wipeout_entity(self, entity, properties) -> None:
        properties.filling = Filling()
        properties.color = PAPER
        try:
            clipping_polygon = entity.boundary_path_wcs()
            self.pipeline.draw_filled_polygon(clipping_polygon, properties)
        except Exception:
            pass


def _config() -> Configuration:
    return Configuration(
        background_policy=BackgroundPolicy.CUSTOM,
        custom_bg_color=PAPER,
        color_policy=ColorPolicy.COLOR,
        hatch_policy=HatchPolicy.SHOW_SOLID,
        text_policy=TextPolicy.IGNORE,
        lineweight_scaling=1.0,
        min_lineweight=0.12,
        hatching_timeout=1.5,
        max_flattening_distance=0.5,
        circle_approximation_count=48,
    )


def _draw_entities(doc, entities: Iterable[Any]) -> list[dict[str, Any]]:
    ents = [e for e in entities if e is not None]
    if not ents:
        return []
    ctx = RenderContext(doc)
    try:
        ctx.set_current_layout(doc.modelspace())
    except Exception:
        pass
    backend = SceneBackend()
    frontend = _PaperWipeoutFrontend(ctx, backend, config=_config())
    try:
        frontend.draw_entities(ents)
    except Exception:
        for e in ents:
            try:
                frontend.draw_entities([e])
            except Exception:
                continue
    return backend.items


def _text_anchor(ent) -> str:
    try:
        halign = int(ent.dxf.halign)
    except Exception:
        halign = 0
    if halign in {1, 4}:  # center / middle
        return "middle"
    if halign in {2, 3, 5}:  # right / aligned / fit
        return "end"
    return "start"


def _entity_anchor(ent) -> Any:  # noqa: ANN401 - ezdxf Vec3
    """对齐文字（正中对齐等）的定位点在 align_point，insert 是 (0,0)。"""
    from . import dxftext

    point = dxftext.primary_anchor(ent)
    if point:
        return Vec3(point[0], point[1], 0)
    return ent.dxf.insert


def _text_payload(ent, *, matrix: Optional[Matrix44] = None) -> Optional[dict[str, Any]]:
    from . import dxftext

    kind = ent.dxftype()
    try:
        if kind == "MTEXT":
            height = float(ent.dxf.char_height or 175)
            pos = ent.dxf.insert
            rot = float(getattr(ent.dxf, "rotation", 0) or 0)
        else:
            height = float(ent.dxf.height or 175)
            pos = _entity_anchor(ent)
            rot = float(getattr(ent.dxf, "rotation", 0) or 0)
    except Exception:
        return None
    # 与索引、改文字共用同一套清洗：标点逐字保留，%%c 等解码。
    text = dxftext.clean_entity_text(ent)
    if not text:
        return None
    x, y = float(pos.x), float(pos.y)
    if matrix is not None:
        p = matrix.transform(Vec3(x, y, 0))
        x, y = float(p.x), float(p.y)
        try:
            ux = matrix.transform_direction(Vec3(1, 0, 0))
            rot = math.degrees(math.atan2(ux.y, ux.x))
        except Exception:
            pass
        try:
            # Uniform-ish scale of height from the matrix.
            uy = matrix.transform_direction(Vec3(0, 1, 0))
            height *= math.hypot(uy.x, uy.y) or 1.0
        except Exception:
            pass
    item: dict[str, Any] = {
        "t": "text",
        "x": _r(x),
        "y": _r(y),
        "s": text,
        "h": _r(height, 1),
        "a": _text_anchor(ent),
    }
    if abs(rot) > 0.05:
        item["r"] = _r(rot, 2)
    hid = _handle(ent)
    if hid:
        item["id"] = hid
    try:
        item["l"] = str(ent.dxf.layer)
    except Exception:
        pass
    return item


def _collect_texts(doc, entities: Iterable[Any], bbox=None, *, margin: float = MARGIN) -> list[dict[str, Any]]:
    items = []
    ctx = None
    try:
        ctx = RenderContext(doc)
        ctx.set_current_layout(doc.modelspace())
    except Exception:
        ctx = None
    for ent in entities:
        if ent is None:
            continue
        if ent.dxftype() not in TEXT_TYPES:
            continue
        try:
            if _layer_off(doc, ent.dxf.layer):
                continue
        except Exception:
            pass
        item = _text_payload(ent)
        if not item:
            continue
        if bbox and not _overlaps((item["x"], item["y"], item["x"], item["y"]), bbox, margin=margin):
            continue
        color = INK
        if ctx is not None:
            try:
                color = _contrast(ctx.resolve_all(ent).color)
            except Exception:
                color = INK
        item["c"] = color
        items.append(item)
    return items


def _insert_record(ent) -> Optional[dict[str, Any]]:
    if ent is None or ent.dxftype() != "INSERT":
        return None
    try:
        name = str(ent.dxf.name)
        p = ent.dxf.insert
    except Exception:
        return None
    rec: dict[str, Any] = {
        "t": "insert",
        "block": name,
        "sym": _sym_key(name),
        "x": _r(float(p.x)),
        "y": _r(float(p.y)),
    }
    try:
        rot = float(ent.dxf.rotation or 0)
        if abs(rot) > 0.05:
            rec["rot"] = _r(rot, 3)
    except Exception:
        pass
    try:
        sx = float(ent.dxf.xscale or 1)
        sy = float(ent.dxf.yscale or 1)
        if abs(sx - 1) > 1e-6:
            rec["sx"] = _r(sx, 4)
        if abs(sy - 1) > 1e-6:
            rec["sy"] = _r(sy, 4)
    except Exception:
        pass
    hid = _handle(ent)
    if hid:
        rec["id"] = hid
    try:
        rec["l"] = str(ent.dxf.layer)
    except Exception:
        pass
    return rec


def _block_texts(doc, name: str, matrix: Optional[Matrix44] = None) -> list[dict[str, Any]]:
    try:
        blk = doc.blocks.get(name)
    except Exception:
        return []
    if blk is None:
        return []
    items = []
    for ent in blk:
        kind = ent.dxftype()
        if kind in TEXT_TYPES:
            item = _text_payload(ent, matrix=matrix)
            if item:
                item["c"] = item.get("c") or INK
                items.append(item)
        elif kind == "INSERT":
            try:
                child = ent.matrix44()
                mat = child if matrix is None else matrix @ child
                items.extend(_block_texts(doc, str(ent.dxf.name), mat))
            except Exception:
                continue
    return items


def build_symbol(doc, name: str) -> Optional[dict[str, Any]]:
    try:
        blk = doc.blocks.get(name)
    except Exception:
        return None
    if blk is None:
        return None
    items = _draw_entities(doc, list(blk))
    items.extend(_block_texts(doc, name))
    if not items:
        # Still return an empty symbol so instances have a hook.
        items = []
    xs: list[float] = []
    ys: list[float] = []
    for it in items:
        if it.get("t") in {"path", "poly", "lines", "text", "pt"}:
            if "x" in it and "y" in it:
                xs.append(float(it["x"]))
                ys.append(float(it["y"]))
            p = it.get("p")
            if p:
                for i in range(0, len(p) - 1, 2):
                    xs.append(float(p[i]))
                    ys.append(float(p[i + 1]))
    bb = [_r(min(xs)), _r(min(ys)), _r(max(xs)), _r(max(ys))] if xs and ys else None
    return {"name": name, "key": _sym_key(name), "bbox": bb, "items": items, "n": len(items)}


def _empty_scene(bbox, *, view_id: Optional[str] = None) -> dict[str, Any]:
    return {
        "version": SCENE_VERSION,
        "bbox": [float(v) for v in bbox] if bbox and len(bbox) == 4 else None,
        "view_id": view_id,
        "pending": False,
        "stamp": None,
        "background": PAPER,
        "symbols": {},
        "items": [],
        "counts": {"items": 0, "inserts": 0, "texts": 0, "symbols": 0},
    }


def build_region_scene(
    doc,
    bbox,
    *,
    view_id: Optional[str] = None,
    pending: bool = False,
    stamp: Optional[str] = None,
    entities: Optional[list[Any]] = None,
) -> dict[str, Any]:
    """Flatten the current subview into a symbol + instance scene."""
    if not bbox or len(bbox) != 4:
        raise ValueError("没有可显示的图幅范围。")
    msp = doc.modelspace()
    ents = entities if entities is not None else filter_region_entities(msp, bbox)
    inserts = [e for e in ents if e.dxftype() == "INSERT"]
    texts = [e for e in ents if e.dxftype() in TEXT_TYPES]
    rest = [e for e in ents if e.dxftype() not in INSTANCE_TYPES | TEXT_TYPES]

    items = _draw_entities(doc, rest)
    items.extend(_collect_texts(doc, texts, bbox))

    symbols: dict[str, dict[str, Any]] = {}
    insert_items = []
    for ent in inserts:
        rec = _insert_record(ent)
        if not rec:
            continue
        name = rec["block"]
        key = rec["sym"]
        if key not in symbols:
            sym = build_symbol(doc, name)
            if sym:
                symbols[key] = sym
            else:
                # Unknown block: skip the instance rather than invent geometry.
                continue
        insert_items.append(rec)
    # Underlay / fixtures first, then loose geometry, then notes — covers later sit in a patch layer.
    items = insert_items + items

    counts = {
        "items": len(items),
        "inserts": len(insert_items),
        "texts": sum(1 for it in items if it.get("t") == "text"),
        "symbols": len(symbols),
        "paths": sum(1 for it in items if it.get("t") in {"path", "poly", "lines"}),
        "source_entities": len(ents),
    }
    return {
        "version": SCENE_VERSION,
        "bbox": [float(v) for v in bbox],
        "view_id": view_id,
        "pending": bool(pending),
        "stamp": stamp if pending else None,
        "background": PAPER,
        "symbols": symbols,
        "items": items,
        "counts": counts,
    }


def _cover_item(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    bb = result.get("bbox")
    if not bb or len(bb) < 4:
        return None
    pad = float(getattr(apply, "COVER_PAD_MM", 20.0) or 20.0)
    x0, y0, x1, y1 = (float(bb[0]) - pad, float(bb[1]) - pad, float(bb[2]) + pad, float(bb[3]) + pad)
    item: dict[str, Any] = {
        "t": "cover",
        "p": [_r(x0), _r(y0), _r(x1), _r(y0), _r(x1), _r(y1), _r(x0), _r(y1), _r(x0), _r(y0)],
        "bbox": [_r(x0), _r(y0), _r(x1), _r(y1)],
        "fill": 1,
        "c": PAPER,
    }
    if result.get("handle"):
        item["id"] = str(result["handle"])
    if result.get("block"):
        item["block"] = result["block"]
    return item


def _text_boxes(ent, matrix: Optional[Matrix44] = None, old_text: Optional[str] = None, edit: Optional[dict[str, Any]] = None):
    """改文字用的粗框：(旧字框, 新字框)，都以文字锚点为起点，按字高和字数估。

    旧字框优先用「改之前的可见字」(结果是 ``from``，以及保留换行的 ``requested``)：
    底图缓存里画的就是这句，长句（超过十几个字）也要整句盖住，否则旧字尾巴会露在
    新字旁边；没有就给个下界。旧字框只盖旧字，不与新字框合并（否则会盖掉新字）。
    """
    from . import dxftext

    try:
        height = float(getattr(ent.dxf, "height", 0) or getattr(ent.dxf, "char_height", 0) or 2.5)
    except Exception:
        height = 2.5
    try:
        rotation = math.radians(float(getattr(ent.dxf, "rotation", 0) or 0))
    except Exception:
        rotation = 0.0
    if matrix is not None:
        try:
            axis = matrix.transform_direction(Vec3(1, 0, 0))
            rotation += math.atan2(axis.y, axis.x)
            height *= math.hypot(axis.x, axis.y) or 1.0
        except Exception:
            pass
    pts = dxftext.text_points(ent)
    origin = dxftext.primary_anchor(ent) or (pts[0] if pts else None)
    if not origin:
        return None

    def _box(text: str) -> list[float]:
        lines = max(1, str(text or "").count("\n") + 1)
        longest = max((len(line) for line in str(text or "").split("\n")), default=1)
        width = max(height * 0.7 * max(longest, 1), height)
        depth = height * lines
        cos_r, sin_r = math.cos(rotation), math.sin(rotation)
        xs, ys = [], []
        for lx, ly in ((0.0, -height * 0.3), (width, -height * 0.3), (width, depth), (0.0, depth)):
            xs.append(origin[0] + lx * cos_r - ly * sin_r)
            ys.append(origin[1] + lx * sin_r + ly * cos_r)
        return [min(xs), min(ys), max(xs), max(ys)]

    def _union(a: list[float], b: list[float]) -> list[float]:
        return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]

    new_box = _box(dxftext.clean_entity_text(ent))
    old_box = _box((old_text if old_text is not None and str(old_text).strip() else "x" * 12))
    if edit:
        # 结果里的 from 把多行压成一行了；旧字原来有几段就按几段算（requested 保留了换行）。
        requested = edit.get("requested")
        if requested and str(requested).strip():
            old_box = _union(old_box, _box(str(requested)))
    return old_box, new_box


def _paper_cover(box: list[float], pad: float = 2.0) -> dict[str, Any]:
    x0, y0, x1, y1 = (float(v) for v in box)
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    return {
        "t": "cover",
        "p": [_r(x0), _r(y0), _r(x1), _r(y0), _r(x1), _r(y1), _r(x0), _r(y1), _r(x0), _r(y0)],
        "fill": 1,
        "c": PAPER,
        "transient": 1,
    }


def build_scene_patch(doc, results: list[dict[str, Any]], bbox) -> dict[str, Any]:
    """Incremental update: covers, new inserts, hides, and restriped neighbors."""
    hide = []
    covers = []
    insert_recs = []
    edit_items: list[dict[str, Any]] = []
    symbols: dict[str, dict[str, Any]] = {}
    for res in results or []:
        if not res.get("ok"):
            continue
        action = res.get("action")
        if action == "delete" and res.get("found", {}).get("kind") == "direct":
            ent = (res.get("found") or {}).get("entity")
            hid = _handle(ent) or res.get("handle")
            if hid:
                hide.append(str(hid))
        elif action == "cover":
            item = _cover_item(res)
            if item:
                covers.append(item)
        elif action == "insert":
            hid = res.get("handle")
            ent = None
            if hid:
                try:
                    ent = doc.entitydb.get(hid)
                except Exception:
                    ent = None
            rec = _insert_record(ent) if ent is not None else None
            if rec is None and res.get("block") and res.get("xy"):
                name = str(res["block"])
                rec = {
                    "t": "insert",
                    "block": name,
                    "sym": _sym_key(name),
                    "x": _r(float(res["xy"][0])),
                    "y": _r(float(res["xy"][1])),
                }
                if res.get("rot") is not None:
                    rec["rot"] = _r(float(res["rot"]), 3)
                if hid:
                    rec["id"] = str(hid)
            if rec:
                key = rec["sym"]
                if key not in symbols:
                    sym = build_symbol(doc, rec["block"])
                    if sym:
                        symbols[key] = sym
                if key in symbols:
                    insert_recs.append(rec)
        elif action == "text_edit":
            # 原地改文字：漫游底图是缓存的老图，旧字在那里；这里按 handle 让页面重画这一处，
            # 再叠上新字，并对旧字会露出来的地方遮一小片纸（临时层，不碰正式底图）。
            hid = res.get("handle")
            ent = None
            if hid:
                try:
                    ent = doc.entitydb.get(hid)
                except Exception:
                    ent = None
            if ent is not None and ent.dxftype() in TEXT_TYPES:
                matrix = None
                host = res.get("host_handle")
                if host:
                    try:
                        host_ent = doc.entitydb.get(host)
                        matrix = host_ent.matrix44() if host_ent is not None else None
                    except Exception:
                        matrix = None
                old_box = _text_boxes(ent, matrix, res.get("from"), res)
                item = _text_payload(ent, matrix=matrix)
                if item:
                    item["c"] = INK
                    edit_items.append(item)
                    if old_box:
                        lb, _nb = old_box
                        edit_items.insert(0, _paper_cover(lb))
                    hide.append(str(hid))
        elif action == "move":
            # Old location is already mutated; restripe the new insert via handle if present.
            hid = res.get("handle")
            if hid:
                try:
                    ent = doc.entitydb.get(hid)
                except Exception:
                    ent = None
                rec = _insert_record(ent) if ent is not None else None
                if rec:
                    insert_recs.append(rec)

    overlay_items: list[dict[str, Any]] = list(edit_items)
    try:
        overlay_ents = apply.plan_overlay_entities(doc, results)
    except Exception:
        log.exception("plan overlay entities for scene failed")
        overlay_ents = None
    if overlay_ents:
        cover_handles = {c.get("id") for c in covers if c.get("id")}
        insert_handles = {r.get("id") for r in insert_recs if r.get("id")}
        rest = []
        for ent in overlay_ents:
            hid = _handle(ent)
            kind = ent.dxftype()
            if hid and hid in cover_handles:
                continue
            if hid and hid in insert_handles:
                continue
            if kind == "INSERT":
                rec = _insert_record(ent)
                if rec:
                    key = rec["sym"]
                    if key not in symbols:
                        sym = build_symbol(doc, rec["block"])
                        if sym:
                            symbols[key] = sym
                    if key in symbols:
                        # Nearby existing inserts are restriped above covers,
                        # not counted as new plan insertions.
                        overlay_items.append(rec)
                continue
            if kind in TEXT_TYPES:
                overlay_items.extend(_collect_texts(doc, [ent], bbox, margin=MARGIN * 2))
                continue
            rest.append(ent)
        overlay_items.extend(_draw_entities(doc, rest))

    dirty = apply.dirty_bbox_from_results(results, view_bbox=bbox)
    return {
        "hide": hide,
        "covers": covers,
        "symbols": symbols,
        "inserts": insert_recs,
        "items": overlay_items,
        "dirty_bbox": [float(v) for v in dirty] if dirty else None,
        "ok_count": sum(1 for r in results if r.get("ok")),
        "failed_count": sum(1 for r in results if not r.get("ok")),
        "actions": {
            "cover": sum(1 for r in results if r.get("ok") and r.get("action") == "cover"),
            "insert": sum(1 for r in results if r.get("ok") and r.get("action") == "insert"),
            "delete": sum(1 for r in results if r.get("ok") and r.get("action") == "delete"),
            "move": sum(1 for r in results if r.get("ok") and r.get("action") == "move"),
            "text_edit": sum(1 for r in results if r.get("ok") and r.get("action") == "text_edit"),
        },
    }


def apply_plan_to_doc(proj: dict[str, Any], plan: dict[str, Any], region: dict[str, Any]):
    """In-memory apply only. Does not write history or the work drawing."""
    src = apply.current_dxf(proj)
    encoding = proj.get("encoding") or "utf-8"
    doc = _read_doc(src, encoding)
    next_rev = apply.next_revision_number(proj)
    layer = f"{apply.NEW_LAYER_PREFIX}-{next_rev}"
    bbox = (region or {}).get("bbox")
    results = apply.apply_mutations(doc, plan, bbox=bbox, layer=layer)
    ok_n = sum(1 for r in results if r.get("ok"))
    if ok_n == 0:
        reasons = [r.get("reason") for r in results if r.get("reason")]
        raise ValueError(reasons[0] if reasons else "方案里没有能预览的改动。")
    return doc, results, ok_n


def preview_plan_scene(proj: dict[str, Any], plan: dict[str, Any], region: dict[str, Any]) -> dict[str, Any]:
    doc, results, ok_n = apply_plan_to_doc(proj, plan, region)
    bbox = (region or {}).get("bbox")
    patch = build_scene_patch(doc, results, bbox)
    scene = build_region_scene(
        doc,
        bbox,
        view_id=(region or {}).get("id"),
        pending=True,
        stamp=apply.STAMP_PREVIEW,
    )
    scene["patch"] = patch
    scene["ok_count"] = ok_n
    scene["failed_count"] = len(results) - ok_n
    return scene



# 取景比例已统一到 placement.context_bbox，这里只留旧名兼容。


def insert_world_bbox(rec: dict[str, Any], symbols: Optional[dict[str, Any]] = None) -> Optional[list[float]]:
    """Axis-aligned extents of an insert (or cover) in world coordinates."""
    if not rec:
        return None
    if rec.get("bbox") and len(rec.get("bbox") or []) == 4 and rec.get("t") in {"cover", "poly"}:
        return [float(v) for v in rec["bbox"]]
    x = rec.get("x")
    y = rec.get("y")
    if x is None or y is None:
        xy = rec.get("xy")
        if not xy or len(xy) < 2:
            return None
        x, y = xy[0], xy[1]
    try:
        x = float(x)
        y = float(y)
    except (TypeError, ValueError):
        return None
    sx = 1.0
    sy = 1.0
    scale = rec.get("scale")
    if isinstance(scale, (list, tuple)) and scale:
        try:
            sx = float(scale[0])
            sy = float(scale[1]) if len(scale) > 1 else sx
        except (TypeError, ValueError):
            pass
    if rec.get("sx") is not None:
        try:
            sx = float(rec["sx"])
        except (TypeError, ValueError):
            pass
    if rec.get("sy") is not None:
        try:
            sy = float(rec["sy"])
        except (TypeError, ValueError):
            pass
    try:
        rot = float(rec.get("rot") or 0.0)
    except (TypeError, ValueError):
        rot = 0.0
    symbols = symbols or {}
    key = rec.get("sym") or _sym_key(str(rec.get("block") or ""))
    sym = symbols.get(key) or {}
    bb = sym.get("bbox")
    if bb and len(bb) == 4:
        corners = (
            (float(bb[0]) * sx, float(bb[1]) * sy),
            (float(bb[2]) * sx, float(bb[1]) * sy),
            (float(bb[2]) * sx, float(bb[3]) * sy),
            (float(bb[0]) * sx, float(bb[3]) * sy),
        )
        rad = math.radians(rot)
        c, s = math.cos(rad), math.sin(rad)
        xs, ys = [], []
        for lx, ly in corners:
            xs.append(x + lx * c - ly * s)
            ys.append(y + lx * s + ly * c)
        return [min(xs), min(ys), max(xs), max(ys)]
    ext = 800.0
    return [x - ext, y - ext, x + ext, y + ext]


def fit_bbox_for_patch(patch: Optional[dict[str, Any]], scene: Optional[dict[str, Any]] = None) -> Optional[list[float]]:
    """Suggested camera box: dirty region plus new insert extents, with modest padding."""
    boxes: list[list[float]] = []
    if patch and patch.get("dirty_bbox") and len(patch["dirty_bbox"]) == 4:
        boxes.append([float(v) for v in patch["dirty_bbox"]])
    symbols: dict[str, Any] = {}
    if scene:
        symbols.update(scene.get("symbols") or {})
    if patch:
        symbols.update(patch.get("symbols") or {})
        for rec in list(patch.get("inserts") or []) + list(patch.get("covers") or []):
            if rec.get("bbox") and len(rec.get("bbox") or []) == 4:
                boxes.append([float(v) for v in rec["bbox"]])
            else:
                bb = insert_world_bbox(rec, symbols)
                if bb:
                    boxes.append(bb)
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    # 视野不能只框住改动本身：那样屏幕上只剩两三个符号，看不出这张图原来长什么样。
    # 交给 placement.context_bbox 统一规则，和位图预览保持同一取景。
    view = None
    raw_view = (scene or {}).get("bbox")
    if isinstance(raw_view, (list, tuple)) and len(raw_view) == 4:
        view = [float(v) for v in raw_view]
    return placement.context_bbox([x0, y0, x1, y1], view)


def page_display_from_event(previous: Optional[dict[str, Any]], ev: dict[str, Any]) -> dict[str, Any]:
    """What the workbench should show after a scene / scene_patch SSE event.

    Prefer the full pending `ev.scene` over stacking a patch on a possibly stale view.
    """
    ev = ev or {}
    scene = ev.get("scene") if isinstance(ev.get("scene"), dict) else None
    patch = ev.get("patch") if isinstance(ev.get("patch"), dict) else {}
    if scene is not None and scene.get("items") is not None:
        displayed = scene
    else:
        displayed = previous
    fit = ev.get("fit_bbox") or fit_bbox_for_patch(patch, displayed or scene)
    return {
        "scene": displayed,
        "fit_bbox": fit,
        "pending": bool(ev.get("pending") if ev.get("pending") is not None else (displayed or {}).get("pending")),
        "stamp": ev.get("stamp") or (displayed or {}).get("stamp"),
    }


def preview_plan_event(
    proj: dict[str, Any],
    plan: dict[str, Any],
    region: dict[str, Any],
    message_id: Optional[str] = None,
) -> dict[str, Any]:
    """SSE payload: patch plus a compact full scene the page can also swap in."""
    packed = preview_plan_scene(proj, plan, region)
    ev = {
        "type": "scene_patch",
        "message_id": message_id,
        "pending": True,
        "stamp": apply.STAMP_PREVIEW,
        "view_id": (region or {}).get("id"),
        "patch": packed.get("patch"),
        "scene": packed,
        "ok_count": packed.get("ok_count"),
    }
    # Drop the nested copy of the patch inside `scene` for the wire payload.
    scene = dict(packed)
    scene.pop("patch", None)
    ev["scene"] = scene
    fit = fit_bbox_for_patch(ev.get("patch"), scene)
    ev["fit_bbox"] = fit
    if ev.get("patch") is not None and fit:
        patch = dict(ev["patch"])
        patch["fit_bbox"] = fit
        ev["patch"] = patch
    return ev


def _cache_key(path: Path, bbox, source: str, view_id: Optional[str]) -> str:
    st = Path(path).stat()
    raw = f"{Path(path).resolve()}|{st.st_mtime_ns}|{st.st_size}|{source}|{view_id}|{tuple(round(float(x), 3) for x in bbox)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _path_token(path: Path) -> str:
    return hashlib.sha1(str(Path(path).resolve()).encode("utf-8")).hexdigest()[:12]


def scene_disk_path(path: Path, bbox, source: str, view_id: Optional[str]) -> Path:
    """Content-addressed scene file for this drawing + view."""
    key = _cache_key(Path(path), bbox, source, view_id)
    return Path(config.SCENE_DIR) / f"scene-{_path_token(path)}-{key}.json"


def _clone(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(data))


def _write_scene_disk(dest: Path, scene: dict[str, Any]) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(scene, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(dest)
    return dest


def _read_scene_disk(dest: Path) -> Optional[dict[str, Any]]:
    if not dest.is_file() or dest.stat().st_size <= 2:
        return None
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except Exception:
        log.exception("could not read scene cache %s", dest)
        return None
    if not isinstance(data, dict) or data.get("items") is None:
        return None
    return data


def _store_memory(key: str, st, scene: dict[str, Any]) -> None:
    with _SCENE_LOCK:
        _SCENE_CACHE[key] = (st.st_mtime, st.st_size, scene)


def cached_scene_exists(path: Path, bbox, source: str, view_id: Optional[str]) -> bool:
    dest = scene_disk_path(path, bbox, source, view_id)
    return dest.is_file() and dest.stat().st_size > 2


def drop_scenes_for_dxf(path: Path) -> int:
    """Drop memory + disk scenes for this drawing (call after the work file changes)."""
    drop_scene_cache()
    try:
        token = _path_token(Path(path))
    except Exception:
        return 0
    root = Path(config.SCENE_DIR)
    if not root.is_dir():
        return 0
    n = 0
    for item in root.glob(f"scene-{token}-*.json"):
        try:
            item.unlink()
            n += 1
        except Exception:
            pass
    for item in root.glob(f"scene-{token}-*.json.tmp"):
        try:
            item.unlink()
        except Exception:
            pass
    return n


def persist_region_scene(
    proj: dict[str, Any],
    region: dict[str, Any],
    *,
    source: str = "work",
    force: bool = False,
) -> dict[str, Any]:
    """Build a region scene if needed and write it to disk."""
    return build_project_scene(proj, region, source=source, pending=False, force=force)


def build_project_scene(
    proj: dict[str, Any],
    region: dict[str, Any],
    *,
    source: Optional[str] = None,
    pending: bool = False,
    plan: Optional[dict[str, Any]] = None,
    force: bool = False,
) -> dict[str, Any]:
    bbox = (region or {}).get("bbox")
    view_id = (region or {}).get("id")
    official = (source or "").strip().lower() in {"official", "source", "original"}
    kind = "official" if official else "work"
    if pending and plan:
        return preview_plan_scene(proj, plan, region)
    if pending and not official:
        last = apply.last_pending_plan(proj)
        if last and last.get("plan"):
            pregion = last.get("region")
            if not pregion or pregion == view_id:
                return preview_plan_scene(proj, last["plan"], region)
    path = apply.original_preview_dxf(proj) if official else apply.current_dxf(proj)
    encoding = proj.get("encoding") or "utf-8"
    path = Path(path)
    key = _cache_key(path, bbox, kind, view_id)
    st = path.stat()
    if not force:
        with _SCENE_LOCK:
            hit = _SCENE_CACHE.get(key)
            if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
                return _clone(hit[2])
        disk = _read_scene_disk(scene_disk_path(path, bbox, kind, view_id))
        if disk is not None:
            _store_memory(key, st, disk)
            return _clone(disk)
    doc = _load_readonly_doc(path, encoding)
    packed = build_region_scene(doc, bbox, view_id=view_id, pending=False, stamp=None)
    try:
        _write_scene_disk(scene_disk_path(path, bbox, kind, view_id), packed)
    except Exception:
        log.exception("could not persist scene for %s %s", path, view_id)
    _store_memory(key, st, packed)
    return _clone(packed)


def drop_scene_cache() -> None:
    with _SCENE_LOCK:
        _SCENE_CACHE.clear()
