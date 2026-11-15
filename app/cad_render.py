from __future__ import annotations

import hashlib
import json
import math
import threading
from pathlib import Path
from typing import Any, Optional

import ezdxf
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.config import (
    BackgroundPolicy,
    ColorPolicy,
    Configuration,
    HatchPolicy,
)
from ezdxf.addons.drawing.matplotlib import SCATTER_POINT_SIZE, MatplotlibBackend
from ezdxf.npshapes import to_matplotlib_path
from matplotlib import font_manager
from matplotlib.collections import LineCollection
from matplotlib.patches import PathPatch
from PIL import Image, ImageDraw, ImageFont

from . import config

_DOC_LOCK = threading.Lock()
_MPL_LOCK = threading.Lock()
_DOC_USE_LOCKS: dict[str, threading.Lock] = {}
_FP_LOCK = threading.Lock()
_DOCS: dict[str, tuple[float, Any]] = {}
_ENTITY_INDEX: dict[str, list[tuple[tuple[float, float, float, float], Any]]] = {}

if Path(config.CJK_FONT).exists():
    try:
        font_manager.fontManager.addfont(config.CJK_FONT)
    except Exception:
        pass

PAD = 0.012
PAPER_COLOR = "#FFFFFF"
# AutoCAD box_fill_scale is typically 1.0–2.0. LibreDWG often writes garbage
# like 2576980378 into DIMENSION anonymous-block MTEXT, which ezdxf then paints
# as a canvas-colored mask covering the whole preview.
_MIN_BG_FILL_SCALE = 0.9
_MAX_BG_FILL_SCALE = 8.0
_DEFAULT_BG_FILL_SCALE = 1.5


def sanitize_text_background(entity) -> bool:
    """Clamp corrupt MTEXT/ATTRIB background fields. Returns True if changed."""
    try:
        t = entity.dxftype()
    except Exception:
        return False
    if t not in {"MTEXT", "ATTRIB", "ATTDEF"}:
        return False
    changed = False
    d = entity.dxf
    if d.hasattr("box_fill_scale"):
        try:
            scale = float(d.box_fill_scale)
        except Exception:
            scale = None
        if scale is None or not math.isfinite(scale) or scale < _MIN_BG_FILL_SCALE or scale > _MAX_BG_FILL_SCALE:
            d.box_fill_scale = _DEFAULT_BG_FILL_SCALE
            changed = True
    if d.hasattr("bg_fill_true_color"):
        try:
            tc = int(d.bg_fill_true_color)
        except Exception:
            tc = -1
        # True color is 0x00RRGGBB; values above 24-bit are conversion junk.
        if tc < 0 or tc > 0xFFFFFF:
            d.discard("bg_fill_true_color")
            changed = True
    if d.hasattr("bg_fill_transparency"):
        try:
            tr = int(d.bg_fill_transparency)
        except Exception:
            tr = -1
        if tr < 0 or tr > 0x01000000:
            d.discard("bg_fill_transparency")
            changed = True
    return changed


def sanitize_doc_text_backgrounds(doc) -> int:
    """Fix corrupt text-background fields in every layout and block."""
    n = 0
    try:
        layouts = list(doc.layouts)
    except Exception:
        layouts = []
    blocks = []
    try:
        blocks = list(doc.blocks)
    except Exception:
        pass
    seen = set()
    for container in list(layouts) + list(blocks):
        try:
            ents = list(container)
        except Exception:
            continue
        for e in ents:
            try:
                hid = e.dxf.handle if e.dxf.hasattr("handle") else id(e)
            except Exception:
                hid = id(e)
            if hid in seen:
                continue
            seen.add(hid)
            if sanitize_text_background(e):
                n += 1
    return n


def entity_points(e):
    pts = []
    d = e.dxf
    for attr in ("insert", "start", "end", "center", "align_point", "text_midpoint"):
        try:
            if d.hasattr(attr):
                p = getattr(d, attr)
                pts.append((float(p.x), float(p.y)))
        except Exception:
            pass
    t = e.dxftype()
    try:
        if t == "LWPOLYLINE":
            pts.extend((float(p[0]), float(p[1])) for p in e.get_points("xy"))
        elif t == "POLYLINE":
            pts.extend((float(v.dxf.location.x), float(v.dxf.location.y)) for v in e.vertices)
        elif t == "SPLINE":
            pts.extend((float(p.x), float(p.y)) for p in e.control_points)
        elif t == "HATCH":
            for p in e.paths:
                try:
                    for v in getattr(p, "vertices", []):
                        if hasattr(v, "x"):
                            pts.append((float(v.x), float(v.y)))
                        elif isinstance(v, (list, tuple)):
                            pts.append((float(v[0]), float(v[1])))
                except Exception:
                    pass
        elif t == "DIMENSION":
            for attr in ("defpoint", "defpoint2", "defpoint3", "text_midpoint"):
                try:
                    if d.hasattr(attr):
                        p = getattr(d, attr)
                        pts.append((float(p.x), float(p.y)))
                except Exception:
                    pass
        elif t in {"ARC", "CIRCLE"}:
            try:
                r = float(d.radius)
                c = d.center
                pts.extend([(c.x + r, c.y), (c.x - r, c.y), (c.x, c.y + r), (c.x, c.y - r)])
            except Exception:
                pass
        elif t in {"SOLID", "TRACE", "3DFACE"}:
            for attr in ("vtx0", "vtx1", "vtx2", "vtx3"):
                try:
                    if d.hasattr(attr):
                        v = getattr(d, attr)
                        pts.append((float(v.x), float(v.y)))
                except Exception:
                    pass
        elif t == "WIPEOUT":
            try:
                for v in e.boundary_path_wcs():
                    pts.append((float(v.x), float(v.y)))
            except Exception:
                try:
                    for v in e.boundary_path:
                        if hasattr(v, "x"):
                            pts.append((float(v.x), float(v.y)))
                        elif isinstance(v, (list, tuple)):
                            pts.append((float(v[0]), float(v[1])))
                except Exception:
                    pass
    except Exception:
        pass
    return pts


def _entity_bbox(e):
    pts = entity_points(e)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def build_entity_index(doc, key: str):
    items = []
    for e in doc.modelspace():
        bb = _entity_bbox(e)
        if bb:
            items.append((bb, e))
    _ENTITY_INDEX[key] = items
    return len(items)


def filter_layout(layout, bbox, margin=8000, doc_key: Optional[str] = None):
    xmin, ymin, xmax, ymax = bbox
    xmin -= margin
    ymin -= margin
    xmax += margin
    ymax += margin
    indexed = _ENTITY_INDEX.get(doc_key) if doc_key else None
    if indexed is not None:
        out = []
        for (ex0, ey0, ex1, ey1), e in indexed:
            if ex1 < xmin or ex0 > xmax or ey1 < ymin or ey0 > ymax:
                continue
            out.append(e)
        return out
    out = []
    for e in layout:
        bb = _entity_bbox(e)
        if not bb:
            continue
        ex0, ey0, ex1, ey1 = bb
        if ex1 < xmin or ex0 > xmax or ey1 < ymin or ey0 > ymax:
            continue
        out.append(e)
    return out


class _BatchedBackend(MatplotlibBackend):
    """把相邻的同类线合并成少量 artist。

    ezdxf 的 mpl 后端每次 draw_solid_lines / draw_path 都新建一个 LineCollection
    或 PathPatch：一张图几千条线就是几千个 artist，单个预览实测十几秒，而耗时
    几乎全在 matplotlib 建 artist、更新 datalim 上，跟图形复杂度无关。这里按
    「颜色 + 线宽」攒起来一次画完；遇到别的图元类型（文字、填充、图片）就先
    把攒的刷出去，所以绘制先后顺序与原实现一致，遮盖关系不会变。

    只合并描边，不合并填充：填充要 detect_holes，跨实体合并会让相邻或重叠的
    两个填充互相当成内孔，颜色会串。
    """

    _MAX_SEGMENTS = 3000

    def __init__(self, ax, **kwargs):
        super().__init__(ax, **kwargs)
        self._lines: dict[tuple, list] = {}
        self._paths: dict[tuple, list] = {}
        self._points: dict[tuple, list] = {}
        self._order: list[tuple] = []
        self._pending = 0

    # -- 攒与刷 ---------------------------------------------------------
    def _key(self, properties) -> tuple:
        try:
            lw = round(float(self.get_lineweight(properties)), 3)
        except Exception:
            lw = 1.0
        return (str(properties.color), lw)

    def _bucket(self, store: dict, key: tuple) -> list:
        buf = store.get(key)
        if buf is None:
            buf = []
            store[key] = buf
            if key not in self._order:
                self._order.append(key)
        return buf

    def _flush(self) -> None:
        if not self._order:
            return
        for key in self._order:
            points = self._points.pop(key, None)
            lines = self._lines.pop(key, None)
            paths = self._paths.pop(key, None)
            color, lw = key
            if points:
                self.ax.scatter(
                    [p[0] for p in points],
                    [p[1] for p in points],
                    s=SCATTER_POINT_SIZE,
                    c=color,
                    zorder=self._get_z(),
                )
            if lines:
                self.ax.add_collection(
                    LineCollection(
                        lines,
                        linewidths=lw,
                        color=color,
                        zorder=self._get_z(),
                        capstyle="butt",
                    )
                )
            for path in paths or []:
                try:
                    patch = PathPatch(
                        to_matplotlib_path([path]),
                        linewidth=lw,
                        fill=False,
                        color=color,
                        zorder=self._get_z(),
                    )
                except ValueError:
                    continue
                self.ax.add_patch(patch)
        self._order = []
        self._pending = 0

    # -- 描边：攒 -------------------------------------------------------
    def draw_solid_lines(self, lines, properties):
        key = self._key(properties)
        segs = self._bucket(self._lines, key)
        pts = self._bucket(self._points, key)
        for s, e in lines:
            if s.isclose(e):
                pts.append((s.x, s.y))
            else:
                segs.append(((s.x, s.y), (e.x, e.y)))
            self._pending += 1
        if self._pending >= self._MAX_SEGMENTS:
            self._flush()

    def draw_line(self, start, end, properties):
        self.draw_solid_lines([(start, end)], properties)

    def draw_path(self, path, properties):
        self._bucket(self._paths, self._key(properties)).append(path)
        self._pending += 1
        if self._pending >= self._MAX_SEGMENTS:
            self._flush()

    def draw_point(self, pos, properties):
        self._bucket(self._points, self._key(properties)).append((pos.x, pos.y))
        self._pending += 1

    # -- 其余图元：先刷再画，保持顺序 ------------------------------------
    def draw_filled_paths(self, paths, properties):
        self._flush()
        super().draw_filled_paths(paths, properties)

    def draw_filled_polygon(self, points, properties):
        self._flush()
        super().draw_filled_polygon(points, properties)

    def draw_text(self, *args, **kwargs):
        self._flush()
        return super().draw_text(*args, **kwargs)

    def draw_image(self, *args, **kwargs):
        self._flush()
        return super().draw_image(*args, **kwargs)

    def finalize(self) -> None:
        self._flush()
        return super().finalize()


def _pin_axes_limits(ax) -> None:
    """预览的 xlim/ylim 是我们自己钉死的，别让每个 artist 去更新 datalim。

    实测这一步能省掉 matplotlib 在 add_collection / add_patch 里做的
    _update_collection_limits 与 _update_patch_limits，占单次预览的两成。
    """
    try:
        ax.set_autoscalex_on(False)
        ax.set_autoscaley_on(False)
        ax._update_collection_limits = lambda *a, **k: None
        ax._update_patch_limits = lambda *a, **k: None
    except Exception:
        pass


class _PaperWipeoutFrontend(Frontend):
    """WIPEOUT must fill with the preview paper color, not the modelspace dark background."""

    def draw_wipeout_entity(self, entity, properties) -> None:
        from ezdxf.addons.drawing.properties import Filling

        properties.filling = Filling()
        properties.color = PAPER_COLOR
        clipping_polygon = entity.boundary_path_wcs()
        self.pipeline.draw_filled_polygon(clipping_polygon, properties)

    def draw_mtext_entity(self, entity, properties) -> None:
        sanitize_text_background(entity)
        return super().draw_mtext_entity(entity, properties)

    def draw_text_entity(self, entity, properties) -> None:
        sanitize_text_background(entity)
        return super().draw_text_entity(entity, properties)


def _guard_huge_fills(frontend, bbox) -> None:
    """Skip filled polygons that would blanket the view (corrupt MTEXT masks)."""
    try:
        view_span = max(float(bbox[2]) - float(bbox[0]), float(bbox[3]) - float(bbox[1]), 1.0)
    except Exception:
        return
    limit = view_span * 20.0
    pipe = getattr(frontend, "pipeline", None)
    if pipe is None or not hasattr(pipe, "draw_filled_polygon"):
        return
    orig = pipe.draw_filled_polygon

    def _span(points):
        xs, ys = [], []
        for p in points:
            try:
                xs.append(float(p.x if hasattr(p, "x") else p[0]))
                ys.append(float(p.y if hasattr(p, "y") else p[1]))
            except Exception:
                continue
        if not xs:
            return 0.0
        return max(max(xs) - min(xs), max(ys) - min(ys))

    def guarded(points, properties):
        try:
            if _span(points) > limit:
                return
        except Exception:
            pass
        return orig(points, properties)

    pipe.draw_filled_polygon = guarded


# 批量绘制开关：出问题时可以整体关掉回到 ezdxf 原生后端。
BATCH_LINES = True

# 以下三个都按「输出分辨率」而不是按图面尺寸来定细节，因为预览是给人看
# 「这张图长什么样」的：一个 40px 的洁具符号，128 段和 32 段在屏幕上没有区别。
# 图里块内实体带虚线样式，一条 700mm 的线会被拆成 280 段；弧按 128 段展平，
# 40 个弧乘 10 个实例就是几十万段，全烧在建 matplotlib Path 上。
PREVIEW_ARC_SEGMENTS = 32
DASH_MIN_PX = 1.2          # 虚线段短于这个像素数就按实线画
FLATTEN_MAX_PX = 0.35      # 曲线展平误差不超过这个像素数


def _pixel_limits(world_span: float, pixel_span: float) -> tuple[float, float]:
    """把「多少个像素」换算成图面单位，得到最少虚线段长与最大展平距离。"""
    try:
        per_px = abs(float(world_span)) / max(abs(float(pixel_span)), 1.0)
    except Exception:
        per_px = 1.0
    if not per_px or per_px != per_px:  # 0 或 nan
        per_px = 1.0
    min_dash = per_px * DASH_MIN_PX
    flatten = min(max(per_px * FLATTEN_MAX_PX, 0.02), 50.0)
    return min_dash, flatten


def make_frontend(
    doc,
    ax,
    hatch=HatchPolicy.SHOW_SOLID,
    *,
    batch: Optional[bool] = None,
    min_dash: float = 0.05,
    flatten: float = 0.01,
):
    ctx = RenderContext(doc)
    cfg = Configuration(
        background_policy=BackgroundPolicy.CUSTOM,
        custom_bg_color=PAPER_COLOR,
        color_policy=ColorPolicy.COLOR,
        lineweight_scaling=1.0,
        min_lineweight=0.12,
        hatching_timeout=1.5,
        hatch_policy=hatch,
        circle_approximation_count=PREVIEW_ARC_SEGMENTS,
        max_flattening_distance=flatten,
        min_dash_length=min_dash,
    )
    # Pin our own figure size and xlim/ylim; ezdxf finalize() would autoscale and resize.
    _pin_axes_limits(ax)
    use_batch = BATCH_LINES if batch is None else bool(batch)
    backend = (_BatchedBackend if use_batch else MatplotlibBackend)(ax, adjust_figure=False)
    return _PaperWipeoutFrontend(ctx, backend, config=cfg)


def render_region(doc, layout, bbox, out_path: Path, *, dpi=130, pad=PAD, margin=4000,
                  hatch=HatchPolicy.SHOW_SOLID, xlim=None, ylim=None, fig_wh=None,
                  entities=None, transparent: bool = False, batch: Optional[bool] = None):
    """Render a crop of model space. Does not write back to the DXF.

    xlim/ylim/fig_wh pin the world-to-pixel transform so a dirty patch can be
    pasted onto a cached full-view image without resampling the sheet.
    """
    xmin, ymin, xmax, ymax = bbox
    w, h = max(xmax - xmin, 1.0), max(ymax - ymin, 1.0)
    if fig_wh:
        fw, fh = float(fig_wh[0]), float(fig_wh[1])
    else:
        long = 16.0
        if w >= h:
            fw, fh = long, max(long * h / w, 1.0)
        else:
            fh, fw = long, max(long * w / h, 1.0)
    bg = "none" if transparent else "white"
    long_side_px = max(fw, fh) * float(dpi)
    fig = plt.figure(figsize=(fw, fh), dpi=dpi, facecolor=bg)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(bg)
    ax.set_aspect("equal")
    ax.axis("off")
    if entities is not None:
        ents = list(entities)
    else:
        ents = filter_layout(layout, bbox, margin=margin, doc_key=getattr(doc, '_tutan_key', None))
    try:
        mapping = list(layout.get_redraw_order()) if hasattr(layout, "get_redraw_order") else []
        if mapping:
            from ezdxf import reorder
            ents = list(reorder.ascending(ents, mapping))
    except Exception:
        pass
    # 图面单位 / 像素：按这个限制虚线密度与曲线展平误差
    min_dash, flatten = _pixel_limits(max(w, h), long_side_px)
    fe = make_frontend(
        doc, ax, hatch=hatch, batch=batch, min_dash=min_dash, flatten=flatten
    )
    try:
        fe.ctx.set_current_layout(layout)
    except Exception:
        pass
    try:
        fe.set_background(fe.config.custom_bg_color)
    except Exception:
        pass
    _guard_huge_fills(fe, (xmin, ymin, xmax, ymax))
    try:
        fe.draw_entities(ents)
    except Exception:
        pass
    # 攒下的图元必须在 savefig 之前落地。不走 pipeline.finalize()，它会
    # autoscale 并改 figure 尺寸，破坏钉死的世界坐标→像素映射。
    try:
        fe.pipeline.backend.finalize()
    except Exception:
        pass
    # Do not call pipeline.finalize(): MatplotlibBackend.finalize() autoscales and
    # resizes the figure, which breaks the pinned world-to-pixel preview transform.
    if xlim is None or ylim is None:
        px, py = w * pad, h * pad
        xlim = (xmin - px, xmax + px)
        ylim = (ymin - py, ymax + py)
    ax.set_xlim(xlim[0], xlim[1])
    ax.set_ylim(ylim[0], ylim[1])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        out_path,
        dpi=dpi,
        facecolor="none" if transparent else "white",
        edgecolor="none",
        pad_inches=0,
        transparent=transparent,
    )
    plt.close(fig)
    return out_path


def _doc_use_lock(dxf_path: Path) -> threading.Lock:
    """Per-drawing lock so two threads do not mutate/render the same cached doc."""
    try:
        key = str(Path(dxf_path).resolve())
    except Exception:
        key = str(dxf_path)
    with _DOC_LOCK:
        lock = _DOC_USE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DOC_USE_LOCKS[key] = lock
        return lock


def load_doc(dxf_path: Path, encoding: str = "utf-8"):
    key = str(dxf_path.resolve())
    mtime = dxf_path.stat().st_mtime
    with _DOC_LOCK:
        hit = _DOCS.get(key)
        if hit and hit[0] == mtime:
            doc = hit[1]
            doc._tutan_key = key
            return doc
    doc = ezdxf.readfile(str(dxf_path), encoding=encoding)
    build_entity_index(doc, key)
    doc._tutan_key = key
    with _DOC_LOCK:
        _DOCS[key] = (mtime, doc)
    return doc


def drop_doc(dxf_path: Path) -> None:
    try:
        key = str(Path(dxf_path).resolve())
    except Exception:
        return
    with _DOC_LOCK:
        _DOCS.pop(key, None)
        _ENTITY_INDEX.pop(key, None)


# Fingerprint by content so the same drawing shared across sessions can reuse previews.
_FP_CACHE: dict[tuple, str] = {}


def _content_fp(dxf_path: Path) -> str:
    st = dxf_path.stat()
    token = (str(dxf_path.resolve()), st.st_mtime_ns, st.st_size)
    with _FP_LOCK:
        hit = _FP_CACHE.get(token)
        if hit:
            return hit
    h = hashlib.sha256()
    h.update(str(st.st_size).encode())
    with dxf_path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    digest = h.hexdigest()[:16]
    with _FP_LOCK:
        if len(_FP_CACHE) > 256:
            _FP_CACHE.clear()
        _FP_CACHE[token] = digest
    return digest


def _cache_key(dxf_path: Path, bbox, dpi: int) -> str:
    raw = f"{_content_fp(dxf_path)}|{tuple(round(float(x),3) for x in bbox)}|{dpi}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def choose_dpi(bbox, dpi: Optional[int] = None) -> int:
    if dpi:
        return int(dpi)
    w = max(float(bbox[2]) - float(bbox[0]), 1)
    if w > 80000:
        return 80
    if w > 40000:
        return 100
    return 130


def cached_preview_exists(dxf_path: Path, bbox: list[float], *, dpi: Optional[int] = None) -> bool:
    dest = base_cache_path(dxf_path, bbox, dpi=dpi)
    return dest.is_file() and dest.stat().st_size > 0


def base_preview(dxf_path: Path, bbox: list[float], *, encoding="utf-8", dpi=None) -> Path:
    dpi = choose_dpi(bbox, dpi)
    dest = base_cache_path(dxf_path, bbox, dpi=dpi)
    if dest.is_file() and dest.stat().st_size > 0:
        return dest
    tmp = dest.with_suffix(".tmp.png")
    w = max(bbox[2] - bbox[0], 1)
    # tighter filter margin for small rooms, looser for whole sheets
    margin = 2000 if w > 40000 else 800
    hatch = HatchPolicy.SHOW_OUTLINE if w > 80000 else HatchPolicy.SHOW_SOLID
    with _doc_use_lock(dxf_path):
        if dest.is_file() and dest.stat().st_size > 0:
            return dest
        doc = load_doc(dxf_path, encoding=encoding)
        with _MPL_LOCK:
            render_region(doc, doc.modelspace(), bbox, tmp, dpi=dpi, margin=margin, hatch=hatch)
        tmp.replace(dest)
        return dest


def world_limits(bbox, pad=PAD):
    xmin, ymin, xmax, ymax = (float(v) for v in bbox)
    w, h = max(xmax - xmin, 1.0), max(ymax - ymin, 1.0)
    return (xmin - w * pad, xmax + w * pad, ymin - h * pad, ymax + h * pad)


def world_to_pixel(x, y, bbox, img_w, img_h, pad=PAD):
    xlim0, xlim1, ylim0, ylim1 = world_limits(bbox, pad)
    px = (float(x) - xlim0) / (xlim1 - xlim0) * img_w
    py = (ylim1 - float(y)) / (ylim1 - ylim0) * img_h
    return px, py


def pixel_to_world(px, py, bbox, img_w, img_h, pad=PAD):
    xlim0, xlim1, ylim0, ylim1 = world_limits(bbox, pad)
    x = xlim0 + (float(px) / img_w) * (xlim1 - xlim0)
    y = ylim1 - (float(py) / img_h) * (ylim1 - ylim0)
    return x, y


def base_cache_path(dxf_path: Path, bbox: list[float], *, dpi=None) -> Path:
    dpi = choose_dpi(bbox, dpi)
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return config.CACHE_DIR / f"base-{_cache_key(Path(dxf_path), bbox, dpi)}.png"


def base_cache_info(dxf_path: Path, bbox: list[float], *, dpi=None) -> Optional[dict[str, Any]]:
    """Return cached full-view metadata if a non-empty PNG already exists."""
    dpi = choose_dpi(bbox, dpi)
    dest = base_cache_path(dxf_path, bbox, dpi=dpi)
    if not (dest.is_file() and dest.stat().st_size > 0):
        return None
    with Image.open(dest) as im:
        width, height = im.size
    return {
        "path": dest,
        "dpi": dpi,
        "width": int(width),
        "height": int(height),
        "bbox": [float(v) for v in bbox],
    }


def seed_base_cache(dxf_path: Path, bbox: list[float], image_path: Path, *, dpi=None) -> Path:
    """Store an unstamped sheet image as the base cache for this drawing+region."""
    dpi = choose_dpi(bbox, dpi)
    dest = base_cache_path(dxf_path, bbox, dpi=dpi)
    src = Path(image_path)
    if src.resolve() == dest.resolve():
        return dest
    tmp = dest.with_suffix(".seed.png")
    Image.open(src).convert("RGB").save(tmp, "PNG", optimize=True)
    tmp.replace(dest)
    return dest


def dirty_pixel_box(dirty_bbox, view_bbox, img_w, img_h, pad=PAD, pixel_pad: int = 2):
    x0, y_top = world_to_pixel(dirty_bbox[0], dirty_bbox[3], view_bbox, img_w, img_h, pad)
    x1, y_bot = world_to_pixel(dirty_bbox[2], dirty_bbox[1], view_bbox, img_w, img_h, pad)
    ix0 = max(0, int(math.floor(min(x0, x1))) - pixel_pad)
    iy0 = max(0, int(math.floor(min(y_top, y_bot))) - pixel_pad)
    ix1 = min(int(img_w), int(math.ceil(max(x0, x1))) + pixel_pad)
    iy1 = min(int(img_h), int(math.ceil(max(y_top, y_bot))) + pixel_pad)
    if ix1 <= ix0 or iy1 <= iy0:
        return None
    return ix0, iy0, ix1, iy1


def _fit_patch(im: Image.Image, width: int, height: int, *, rgba: bool = False) -> Image.Image:
    mode = "RGBA" if rgba else "RGB"
    fill = (0, 0, 0, 0) if rgba else (255, 255, 255)
    im = im.convert(mode)
    if im.size == (width, height):
        return im
    canvas = Image.new(mode, (width, height), fill)
    src = im
    if src.size[0] != width or src.size[1] != height:
        # Prefer crop/pad over resampling so the world-to-pixel grid stays exact.
        src = src.crop((0, 0, min(src.size[0], width), min(src.size[1], height)))
    canvas.paste(src, (0, 0))
    return canvas


def _dirty_patch(
    doc,
    view_bbox: list[float],
    dirty_bbox: list[float],
    base_info: dict[str, Any],
    dest: Path,
    *,
    pad=PAD,
    entities=None,
):
    """Redraw dirty_bbox in the cached base's pixel grid. Returns patch, box, world, base, tmp."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img_w, img_h = int(base_info["width"]), int(base_info["height"])
    dpi = int(base_info["dpi"])
    box = dirty_pixel_box(dirty_bbox, view_bbox, img_w, img_h, pad=pad)
    if not box:
        raise ValueError("dirty region does not overlap the current view")
    ix0, iy0, ix1, iy1 = box
    pw, ph = ix1 - ix0, iy1 - iy0
    wx0, wy1 = pixel_to_world(ix0, iy0, view_bbox, img_w, img_h, pad)
    wx1, wy0 = pixel_to_world(ix1, iy1, view_bbox, img_w, img_h, pad)
    world = [min(wx0, wx1), min(wy0, wy1), max(wx0, wx1), max(wy0, wy1)]
    tmp = dest.with_suffix(".dirty.png")
    w_view = max(float(view_bbox[2]) - float(view_bbox[0]), 1)
    margin = 4000 if w_view > 40000 else 2000
    hatch = HatchPolicy.SHOW_OUTLINE if w_view > 80000 else HatchPolicy.SHOW_SOLID
    transparent = entities is not None
    with _MPL_LOCK:
        render_region(
            doc,
            doc.modelspace(),
            world,
            tmp,
            dpi=dpi,
            margin=margin,
            hatch=hatch,
            xlim=(world[0], world[2]),
            ylim=(world[1], world[3]),
            fig_wh=(pw / dpi, ph / dpi),
            entities=entities,
            transparent=transparent,
        )
    patch = _fit_patch(Image.open(tmp), pw, ph, rgba=transparent)
    base = Image.open(base_info["path"]).convert("RGBA" if transparent else "RGB")
    if base.size != (img_w, img_h):
        base = base.resize((img_w, img_h), Image.Resampling.NEAREST)
    return patch, box, world, base, tmp


def _paste_patch(base: Image.Image, patch: Image.Image, xy, *, transparent: bool) -> Image.Image:
    out = base
    if transparent and patch.mode == "RGBA":
        out.paste(patch, xy, patch)
        return out.convert("RGB")
    out.paste(patch, xy)
    if out.mode != "RGB":
        return out.convert("RGB")
    return out


def composite_dirty_preview(
    doc,
    view_bbox: list[float],
    dirty_bbox: list[float],
    base_info: dict[str, Any],
    dest: Path,
    *,
    stamp: Optional[str] = None,
    pad=PAD,
    entities=None,
) -> Path:
    """Redraw only dirty_bbox with the cached base's transform, then paste and stamp."""
    dest = Path(dest)
    patch, box, _world, base, tmp = _dirty_patch(
        doc, view_bbox, dirty_bbox, base_info, dest, pad=pad, entities=entities,
    )
    out = _paste_patch(base.copy(), patch, (box[0], box[1]), transparent=entities is not None)
    composed = dest.with_suffix(".composed.png")
    out.save(composed, "PNG", optimize=True)
    try:
        tmp.unlink()
    except Exception:
        pass
    if stamp:
        stamp_preview(composed, stamp)
    composed.replace(dest)
    return dest


def render_dirty_crop_preview(
    doc,
    view_bbox: list[float],
    dirty_bbox: list[float],
    base_info: dict[str, Any],
    dest: Path,
    *,
    stamp: Optional[str] = None,
    pad=PAD,
    entities=None,
) -> list[float]:
    """Redraw the dirty region and save only that crop, not a full-sheet composite.

    Returns the exact world bbox of the saved image (edge-to-edge, no extra pad).
    """
    dest = Path(dest)
    patch, box, world, base, tmp = _dirty_patch(
        doc, view_bbox, dirty_bbox, base_info, dest, pad=pad, entities=entities,
    )
    ix0, iy0, ix1, iy1 = box
    crop = _paste_patch(base.crop((ix0, iy0, ix1, iy1)), patch, (0, 0), transparent=entities is not None)
    out = dest.with_suffix(".crop.png")
    crop.save(out, "PNG", optimize=True)
    try:
        tmp.unlink()
    except Exception:
        pass
    if stamp:
        stamp_preview(out, stamp)
    out.replace(dest)
    return world


def nearly_full_dirty(dirty_bbox, view_bbox, *, area_ratio: float = 0.8, span_ratio: float = 0.92) -> bool:
    dx = max(float(dirty_bbox[2]) - float(dirty_bbox[0]), 0)
    dy = max(float(dirty_bbox[3]) - float(dirty_bbox[1]), 0)
    vw = max(float(view_bbox[2]) - float(view_bbox[0]), 1)
    vh = max(float(view_bbox[3]) - float(view_bbox[1]), 1)
    if (dx * dy) / (vw * vh) >= area_ratio:
        return True
    return (dx / vw) >= span_ratio and (dy / vh) >= span_ratio


def _font(size: int, bold: bool = False):
    path = config.CJK_FONT_BOLD if bold and Path(config.CJK_FONT_BOLD).exists() else config.CJK_FONT
    if Path(path).exists():
        try:
            return ImageFont.truetype(path, size=size, index=2)
        except Exception:
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def _plan_has_marks(plan: dict[str, Any]) -> bool:
    return bool(plan.get("inserts") or plan.get("pipes") or plan.get("texts") or plan.get("deletes") or plan.get("replaces") or plan.get("moves"))


def overlay_preview(base_path: Path, bbox: list[float], plan: dict[str, Any], dest: Path) -> Path:
    """Draw proposed inserts/pipes/texts onto a copy of the crop. Official DXF is not touched."""
    img = Image.open(base_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = img.size
    cyan = (42, 111, 122, 230)
    ochre = (196, 132, 42, 235)
    redline = (181, 74, 58, 235)
    ink = (28, 43, 51, 235)
    font = _font(max(16, w // 80))
    small = _font(max(14, w // 90))

    def xy(pt):
        return world_to_pixel(float(pt[0]), float(pt[1]), bbox, w, h)

    for pipe in plan.get("pipes") or []:
        try:
            a = xy(pipe["from"])
            b = xy(pipe["to"])
        except Exception:
            continue
        width = pipe.get("const_width") or 25
        # scale line width loosely from drawing units
        span = max(bbox[2] - bbox[0], 1)
        lw = max(3, min(10, int(w * (float(width) * 4) / span)))
        draw.line([a, b], fill=cyan, width=lw)
        # end caps
        r = max(3, lw)
        for pt in (a, b):
            draw.ellipse((pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r), fill=cyan)

    for ins in plan.get("inserts") or []:
        try:
            cx, cy = xy(ins["xy"])
        except Exception:
            continue
        size = max(10, w // 70)
        draw.polygon(
            [
                (cx, cy - size),
                (cx + size, cy),
                (cx, cy + size),
                (cx - size, cy),
            ],
            outline=ochre,
            width=3,
        )
        draw.line((cx - size * 0.4, cy, cx + size * 0.4, cy), fill=ochre, width=2)
        draw.line((cx, cy - size * 0.4, cx, cy + size * 0.4), fill=ochre, width=2)
        label = str(ins.get("block") or "插入")
        draw.text((cx + size + 4, cy - size), label, font=font, fill=ochre)

    rust = (181, 74, 58, 235)
    for item in plan.get("deletes") or []:
        try:
            cx, cy = xy(item.get("xy") or item.get("from_xy") or item.get("from"))
        except Exception:
            continue
        size = max(10, w // 70)
        draw.line((cx - size, cy - size, cx + size, cy + size), fill=rust, width=3)
        draw.line((cx - size, cy + size, cx + size, cy - size), fill=rust, width=3)
        label = str(item.get("block") or "删除")
        draw.text((cx + size + 4, cy - size), "删除 " + label, font=font, fill=rust)

    for item in plan.get("replaces") or []:
        try:
            fx, fy = xy(item.get("from_xy") or item.get("from") or item.get("xy"))
            tx, ty = xy(item.get("xy") or item.get("to") or item.get("from_xy") or item.get("from"))
        except Exception:
            continue
        size = max(10, w // 70)
        draw.line((fx - size, fy - size, fx + size, fy + size), fill=rust, width=3)
        draw.line((fx - size, fy + size, fx + size, fy - size), fill=rust, width=3)
        draw.polygon(
            [(tx, ty - size), (tx + size, ty), (tx, ty + size), (tx - size, ty)],
            outline=ochre,
            width=3,
        )
        label = f"{item.get('from_block') or item.get('block') or '原块'} → {item.get('block') or '新块'}"
        draw.text((tx + size + 4, ty - size), label, font=font, fill=ochre)

    for item in plan.get("moves") or []:
        try:
            a = xy(item.get("from") or item.get("from_xy"))
            b = xy(item.get("to") or item.get("xy"))
        except Exception:
            continue
        draw.line([a, b], fill=ochre, width=3)
        draw.ellipse((b[0] - 5, b[1] - 5, b[0] + 5, b[1] + 5), fill=ochre)
        label = str(item.get("block") or "移动")
        draw.text((b[0] + 8, b[1] - 10), "移动 " + label, font=font, fill=ochre)

    for text in plan.get("texts") or []:
        try:
            tx, ty = xy(text["xy"])
        except Exception:
            continue
        label = str(text.get("text") or "")
        if not label:
            continue
        draw.rectangle((tx - 3, ty - 3, tx + 3, ty + 3), fill=redline)
        draw.text((tx + 6, ty - 8), label, font=font, fill=redline)

    # stamp — only a small board ticket, not a marketing badge
    if _plan_has_marks(plan):
        stamp = "方案预览，未写入正式图纸"
        tw, th = _text_size(draw, stamp, small)
        pad_x, pad_y = 10, 7
        x0, y0 = 12, 12
        draw.rectangle(
            (x0, y0, x0 + tw + pad_x * 2, y0 + th + pad_y * 2),
            fill=(232, 237, 240, 220),
            outline=ink,
            width=2,
        )
        draw.text((x0 + pad_x, y0 + pad_y), stamp, font=small, fill=ink)

    dest.parent.mkdir(parents=True, exist_ok=True)
    out = Image.alpha_composite(img, overlay).convert("RGB")
    out.save(dest, "PNG", optimize=True)
    return dest


def _text_size(draw, text, font):
    try:
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0], box[3] - box[1]
    except Exception:
        return (len(text) * 8, 14)


def stamp_preview(path: Path, text: str) -> Path:
    """Small corner ticket. No block names or coordinates."""
    path = Path(path)
    img = Image.open(path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, _h = img.size
    small = _font(max(14, w // 90))
    ink = (28, 43, 51, 235)
    tw, th = _text_size(draw, text, small)
    pad_x, pad_y = 10, 7
    x0, y0 = 12, 12
    draw.rectangle(
        (x0, y0, x0 + tw + pad_x * 2, y0 + th + pad_y * 2),
        fill=(232, 237, 240, 220),
        outline=ink,
        width=2,
    )
    draw.text((x0 + pad_x, y0 + pad_y), text, font=small, fill=ink)
    out = Image.alpha_composite(img, overlay).convert("RGB")
    tmp = path.with_suffix(".stamp.png")
    out.save(tmp, "PNG", optimize=True)
    tmp.replace(path)
    return path


def annotate_changes(
    path: Path,
    bbox,
    dirty,
    results: Optional[list[dict]] = None,
) -> Path:
    """在预览图上把「改哪儿」圈出来：一圈虚线框 + 一个落位点。

    取景扩到能看到周边之后，光看图不知道哪几笔是方案加的，这里补一层标记。
    """
    path = Path(path)
    img = Image.open(path).convert("RGBA")
    w, h = img.size
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    redline = (181, 74, 58, 235)
    ochre = (196, 132, 42, 235)

    def _px(x, y):
        return world_to_pixel(x, y, bbox, w, h, pad=PAD)

    box = None
    try:
        dx0, dy0, dx1, dy1 = [float(v) for v in dirty]
        ax, ay = _px(dx0, dy1)
        bx, by = _px(dx1, dy0)
        box = (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))
    except Exception:
        box = None

    width = max(2, w // 600)
    if box and box[2] - box[0] > 1 and box[3] - box[1] > 1:
        x0, y0, x1, y1 = box
        # 虚线框：线段自己算，省得依赖 PIL 的虚线支持。
        step = max(10, int((x1 - x0) // 24))
        for x in range(int(x0), int(x1), step * 2):
            draw.line([(x, y0), (min(x + step, x1), y0)], fill=redline, width=width)
            draw.line([(x, y1), (min(x + step, x1), y1)], fill=redline, width=width)
        step_y = max(10, int((y1 - y0) // 24))
        for y in range(int(y0), int(y1), step_y * 2):
            draw.line([(x0, y), (x0, min(y + step_y, y1))], fill=redline, width=width)
            draw.line([(x1, y), (x1, min(y + step_y, y1))], fill=redline, width=width)
        # 四角加粗，缩小看也能认出来。
        arm = max(8, min(24, int((x1 - x0) // 6), int((y1 - y0) // 6)))
        for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            draw.line([(cx, cy), (cx + arm * sx, cy)], fill=redline, width=width + 1)
            draw.line([(cx, cy), (cx, cy + arm * sy)], fill=redline, width=width + 1)

    # 落位点：每个写进去的插入/移动画一个小十字，防止框里有多处改动时找不到。
    for res in results or []:
        if not res.get("ok"):
            continue
        xy = res.get("xy") if res.get("action") in {"insert", "cover", "replace"} else res.get("to")
        if not xy or len(xy) < 2:
            continue
        try:
            px, py = _px(float(xy[0]), float(xy[1]))
        except Exception:
            continue
        r = max(4, w // 200)
        draw.line([(px - r, py), (px + r, py)], fill=ochre, width=width)
        draw.line([(px, py - r), (px, py + r)], fill=ochre, width=width)

    out = Image.alpha_composite(img, overlay).convert("RGB")
    tmp = path.with_suffix(".mark.png")
    out.save(tmp, "PNG", optimize=True)
    tmp.replace(path)
    return path


def render_doc_preview(doc, bbox, dest: Path, *, dpi=None, stamp: Optional[str] = None) -> Path:
    """Render an in-memory DXF crop. Does not touch the source path cache."""
    dest = Path(dest)
    dpi = choose_dpi(bbox, dpi)
    w = max(float(bbox[2]) - float(bbox[0]), 1)
    margin = 2000 if w > 40000 else 800
    hatch = HatchPolicy.SHOW_OUTLINE if w > 80000 else HatchPolicy.SHOW_SOLID
    tmp = dest.with_suffix(".tmp.png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _MPL_LOCK:
        render_region(doc, doc.modelspace(), bbox, tmp, dpi=dpi, margin=margin, hatch=hatch)
    if stamp:
        stamp_preview(tmp, stamp)
    tmp.replace(dest)
    return dest


def overlay_path_for(project_id: str, message_id: str) -> Path:
    path = config.VAR_DIR / "overlays" / project_id / f"{message_id}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def png_to_pdf(png_path: Path, dest: Path, *, dpi: Optional[int] = None) -> Path:
    """Wrap a sheet PNG in a single-page PDF. Raster, but sharpens with the preview dpi."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(png_path).convert("RGB")
    if not dpi:
        dpi = 130
    tmp = dest.with_suffix(".tmp.pdf")
    img.save(tmp, "PDF", resolution=float(dpi))
    tmp.replace(dest)
    return dest


def export_doc_pdf(doc, bbox: list[float], dest: Path, *, dpi=None, stamp: Optional[str] = None,
                   base_info: Optional[dict[str, Any]] = None, dirty_bbox: Optional[list[float]] = None,
                   entities=None) -> Path:
    """Export an in-memory drawing as a single-page PDF, reusing a cached base when possible."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dpi = choose_dpi(bbox, dpi)
    png = dest.with_suffix(".sheet.png")
    used_incremental = False
    if base_info and dirty_bbox and not nearly_full_dirty(dirty_bbox, bbox):
        try:
            composite_dirty_preview(
                doc, list(bbox), list(dirty_bbox), base_info, png, stamp=stamp, entities=entities,
            )
            used_incremental = True
        except Exception:
            used_incremental = False
    if not used_incremental:
        render_doc_preview(doc, list(bbox), png, dpi=dpi, stamp=stamp)
    png_to_pdf(png, dest, dpi=dpi)
    return dest


def export_region_pdf(dxf_path: Path, bbox: list[float], dest: Path, *, encoding="utf-8", dpi=None,
                      stamp: Optional[str] = None) -> Path:
    """Export a crop of the current drawing as a single-page PDF. Official DXF is not modified."""
    dpi = choose_dpi(bbox, dpi)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    png = base_preview(Path(dxf_path), list(bbox), encoding=encoding, dpi=dpi)
    if stamp:
        tmp_png = dest.with_suffix(".stamped.png")
        Image.open(png).convert("RGB").save(tmp_png, "PNG")
        stamp_preview(tmp_png, stamp)
        png_to_pdf(tmp_png, dest, dpi=dpi)
        try:
            tmp_png.unlink()
        except Exception:
            pass
        return dest
    return png_to_pdf(png, dest, dpi=dpi)
