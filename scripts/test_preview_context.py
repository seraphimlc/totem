#!/usr/bin/env python3
"""方案预览要「看得清 + 看得到环境」。

回归两件事：
1. 取景窗口不能只框住改动本身（那样屏幕上只剩两三个符号，看不出原貌）；
2. 位图预览不能再从整幅底图上裁（小区域会裁出一百多像素的缩略图）。
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf
from PIL import Image

from app import apply, cad_render, placement, scene

SHEET = [0.0, 0.0, 20000.0, 14000.0]


def _ok(name: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" → {detail}" if detail != "" else ""))
    if not cond:
        raise AssertionError(name)


def _span(box) -> tuple[float, float]:
    return (box[2] - box[0], box[3] - box[1])


def _make_drawing(path: Path) -> Path:
    doc = ezdxf.new("R2007", setup=True)
    doc.layers.add("图框", color=7)
    blk = doc.blocks.new(name="CHAIR")
    # 图形中心刻意偏离块基点：这正是「插入点看着偏」的来源。
    blk.add_lwpolyline([(-300, -250), (300, -250), (300, 250), (-300, 250)], close=True)
    blk.add_circle((0, -250), radius=40)
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (20000, 0), (20000, 14000), (0, 14000)],
        close=True,
        dxfattribs={"layer": "图框"},
    )
    # 两道内墙 + 轴网，作为「周边环境」的参照
    msp.add_line((0, 7000), (20000, 7000), dxfattribs={"layer": "图框"})
    msp.add_line((10000, 0), (10000, 14000), dxfattribs={"layer": "图框"})
    for x in range(2000, 20000, 2000):
        msp.add_line((x, 0), (x, 300), dxfattribs={"layer": "图框"})
    for y in range(2000, 14000, 2000):
        msp.add_line((0, y), (300, y), dxfattribs={"layer": "图框"})
    msp.add_text("大堂", height=400).set_placement((4000, 11000))
    msp.add_blockref("CHAIR", (6000, 4000))
    msp.add_blockref("CHAIR", (8000, 4000))
    doc.saveas(str(path))
    return path


def _proj(root: Path, dxf: Path) -> dict:
    return {
        "id": "preview-test",
        "name": "preview-test",
        "root": str(root),
        "dxf_path": str(dxf),
        "encoding": "utf-8",
        "chat_path": str(root / "chat.json"),
    }


def _region() -> dict:
    return {"id": "sheet:test", "title": "测试平面", "bbox": list(SHEET)}


def test_context_geometry() -> None:
    view = list(SHEET)
    dirty = [10000.0, 6000.0, 14000.0, 8000.0]  # 4000 x 2000
    ctx = placement.context_bbox(dirty, view)
    w, h = _span(ctx)
    _ok("context-wide-enough", w >= 8000 and h >= 8000, (w, h))
    _ok("context-caps-at-60pct", w <= 20000 * 0.6 + 1 and h <= 14000 * 0.6 + 1, (w, h))
    cx, cy = (ctx[0] + ctx[2]) / 2, (ctx[1] + ctx[3]) / 2
    _ok("context-centered", abs(cx - 12000) < 1 and abs(cy - 7000) < 1, (cx, cy))
    _ok(
        "context-covers-dirty",
        ctx[0] <= dirty[0] and ctx[1] <= dirty[1] and ctx[2] >= dirty[2] and ctx[3] >= dirty[3],
        ctx,
    )

    # 贴着图幅角，窗口要被推回来，而不是切掉改动。
    corner = [100.0, 100.0, 900.0, 600.0]
    cctx = placement.context_bbox(corner, view)
    _ok(
        "context-clamped-to-view",
        cctx[0] >= -1 and cctx[1] >= -1 and cctx[2] <= 20001 and cctx[3] <= 14001,
        cctx,
    )
    _ok(
        "context-keeps-corner-edits",
        cctx[0] <= corner[0] and cctx[1] <= corner[1],
        cctx,
    )

    # 改动本身就很大时，干脆给整幅。
    big = [0.0, 0.0, 20000.0, 14000.0]
    _ok("context-big-becomes-view", placement.context_bbox(big, view) == view)

    # 没有图幅信息时按改动自身外扩。
    only = placement.context_bbox([0.0, 0.0, 1000.0, 1000.0], None)
    ow, oh = _span(only)
    _ok("context-without-view", ow > 1000 and oh > 1000, (ow, oh))


def test_scene_fit_window() -> None:
    dirty = [11000.0, 6000.0, 15000.0, 8000.0]
    patch = {"dirty_bbox": dirty, "inserts": [{"x": 13000.0, "y": 7000.0}]}
    scene_dict = {"bbox": list(SHEET)}
    fit = scene.fit_bbox_for_patch(patch, scene_dict)
    w, h = _span(fit)
    _ok("fit-not-tight", w >= 8000 and h >= 8000, (w, h))
    _ok("fit-not-whole-sheet", w < 20000 * 0.9, w)
    dw, dh = _span(dirty)
    _ok("fit-has-context", w > dw * 1.8 and h > dh * 1.8, ((w, dw), (h, dh)))

    # 没有 scene 时也不能崩。
    bare = scene.fit_bbox_for_patch({"dirty_bbox": dirty})
    _ok("fit-without-scene", bare is not None and _span(bare)[0] > dw, bare)


def test_preview_png(root: Path, dxf: Path) -> None:
    proj = _proj(root, dxf)
    region = _region()
    plan = {
        "mode": "plan",
        "reply": "在大堂中间补一张椅子，请看右侧预览。",
        "inserts": [{"block": "CHAIR", "xy": [12000.0, 6000.0], "rot": 0, "scale": [1, 1]}],
    }
    before = hashlib.sha256(dxf.read_bytes()).hexdigest()
    dest = root / "preview.png"
    out = apply.render_plan_preview(proj, plan, region, dest)

    _ok("preview-crop-used", bool(out.get("crop")), out.get("crop"))
    crop = out.get("crop_bbox") or []
    dirty = out.get("dirty_bbox") or []
    _ok("preview-has-crop-bbox", len(crop) == 4, crop)
    cw, ch = _span(crop)
    dw, dh = _span(dirty)
    _ok("preview-window-bigger-than-dirty", cw > dw * 1.8 and ch > dh * 1.8, ((cw, dw), (ch, dh)))
    _ok("preview-focus-bbox", out.get("focus_bbox") == dirty, out.get("focus_bbox"))

    im = Image.open(dest).convert("RGB")
    w, h = im.size
    _ok("preview-pixels-legible", w >= 1200 and h >= 400, (w, h))

    gray = im.convert("L")
    px = list(gray.getdata())
    ink = sum(1 for v in px if v < 200) / len(px)
    _ok("preview-has-drawing", 0.001 < ink < 0.6, f"ink={ink * 100:.2f}%")

    # 改动框要真的画出来，缩小看也能找到。
    red = 0
    for r, g, b in im.getdata():
        if r > 140 and g < 110 and b < 100:
            red += 1
    _ok("preview-marked", red > 200, f"red px={red}")

    after = hashlib.sha256(dxf.read_bytes()).hexdigest()
    _ok("preview-does-not-write-dxf", before == after)

    # 强制整幅渲染仍可用。
    full = apply.render_plan_preview(proj, plan, region, root / "full.png", crop=False)
    _ok("preview-full-sheet", full.get("crop") is False)
    _ok("preview-full-matches-sheet", Image.open(root / "full.png").size[0] >= 1200)


def test_annotate_marks(root: Path) -> None:
    src = root / "plain.png"
    cad_render.render_doc_preview(_load(root / "drawing.dxf"), [10000.0, 5000.0, 14000.0, 9000.0], src, stamp=None)
    cad_render.annotate_changes(src, [10000.0, 5000.0, 14000.0, 9000.0], [12000.0, 7000.0, 12500.0, 7500.0], [])
    im = Image.open(src).convert("RGB")
    red = sum(1 for r, g, b in im.getdata() if r > 140 and g < 110 and b < 100)
    _ok("annotate-draws-frame", red > 200, f"red px={red}")


def _load(path: Path):
    import ezdxf as _ez

    return _ez.readfile(str(path))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tuteng-preview-"))
    dxf = _make_drawing(tmp / "drawing.dxf")
    test_context_geometry()
    test_scene_fit_window()
    test_annotate_marks(tmp)
    test_preview_png(tmp, dxf)
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
