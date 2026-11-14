#!/usr/bin/env python3
"""Black-preview fix: corrupt DIMENSION/MTEXT box_fill_scale must not cover the sheet."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CAD_DATA_DIR", "")

import ezdxf  # noqa: E402
from app import cad_render  # noqa: E402


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""), flush=True)


def _stats(path: Path):
    im = Image.open(path)
    colors = im.getcolors(im.size[0] * im.size[1]) or []
    colors.sort(key=lambda x: -x[0])
    total = im.size[0] * im.size[1]
    top_n, top_c = colors[0]
    rgb = top_c[:3]
    return {
        "size": im.size,
        "unique": len(colors),
        "top": rgb,
        "top_pct": 100.0 * top_n / total,
        "dark_bg": rgb == (33, 40, 48) or (rgb[0] < 50 and rgb[1] < 50 and rgb[2] < 60 and top_n / total > 0.9),
        "whiteish": rgb[0] > 240 and rgb[1] > 240 and rgb[2] > 240,
        "ink_pct": 100.0 * (total - top_n) / total,
    }


def _make_dxf(path: Path, *, corrupt: bool) -> Path:
    doc = ezdxf.new("AC1021")
    msp = doc.modelspace()
    # A visible red rectangle, 10000 x 8000 drawing units.
    msp.add_lwpolyline([(0, 0), (10000, 0), (10000, 8000), (0, 8000), (0, 0)], close=True, dxfattribs={"color": 1})
    msp.add_line((1000, 1000), (9000, 7000), dxfattribs={"color": 4})
    mtext = msp.add_mtext("150", dxfattribs={"char_height": 125, "color": 7, "insert": (5000, 4000)})
    if corrupt:
        mtext.dxf.bg_fill = 3
        mtext.dxf.box_fill_scale = 2576980378.0
        mtext.dxf.bg_fill_color = 256
        mtext.dxf.bg_fill_true_color = 3254780057
        mtext.dxf.bg_fill_transparency = 589554511
    doc.saveas(str(path), encoding="utf-8")
    return path


def test_sanitize_helper() -> None:
    doc = ezdxf.new("AC1021")
    msp = doc.modelspace()
    mtext = msp.add_mtext("R750", dxfattribs={"char_height": 125, "insert": (0, 0)})
    mtext.dxf.bg_fill = 3
    mtext.dxf.box_fill_scale = 2576980378.0
    mtext.dxf.bg_fill_true_color = 3254780057
    mtext.dxf.bg_fill_transparency = 589554511
    changed = cad_render.sanitize_text_background(mtext)
    _ok("sanitize-changed", changed)
    _ok("scale-clamped", abs(float(mtext.dxf.box_fill_scale) - 1.5) < 1e-6, str(mtext.dxf.box_fill_scale))
    _ok("true-color-dropped", not mtext.dxf.hasattr("bg_fill_true_color"))
    n = cad_render.sanitize_doc_text_backgrounds(doc)
    _ok("doc-sanitize-idempotent", n == 0, str(n))


def test_corrupt_mtext_does_not_blackout() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_dxf(td / "corrupt.dxf", corrupt=True)
        dest = td / "preview.png"
        doc = cad_render.load_doc(src)
        cad_render.render_region(doc, doc.modelspace(), [0, 0, 10000, 8000], dest, dpi=80, margin=200)
        st = _stats(dest)
        _ok("not-modelspace-black", not st["dark_bg"], str(st))
        _ok("paper-is-light", st["whiteish"], str(st))
        _ok("geometry-visible", st["ink_pct"] > 0.5, str(st))
        _ok("enough-colors", st["unique"] >= 8, str(st))


def test_clean_drawing_stays_readable() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = _make_dxf(td / "clean.dxf", corrupt=False)
        dest = td / "preview.png"
        doc = cad_render.load_doc(src)
        cad_render.render_region(doc, doc.modelspace(), [0, 0, 10000, 8000], dest, dpi=80, margin=200)
        st = _stats(dest)
        _ok("clean-paper-light", st["whiteish"], str(st))
        _ok("clean-geometry-visible", st["ink_pct"] > 0.5, str(st))


def main() -> int:
    test_sanitize_helper()
    test_corrupt_mtext_does_not_blackout()
    test_clean_drawing_stays_readable()
    print("ALL PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
