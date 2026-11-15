#!/usr/bin/env python3
"""预览渲染提速的回归测试。

锁住两件事：
1. 批量绘制（_BatchedBackend）出图与 ezdxf 原生后端一致——提速不能改变外观；
2. 虚线密度与曲线展平按输出像素限制——图幅很大时不能把一条线拆成几百段。
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf
from PIL import Image, ImageChops

from app import cad_render

SHEET = [0.0, 0.0, 40000.0, 28000.0]


def _ok(name: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f" → {detail}" if detail != "" else ""))
    if not cond:
        raise AssertionError(name)


def _make_drawing(path: Path) -> Path:
    doc = ezdxf.new("R2007", setup=True)
    if "DASHED" not in doc.linetypes:
        doc.linetypes.add("DASHED", pattern=[0.4, 0.2, -0.2])
    doc.layers.add("虚线层", color=3, linetype="DASHED")
    doc.layers.add("实线层", color=7)

    blk = doc.blocks.new(name="CHAIR")
    blk.add_circle((0, 0), radius=120)
    blk.add_arc((0, 0), radius=90, start_angle=0, end_angle=180)
    blk.add_lwpolyline([(-120, -80), (120, -80), (120, 80), (-120, 80)], close=True)
    for i in range(6):
        blk.add_line((-100, -60 + i * 20), (100, -60 + i * 20))

    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (40000, 0), (40000, 28000), (0, 28000)], close=True, dxfattribs={"layer": "实线层"}
    )
    for x in range(2000, 40000, 2000):
        msp.add_line((x, 0), (x, 28000), dxfattribs={"layer": "虚线层"})
    for y in range(2000, 28000, 2000):
        msp.add_line((0, y), (40000, y), dxfattribs={"layer": "虚线层"})
    for row in range(6):
        for col in range(8):
            msp.add_blockref("CHAIR", (2500 + col * 4800, 2500 + row * 4200), dxfattribs={"layer": "实线层"})
    msp.add_text("平面图", height=600).set_placement((1000, 26000))
    doc.saveas(str(path))
    return path


def test_pixel_limits() -> None:
    small_dash, small_flat = cad_render._pixel_limits(1000.0, 2080.0)
    big_dash, big_flat = cad_render._pixel_limits(40000.0, 2080.0)
    _ok("limits-scale-with-span", big_dash > small_dash * 5, (small_dash, big_dash))
    _ok("limits-dash-above-zero", small_dash > 0 and big_dash > 0)
    _ok("limits-flatten-clamped", 0.02 <= big_flat <= 50.0, big_flat)
    _ok("limits-flatten-scales", big_flat > small_flat, (small_flat, big_flat))
    # 除以零/坏参数不能炸
    edge = cad_render._pixel_limits(0.0, 0.0)
    _ok("limits-survive-zero", all(v > 0 for v in edge), edge)


def test_batched_matches_native(tmp: Path, dxf: Path) -> None:
    doc = ezdxf.readfile(str(dxf))
    msp = doc.modelspace()
    ents = cad_render.filter_layout(msp, SHEET, margin=1000)
    _ok("fixture-has-entities", len(ents) > 50, len(ents))

    native = tmp / "native.png"
    batched = tmp / "batched.png"
    t0 = time.time()
    cad_render.render_region(doc, msp, SHEET, native, dpi=110, entities=ents, batch=False)
    t_native = time.time() - t0
    t0 = time.time()
    cad_render.render_region(doc, msp, SHEET, batched, dpi=110, entities=ents, batch=True)
    t_batched = time.time() - t0

    a = Image.open(native).convert("L")
    b = Image.open(batched).convert("L")
    _ok("same-size", a.size == b.size, (a.size, b.size))

    hist = ImageChops.difference(a, b).histogram()
    total = sum(hist)
    identical = hist[0] / total
    _ok("pixels-identical", identical > 0.995, f"{identical * 100:.3f}%")
    big_diff = sum(hist[32:]) / total
    _ok("no-visible-diff", big_diff < 0.002, f"{big_diff * 100:.4f}%")

    ink = sum(1 for v in b.getdata() if v < 200) / total
    _ok("has-drawing", 0.002 < ink < 0.6, f"ink={ink * 100:.2f}%")
    print(f"     native={t_native:.2f}s batched={t_batched:.2f}s")
    _ok("batched-not-slower", t_batched <= t_native * 1.2 + 0.2, (t_native, t_batched))


def test_dash_density_bounded(tmp: Path, dxf: Path) -> None:
    """大图幅 + 虚线：段数应被像素尺度压住，而不是跟图面长度成正比。"""
    doc = ezdxf.readfile(str(dxf))
    msp = doc.modelspace()
    ents = cad_render.filter_layout(msp, SHEET, margin=1000)

    counter = {"segments": 0, "calls": 0}
    orig = cad_render._BatchedBackend.draw_solid_lines

    def counting(self, segs, properties):
        n = 0
        for _ in segs:
            n += 1
        counter["segments"] += n
        counter["calls"] += 1
        return orig(self, segs, properties)

    cad_render._BatchedBackend.draw_solid_lines = counting
    try:
        cad_render.render_region(
            doc, msp, SHEET, tmp / "dash.png", dpi=110, entities=ents, batch=True
        )
    finally:
        cad_render._BatchedBackend.draw_solid_lines = orig

    _ok("dash-batched-calls", counter["calls"] > 0, counter)
    # 图里虚线总长约 2 × (20 条 × 40m) ≈ 大几百米，按 1.2px 限密度后
    # 段数应与像素数同量级，而不是十几万。
    _ok("dash-segments-bounded", counter["segments"] < 60000, counter["segments"])

    # 关掉按像素限制（等价旧行为）应该明显更多段。
    counter2 = {"segments": 0}
    orig_limits = cad_render._pixel_limits
    cad_render._pixel_limits = lambda *a, **k: (0.01, 0.01)
    cad_render._BatchedBackend.draw_solid_lines = lambda self, segs, properties: (
        counter2.__setitem__("segments", counter2["segments"] + sum(1 for _ in segs)),
        orig(self, segs, properties),
    )[1]
    try:
        cad_render.render_region(
            doc, msp, SHEET, tmp / "dash_dense.png", dpi=110, entities=ents, batch=True
        )
    finally:
        cad_render._BatchedBackend.draw_solid_lines = orig
        cad_render._pixel_limits = orig_limits
    _ok(
        "dash-limit-actually-helps",
        counter2["segments"] > counter["segments"] * 2,
        (counter["segments"], counter2["segments"]),
    )


def test_switch_off(tmp: Path, dxf: Path) -> None:
    """BATCH_LINES 关掉时应回到原生后端，且能正常出图。"""
    old = cad_render.BATCH_LINES
    cad_render.BATCH_LINES = False
    try:
        doc = ezdxf.readfile(str(dxf))
        msp = doc.modelspace()
        ents = cad_render.filter_layout(msp, SHEET, margin=1000)
        out = tmp / "off.png"
        cad_render.render_region(doc, msp, SHEET, out, dpi=90, entities=ents)
        _ok("switch-off-renders", out.is_file() and out.stat().st_size > 1000)
    finally:
        cad_render.BATCH_LINES = old


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tuteng-render-"))
    dxf = _make_drawing(tmp / "drawing.dxf")
    test_pixel_limits()
    test_batched_matches_native(tmp, dxf)
    test_dash_density_bounded(tmp, dxf)
    test_switch_off(tmp, dxf)
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
