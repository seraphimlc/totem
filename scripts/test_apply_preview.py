#!/usr/bin/env python3
"""Unit tests for overlay replace, plan preview, write-back, undo, and compact replies."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CAD_DATA_DIR", "")

MALL = Path("/workspace/cad-nl/tmp/rebuild-indexes/万商城-session.dxf")
PREVIEW_DIR = ROOT / "var" / "preview-tests"


def _fixture(env_name: str, candidates: list[Path], fallback: str) -> Path:
    """Env override → original Linux paths → a temp path that will just not exist."""
    env = (os.getenv(env_name) or "").strip()
    if env:
        return Path(env)
    data = (os.getenv("CAD_DATA_DIR") or "").strip()
    if data:
        candidates = [Path(data) / "tmp" / "rebuild-indexes" / Path(fallback).name] + candidates
    for cand in candidates:
        if cand.is_file():
            return cand
    return Path(tempfile.gettempdir()) / "tuteng-fixtures" / Path(fallback).name


MALL = _fixture("TUTENG_DXF_FIXTURE", [MALL], "万商城-session.dxf")

SQUAT = "做成ACAD TuKuai0155"
SEAT = "A$C2A8643E5"
REGION_ID = "subview:水施-007:二层卫排水详图"
REGION_BBOX = [2955772.3, 504433.0, 2980607.0, 516475.8]
SQUATS = [
    [2968464.486, 513987.555],
    [2968464.486, 512982.555],
    [2968464.486, 511977.555],
    [2968464.486, 510972.555],
    [2968464.486, 509847.555],
    [2968464.486, 508842.555],
    [2968464.486, 507837.555],
    [2968464.486, 506832.555],
]
ACCESS = [2964524.845, 508532.778]


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""), flush=True)


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _proj(root: Path, dxf: Path) -> dict:
    return {
        "id": "test",
        "name": "test",
        "root": str(root),
        "dxf_path": str(dxf),
        "encoding": "utf-8",
        "chat_path": str(root / "chat.json"),
    }


def _mall_plan(*, rot=90, squats=None):
    pts = list(SQUATS if squats is None else squats)
    return {
        "mode": "plan",
        "reply": "已把本图八处蹲便改为坐便，请看右侧预览。" if len(pts) == 8 else f"已把本图{len(pts)}处蹲便改为坐便，请看右侧预览。",
        "analysis": "THIS ANALYSIS MUST NOT LEAK INTO REPLY OR HISTORY. " + "x=2968464 " * 20,
        "sheet": "二层卫排水详图",
        "replaces": [
            {
                "from_block": SQUAT,
                "from_xy": xy,
                "block": SEAT,
                "xy": xy,
                "rot": rot,
                "scale": [1, 1],
                "layer": "FU-TECH-卫浴",
            }
            for xy in pts
        ],
        "inserts": [],
        "deletes": [],
        "moves": [],
        "pipes": [],
        "texts": [],
        "do_not_touch": [ACCESS],
    }


def test_reply_and_history() -> None:
    from app import prompts

    plan = _mall_plan()
    _ok("reply-ignores-analysis", prompts.reply_text(plan, "RAW JSON") == plan["reply"])
    _ok(
        "reply-empty-not-analysis",
        "THIS ANALYSIS" not in prompts.reply_text({"analysis": "THIS ANALYSIS MUST NOT LEAK", "reply": ""}, ""),
    )
    hist = prompts.build_history_messages(
        [
            {"role": "user", "text": "把蹲便改成坐便"},
            {"role": "assistant", "text": plan["reply"], "plan": plan},
        ]
    )
    blob = json.dumps(hist, ensure_ascii=False)
    _ok("history-no-coord-array", "2968464" not in blob, blob)
    _ok("history-no-full-replaces", '"replaces"' not in blob and "from_xy" not in blob, blob)
    _ok("history-one-line", "替换8处" in blob, blob)
    _ok("history-max-chars-default-shrunk", prompts.build_history_messages.__kwdefaults__.get("max_chars", 9999) <= 3000)


def _make_nested_drawing(path: Path) -> dict:
    import ezdxf

    doc = ezdxf.new("R2010")
    squat = doc.blocks.new("SQUAT")
    squat.add_lwpolyline([(0, -50), (200, -50), (200, 50), (0, 50)], close=True)
    squat.add_hatch(color=1).paths.add_polyline_path([(0, -50), (200, -50), (200, 50), (0, 50)], is_closed=True)
    seat = doc.blocks.new("SEAT")
    seat.add_lwpolyline([(-40, -300), (40, -300), (40, 0), (-40, 0)], close=True)
    host = doc.blocks.new("HOST")
    host.add_lwpolyline([(-400, -400), (400, -400), (400, 400), (-400, 400)], close=True)
    # Phase 3：共享图块内部的符号一律不改（覆盖也无法可靠盖住共享块图元，块定义更不能改）。
    # 这个蹲便仍然保留在 HOST 里，用来验证「计划点什么，块定义都原样不动」。
    host.add_blockref("SQUAT", insert=(100, 80), dxfattribs={"rotation": 180, "layer": "FF-FIXT"})
    # 一个普通共享块：被两处插入，用来验证「嵌套目标被如实拒绝」
    plain = doc.blocks.new("PLAIN_HOST")
    # PLAIN_HOST 里一个「保护层」符号：只要目标是它，就必须拒绝，而且绝不能退化成
    # 「覆盖共享块」或去改块定义。
    plain.add_lwpolyline([(-400, -400), (400, -400), (400, 400), (-400, 400)], close=True)
    plain.add_blockref("SEAT", insert=(0, 0), dxfattribs={"layer": "图框"})
    plain.add_text("共享注记", dxfattribs={"height": 50, "insert": (0, -300)})
    msp = doc.modelspace()
    msp.add_blockref("HOST", insert=(10000, 20000), dxfattribs={"layer": "TWT_DIM"})
    msp.add_blockref("HOST", insert=(80000, 80000), dxfattribs={"layer": "TWT_DIM"})
    msp.add_blockref("PLAIN_HOST", insert=(10000, 10000), dxfattribs={"layer": "TWT_DIM"})
    msp.add_blockref("PLAIN_HOST", insert=(10200, 10200), dxfattribs={"layer": "0"})
    msp.add_blockref("SQUAT", insert=(500, 600), dxfattribs={"rotation": 0, "layer": "FF-FIXT"})
    doc.saveas(str(path))
    # world pos of nested squat in first host: 10000+100, 20000+80 = 10100, 20080
    return {
        "nested_xy": [10100.0, 20080.0],
        "shared_xy": [10000.0, 10000.0],
        "layer0_xy": [10200.0, 10200.0],
        # 共享块符号只在本视图的检查框里：模型空间宿主 PLAIN_HOST 在 (10000, 10000)，
        # 嵌套 squat 的定位框必须圈住宿主本身。
        "shared_bbox": [9500.0, 9500.0, 15000.0, 15000.0],
        "other_host": [80000.0, 80000.0],
        "direct_xy": [500.0, 600.0],
        "bbox": [9000.0, 19000.0, 12000.0, 22000.0],
    }


def test_overlay_replace_and_undo() -> None:
    import ezdxf
    from app import apply

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "out" / "source.dxf"
        src.parent.mkdir(parents=True)
        info = _make_nested_drawing(src)
        before = _sha(src)
        proj = _proj(root, src)
        region = {"id": "subview:test:stalls", "bbox": info["bbox"]}
        plan = {
            "mode": "plan",
            "reply": "已替换一处蹲便。",
            "replaces": [
                {
                    "from_block": "SQUAT",
                    "from_xy": info["nested_xy"],
                    "block": "SEAT",
                    "xy": info["nested_xy"],
                    "rot": 90,
                    "layer": "FU-TECH-卫浴",
                }
            ],
        }

        # Phase 3：共享块里的蹲便不能「声称已替换」。预览/应用都如实报不支持，什么都不写。
        shared_plan = {
            "mode": "plan",
            "reply": "已替换一处蹲便。",
            "replaces": [
                {
                    "from_block": "SEAT",
                    "from_xy": info["shared_xy"],
                    "block": "SQUAT",
                    "xy": info["shared_xy"],
                    "rot": 90,
                    "layer": "FU-TECH-卫浴",
                }
            ],
        }
        shared_region = {"id": "subview:test:shared", "bbox": info["shared_bbox"]}
        try:
            apply.render_plan_preview(proj, shared_plan, shared_region, root / "shared.png")
        except apply.PlanBlockedError as exc:
            # 预检如实报「嵌套在共享块里」（nested_not_supported），不能是笼统的 not_applied。
            _ok("nested-preview-blocked", "nested_not_supported" in exc.codes and "not_applied" not in exc.codes, str(exc.codes))
        else:
            raise AssertionError("FAIL nested-preview-blocked: 嵌套目标居然预览成功")
        try:
            apply.apply_plan(proj, shared_plan, region=shared_region, message_id="nested")
        except apply.PlanBlockedError as exc:
            _ok("nested-apply-blocked", "nested_not_supported" in exc.codes and "not_applied" not in exc.codes, str(exc.codes))
        else:
            raise AssertionError("FAIL nested-apply-blocked: 嵌套目标居然应用成功")
        _ok("nested-no-write", _sha(src) == before and not apply.is_dirty(proj) and apply.load_history(proj)["entries"] == [])

        # 共享块被插在图层 0 的实例：模型空间实体自己图层是 0，但共享块里 0 层符号继承宿主，
        # 而宿主在 0 层时 CAD 里实际继承的是共享块定义所处的图层——这一处在模型空间找不到，
        # 不能声称已改。
        layer0_plan = {
            "mode": "plan",
            "reply": "改掉共享块里的蹲便。",
            "deletes": [{"block": "SEAT", "xy": info["layer0_xy"]}],
        }
        try:
            apply.render_plan_preview(proj, layer0_plan, region, root / "layer0.png")
        except ValueError as exc:
            _ok("layer0-nested-not-found", "没有找到" in str(exc), str(exc))
        else:
            raise AssertionError("FAIL layer0-nested-not-found: 模型空间里找不到的共享块内容被当成已定位")
        _ok("layer0-no-write", _sha(src) == before)

        # 顶层替换：预览不写文件、不建修订；确认后写工作图与会话源，撤回恢复原字节。
        plan2 = {
            "mode": "plan",
            "reply": "删掉明装蹲便。",
            "replaces": [
                {
                    "from_block": "SQUAT",
                    "from_xy": info["direct_xy"],
                    "block": "SEAT",
                    "xy": info["direct_xy"],
                    "rot": 0,
                }
            ],
        }
        region2 = {"id": "r2", "bbox": [0.0, 0.0, 2000.0, 2000.0]}
        dest_png = root / "preview.png"
        preview = apply.render_plan_preview(proj, plan2, region2, dest_png)
        _ok("preview-png", dest_png.is_file() and dest_png.stat().st_size > 0, str(dest_png.stat().st_size))
        _ok("preview-no-revision", apply.load_history(proj)["entries"] == [])
        _ok("preview-no-work", not apply.is_dirty(proj))
        _ok("preview-source-untouched", _sha(src) == before)
        _ok("preview-status-no-undo", apply.status(proj)["can_undo"] is False)

        host_before = _host_squat_count(src, "HOST")
        result = apply.apply_plan(proj, plan2, region=region2, message_id="m1")
        _ok("confirm-ok", result["ok"] and result["can_undo"] and result["ok_count"] >= 1, str(result))
        _ok("new-layer", str(result.get("layer") or "").startswith("图腾-新增-"), result.get("layer"))
        _ok("source-written", _sha(src) != before)
        _ok("official-flag", result.get("official_untouched") is False)
        work = apply.work_path(proj)
        _ok("work-written", work.is_file())
        doc = ezdxf.readfile(str(work))
        msp = doc.modelspace()
        _ok("host-def-unchanged", _host_squat_count(work, "HOST") == host_before)
        work_doc = ezdxf.readfile(str(work))
        _ok(
            "plain-host-def-unchanged",
            sum(1 for e in work_doc.blocks.get("PLAIN_HOST") if e.dxftype() == "INSERT" and e.dxf.name == "SEAT") == 1,
        )
        top_squats = [
            e
            for e in msp.query("INSERT")
            if e.dxf.name == "SQUAT" and abs(e.dxf.insert.x - 500) < 1 and abs(e.dxf.insert.y - 600) < 1
        ]
        _ok("direct-old-deleted", top_squats == [], str(len(top_squats)))
        new_inserts = [e for e in msp.query("INSERT") if e.dxf.name == "SEAT"]
        _ok("new-insert-present", len(new_inserts) == 1, str(len(new_inserts)))
        _ok(
            "new-insert-layer",
            all(str(e.dxf.layer).startswith("图腾-新增-") for e in new_inserts),
            ",".join(e.dxf.layer for e in new_inserts),
        )
        _ok("not-official-layer", all(e.dxf.layer != "FU-TECH-卫浴" for e in new_inserts))
        other = [e for e in msp.query("INSERT") if e.dxf.name in {"HOST", "PLAIN_HOST"}]
        _ok("both-hosts-remain", len(other) == 4, str([e.dxf.name for e in other]))
        undone = apply.undo_last(proj)
        _ok("undo-ok", undone["ok"] and undone.get("restored_official"))
        _ok("undo-no-work", not apply.is_dirty(proj))
        _ok("undo-no-more", undone["can_undo"] is False)
        _ok("undo-source-bytes", _sha(src) == before, "source bytes should match pre-edit")
        _ok("status-untouched", apply.status(proj)["official_untouched"] is True)

def _host_squat_count(dxf: Path, host: str) -> int:
    import ezdxf

    doc = ezdxf.readfile(str(dxf))
    return sum(1 for e in doc.blocks.get(host) if e.dxftype() == "INSERT" and e.dxf.name == "SQUAT")


def test_mall_preview_and_writeback() -> None:
    import ezdxf
    from app import apply, cad_render

    _ok("mall-present", MALL.is_file(), str(MALL))
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    region = {"id": REGION_ID, "bbox": REGION_BBOX}
    plan = _mall_plan(rot=90)

    # Before: official crop + old annotation-style overlay (for comparison only).
    before_png = PREVIEW_DIR / "before-region.png"
    cad_render.base_preview(MALL, REGION_BBOX, encoding="utf-8")
    # Re-render through base_preview cache or copy out:
    cached = cad_render.base_preview(MALL, REGION_BBOX, encoding="utf-8")
    shutil.copy2(cached, before_png)
    anno = PREVIEW_DIR / "before-annotation-overlay.png"
    cad_render.overlay_preview(cached, REGION_BBOX, plan, anno)
    _ok("before-png", before_png.is_file() and before_png.stat().st_size > 0, str(before_png))
    _ok("annotation-png", anno.is_file() and anno.stat().st_size > 0, str(anno))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "out" / "source.dxf"
        src.parent.mkdir(parents=True)
        shutil.copy2(MALL, src)
        before = _sha(src)
        _ok("mall-sha-prefix", before.startswith("1d35a370"), before[:16])
        proj = _proj(root, src)

        after_png = PREVIEW_DIR / "after-plan-preview.png"
        preview = apply.render_plan_preview(proj, plan, region, after_png)
        _ok("mall-preview-png", after_png.is_file() and after_png.stat().st_size > 1000, str(after_png.stat().st_size))
        _ok("mall-preview-no-write", _sha(src) == before and not apply.is_dirty(proj))
        _ok("mall-preview-ok-count", preview["ok_count"] >= 16, str(preview["ok_count"]))
        rots = [r.get("rot") for r in preview["results"] if r.get("action") == "insert"]
        _ok("mall-rot-adjusted", all(abs((float(r) % 360) - 270) < 1 for r in rots), str(rots))
        _ok("mall-layers", all(str(r.get("layer") or "").startswith("图腾-新增-") for r in preview["results"] if r.get("action") == "insert"))

        # Stall and accessible close-ups from the same temp apply (re-apply in memory for crops).
        doc = apply._read_doc(src, "utf-8")
        apply.apply_mutations(doc, plan, bbox=REGION_BBOX, layer="图腾-新增-1")
        stall_xy = SQUATS[0]
        stall_bb = [stall_xy[0] - 900, stall_xy[1] - 600, stall_xy[0] + 400, stall_xy[1] + 600]
        acc_bb = [ACCESS[0] - 700, ACCESS[1] - 800, ACCESS[0] + 900, ACCESS[1] + 500]
        cad_render.render_doc_preview(doc, stall_bb, PREVIEW_DIR / "after-stall.png")
        cad_render.render_doc_preview(doc, acc_bb, PREVIEW_DIR / "after-accessible.png")
        cad_render.render_doc_preview(doc, [2967200, 506400, 2969200, 514400], PREVIEW_DIR / "after-stalls-row.png")
        order = list(doc.modelspace().entities_in_redraw_order())
        def _first(pred):
            for i, ent in enumerate(order):
                if pred(ent):
                    return i
            return None
        cover_i = _first(lambda e: e.dxftype() == "WIPEOUT")
        pipe_i = _first(lambda e: e.dxftype() == "LWPOLYLINE" and getattr(e.dxf, "layer", "") == "PIPE-污水")
        _ok("pipes-after-covers", cover_i is not None and pipe_i is not None and cover_i < pipe_i, f"cover={cover_i} pipe={pipe_i}")
        from PIL import Image
        stall_img = Image.open(PREVIEW_DIR / "after-stall.png")
        sw, sh = stall_img.size
        # Vertical soil stack at x=2968111.6 should stay visible through the first stall.
        yellow = 0
        for y_world in [stall_xy[1] + d for d in range(-250, 251, 5)]:
            px, py = cad_render.world_to_pixel(2968111.6, y_world, stall_bb, sw, sh)
            for dx in range(-8, 9):
                xx, yy = int(px + dx), int(py)
                if 0 <= xx < sw and 0 <= yy < sh:
                    pix = stall_img.getpixel((xx, yy))
                    r, g, b = pix[0], pix[1], pix[2]
                    if r > 200 and g > 200 and b < 90:
                        yellow += 1
        _ok("pipe-visible-through-stall", yellow >= 80, f"yellow={yellow} size={sw}x{sh}")
        # Copy the earlier original stall/accessible if present from /tmp, else render from source.
        orig = apply._read_doc(src, "utf-8")
        cad_render.render_doc_preview(orig, stall_bb, PREVIEW_DIR / "before-stall.png")
        cad_render.render_doc_preview(orig, acc_bb, PREVIEW_DIR / "before-accessible.png")
        _ok("stall-after", (PREVIEW_DIR / "after-stall.png").stat().st_size > 0)

        result = apply.apply_plan(proj, plan, region=region, message_id="e11cf2a2c5d8")
        _ok("mall-confirm-ok", result["ok"] and result["can_undo"])
        _ok("mall-source-changed", _sha(src) != before)
        # Accessible seated toilet still present at original location as a nested insert in the host.
        work_doc = ezdxf.readfile(str(apply.work_path(proj)))
        found = apply.find_symbol(work_doc, work_doc.modelspace(), SEAT, ACCESS, REGION_BBOX)
        _ok("accessible-untouched-nested", found is not None and found["kind"] == "overlay", str(found and found.get("kind")))
        # Host definition still contains the 8 squats (not exploded / edited).
        host_squats = sum(
            1
            for e in work_doc.blocks.get("A$C59327A70")
            if e.dxftype() == "INSERT" and e.dxf.name == SQUAT
        )
        _ok("host-still-has-8-squats", host_squats == 8, str(host_squats))
        # 8 new seated inserts on the new layer, and 8 covers.
        new_seats = [
            e
            for e in work_doc.modelspace().query("INSERT")
            if e.dxf.name == SEAT and str(e.dxf.layer).startswith("图腾-新增-")
        ]
        _ok("eight-new-seats", len(new_seats) == 8, str(len(new_seats)))
        covers = [
            e
            for e in work_doc.modelspace()
            if e.dxftype() in {"WIPEOUT", "HATCH", "SOLID"} and str(getattr(e.dxf, "layer", "")).startswith("图腾-新增-")
        ]
        _ok("eight-covers", len(covers) == 8, f"{len(covers)} {[e.dxftype() for e in covers]}")

        undone = apply.undo_last(proj)
        _ok("mall-undo-bytes", undone.get("restored_official") and _sha(src) == before)
        _ok("mall-golden-untouched", _sha(MALL) == before)



def _crop_world(img, view_bbox, world_bb, pad=None):
    from app import cad_render

    if pad is None:
        pad = cad_render.PAD
    w, h = img.size
    x0, y0 = cad_render.world_to_pixel(world_bb[0], world_bb[3], view_bbox, w, h, pad)
    x1, y1 = cad_render.world_to_pixel(world_bb[2], world_bb[1], view_bbox, w, h, pad)
    left, right = sorted((int(x0), int(x1)))
    top, bottom = sorted((int(y0), int(y1)))
    left = max(0, left)
    top = max(0, top)
    right = min(w, right)
    bottom = min(h, bottom)
    return img.crop((left, top, right, bottom))


def _mean_abs(a, b) -> float:
    from PIL import ImageChops

    a = a.convert("RGB")
    b = b.convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    hist = diff.histogram()
    total = 0
    count = 0
    for channel in range(3):
        for i in range(256):
            n = hist[channel * 256 + i]
            total += n * i
            count += n
    return total / max(count, 1)


def _count_yellow(img, view_bbox, x, y0, y1, step=5, dx_range=8, pad=None) -> int:
    from app import cad_render

    if pad is None:
        pad = cad_render.PAD
    w, h = img.size
    yellow = 0
    y = y0
    while y <= y1:
        px, py = cad_render.world_to_pixel(x, y, view_bbox, w, h, pad)
        for dx in range(-dx_range, dx_range + 1):
            xx, yy = int(px + dx), int(py)
            if 0 <= xx < w and 0 <= yy < h:
                pix = img.getpixel((xx, yy))
                r, g, b = pix[0], pix[1], pix[2]
                if r > 200 and g > 200 and b < 90:
                    yellow += 1
        y += step
    return yellow


def _pdf_info(path: Path) -> dict:
    data = path.read_bytes()
    if not data.startswith(b"%PDF"):
        raise AssertionError(f"not a PDF: {data[:16]!r}")
    import re

    box = re.search(rb"/MediaBox\s*\[\s*([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*\]", data)
    width = re.search(rb"/Width\s+(\d+)", data)
    height = re.search(rb"/Height\s+(\d+)", data)
    return {
        "size": path.stat().st_size,
        "mediabox": [float(x) for x in box.groups()] if box else None,
        "pixel": (int(width.group(1)), int(height.group(1))) if width and height else None,
    }


def _img_info(path: Path) -> str:
    from PIL import Image

    path = Path(path)
    with Image.open(path) as im:
        w, h = im.size
    return f"{w}x{h}px {path.stat().st_size}B"


def _has_preview_stamp(img) -> bool:
    """The corner ticket uses a pale gray fill around (232, 237, 240)."""
    w, h = img.size
    sample = img.crop((0, 0, min(280, w), min(100, h))).convert("RGB")
    hits = 0
    for pix in sample.getdata():
        r, g, b = pix[0], pix[1], pix[2]
        if abs(r - 232) <= 18 and abs(g - 237) <= 18 and abs(b - 240) <= 18:
            hits += 1
    return hits >= 40


def _count_red_markup(img) -> int:
    sample = img.convert("RGB").resize((min(400, img.size[0]), min(200, img.size[1])))
    red = 0
    for pix in sample.getdata():
        r, g, b = pix[0], pix[1], pix[2]
        if r > 150 and g < 90 and b < 90:
            red += 1
    return red


def _bbox_contains(box, xy, pad: float = 0.0) -> bool:
    return box[0] - pad <= xy[0] <= box[2] + pad and box[1] - pad <= xy[1] <= box[3] + pad


def _bbox_area(box) -> float:
    return max(float(box[2]) - float(box[0]), 0) * max(float(box[3]) - float(box[1]), 0)


def _stall_patch_diff(after_img, before_img, view_bbox, xy, half: float = 350, pad=0) -> float:
    world = [xy[0] - half, xy[1] - half, xy[0] + half, xy[1] + half]
    return _mean_abs(
        _crop_world(after_img, view_bbox, world, pad=pad),
        _crop_world(before_img, view_bbox, world, pad=pad),
    )


def _base_crop(base_img, view_bbox, dirty, dest: Path):
    from app import cad_render

    w, h = base_img.size
    box = cad_render.dirty_pixel_box(dirty, view_bbox, w, h)
    if not box:
        raise AssertionError(f"no pixel box for {dirty}")
    crop = base_img.crop(box).convert("RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dest, "PNG")
    return crop


def test_incremental_composite_pdf_and_sse() -> None:
    import time
    import tempfile
    from PIL import Image
    from app import apply, cad_render, projects, main as app_main

    _ok("mall-present-inc", MALL.is_file(), str(MALL))
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    region = {"id": REGION_ID, "bbox": REGION_BBOX}
    plan = _mall_plan(rot=90)
    plan7 = _mall_plan(rot=90, squats=SQUATS[:-1])
    _ok("seven-plan-omits-last", len(plan7["replaces"]) == 7 and abs(SQUATS[-1][1] - 506832.555) < 0.01)
    stall_xy = SQUATS[0]
    stall_bb = [stall_xy[0] - 900, stall_xy[1] - 600, stall_xy[0] + 400, stall_xy[1] + 600]
    acc_bb = [ACCESS[0] - 700, ACCESS[1] - 800, ACCESS[0] + 900, ACCESS[1] + 500]
    region_w = REGION_BBOX[2] - REGION_BBOX[0]
    region_area = _bbox_area(REGION_BBOX)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "out" / "source.dxf"
        src.parent.mkdir(parents=True)
        shutil.copy2(MALL, src)
        before = _sha(src)
        _ok("inc-sha-prefix", before.startswith("1d35a370"), before[:16])
        proj = _proj(root, src)

        # Warm the base cache (used by PDF / full-sheet comparison, not by crop preview).
        t_cache0 = time.perf_counter()
        cached = cad_render.base_preview(src, REGION_BBOX, encoding="utf-8")
        t_cache = time.perf_counter() - t_cache0
        shutil.copy2(cached, PREVIEW_DIR / "before-region.png")
        _ok("base-cache-warm", Path(cached).is_file() and Path(cached).stat().st_size > 0, f"{Path(cached).stat().st_size} cache={t_cache:.2f}s")

        crop8_png = PREVIEW_DIR / "after-plan-crop.png"
        t0 = time.perf_counter()
        preview = apply.render_plan_preview(proj, plan, region, crop8_png)
        t_crop8 = time.perf_counter() - t0
        _ok("crop8-preview-png", crop8_png.is_file() and crop8_png.stat().st_size > 1000, _img_info(crop8_png))
        _ok("crop8-flag", preview.get("crop") is True, str(preview.get("crop")))
        _ok("crop8-not-incremental", preview.get("incremental") is False, str(preview.get("incremental")))
        _ok("crop8-no-write", _sha(src) == before and not apply.is_dirty(proj))
        dirty8 = preview.get("dirty_bbox") or []
        view8 = preview.get("crop_bbox") or dirty8
        _ok(
            "crop8-bbox-narrow",
            len(dirty8) == 4
            and (dirty8[2] - dirty8[0]) < 0.4 * region_w
            and _bbox_area(dirty8) < 0.5 * region_area,
            str(dirty8),
        )
        missing = [xy for xy in SQUATS if not _bbox_contains(dirty8, xy)]
        _ok("crop8-contains-eight-stalls", missing == [], str(missing))
        _ok("crop8-excludes-accessible", not _bbox_contains(dirty8, ACCESS), f"dirty={dirty8} access={ACCESS}")

        crop7_png = PREVIEW_DIR / "after-plan-seven-crop.png"
        t7 = time.perf_counter()
        preview7 = apply.render_plan_preview(proj, plan7, region, crop7_png)
        t_crop7 = time.perf_counter() - t7
        _ok("crop7-preview-png", crop7_png.is_file() and crop7_png.stat().st_size > 1000, _img_info(crop7_png))
        _ok("crop7-flag", preview7.get("crop") is True, str(preview7.get("crop")))
        _ok("crop7-ok-count", preview7.get("ok_count", 0) >= 14, str(preview7.get("ok_count")))
        dirty7 = preview7.get("dirty_bbox") or []
        view7 = preview7.get("crop_bbox") or dirty7
        _ok(
            "crop7-bbox-narrow",
            len(dirty7) == 4
            and (dirty7[2] - dirty7[0]) < 0.4 * region_w
            and _bbox_area(dirty7) < 0.5 * region_area,
            str(dirty7),
        )
        missing7 = [xy for xy in SQUATS[:-1] if not _bbox_contains(dirty7, xy)]
        _ok("crop7-contains-seven-stalls", missing7 == [], str(missing7))

        full_png = PREVIEW_DIR / "after-plan-preview.png"
        t1 = time.perf_counter()
        full = apply.render_plan_preview(proj, plan, region, full_png, incremental=False)
        t_full = time.perf_counter() - t1
        _ok("full-preview-png", full_png.is_file() and full_png.stat().st_size > 1000, _img_info(full_png))
        _ok("full-not-incremental", full.get("incremental") is False)
        _ok("full-not-crop", full.get("crop") is False, str(full.get("crop")))
        print(
            f"TIMING crop8={t_crop8:.2f}s crop7={t_crop7:.2f}s full={t_full:.2f}s cache_build={t_cache:.2f}s",
            flush=True,
        )
        print(
            f"SIZES crop8={_img_info(crop8_png)} crop7={_img_info(crop7_png)} full={_img_info(full_png)}",
            flush=True,
        )
        _ok(
            "crop8-faster-than-full",
            t_crop8 < t_full * 0.55 or (t_full > 5 and t_crop8 * 2 < t_full),
            f"crop8={t_crop8:.2f}s full={t_full:.2f}s",
        )
        _ok(
            "crop7-faster-than-full",
            t_crop7 < t_full * 0.55 or (t_full > 5 and t_crop7 * 2 < t_full),
            f"crop7={t_crop7:.2f}s full={t_full:.2f}s",
        )
        crop8_wh = Image.open(crop8_png).size
        full_wh = Image.open(full_png).size
        _ok(
            "crop8-smaller-than-full-sheet",
            crop8_wh[0] < full_wh[0] * 0.7 or crop8_wh[0] * crop8_wh[1] < full_wh[0] * full_wh[1] * 0.85,
            f"crop8={_img_info(crop8_png)} full={_img_info(full_png)}",
        )

        crop8_img = Image.open(crop8_png)
        crop7_img = Image.open(crop7_png)
        base_img = Image.open(cached)
        before8 = PREVIEW_DIR / "before-plan-crop.png"
        before7 = PREVIEW_DIR / "before-plan-seven-crop.png"
        before8_img = _base_crop(base_img, REGION_BBOX, dirty8, before8)
        before7_img = _base_crop(base_img, REGION_BBOX, dirty7, before7)
        _ok(
            "crop8-matches-base-size",
            crop8_img.size == before8_img.size,
            f"after={crop8_img.size} before={before8_img.size} crop_bbox={view8}",
        )

        diffs8 = [_stall_patch_diff(crop8_img, before8_img, view8, xy) for xy in SQUATS]
        _ok("crop8-stalls-changed", all(d > 8 for d in diffs8), " ".join(f"{d:.2f}" for d in diffs8))
        diffs7 = [_stall_patch_diff(crop7_img, before7_img, view7, xy) for xy in SQUATS[:-1]]
        _ok("crop7-seven-stalls-changed", all(d > 8 for d in diffs7), " ".join(f"{d:.2f}" for d in diffs7))
        if _bbox_contains(dirty7, SQUATS[-1]):
            omitted = _stall_patch_diff(crop7_img, before7_img, view7, SQUATS[-1])
            _ok("crop7-omitted-stall-unchanged", omitted < 8, f"mean_abs={omitted:.3f}")

        yellow = _count_yellow(crop8_img, view8, 2968111.6, stall_xy[1] - 250, stall_xy[1] + 250, pad=0)
        _ok("crop8-pipe-visible-through-stall", yellow >= 80, f"yellow={yellow}")
        _ok("crop8-has-stamp", _has_preview_stamp(crop8_img))
        _ok("crop7-has-stamp", _has_preview_stamp(crop7_img))
        red8 = _count_red_markup(crop8_img)
        red7 = _count_red_markup(crop7_img)
        _ok("crop8-no-red-x-markup", red8 < 80, f"red={red8}")
        _ok("crop7-no-red-x-markup", red7 < 80, f"red={red7}")

        # Accessible 坐便 is outside the dirty crop: check the in-memory DXF and a dedicated crop.
        orig = apply._read_doc(src, "utf-8")
        doc = apply._read_doc(src, "utf-8")
        apply.apply_mutations(doc, plan, bbox=REGION_BBOX, layer="图腾-新增-1")
        found = apply.find_symbol(doc, doc.modelspace(), SEAT, ACCESS, REGION_BBOX)
        _ok("accessible-untouched-dxf", found is not None and found["kind"] == "overlay", str(found and found.get("kind")))
        acc_after = PREVIEW_DIR / "after-accessible.png"
        acc_before = PREVIEW_DIR / "before-accessible.png"
        cad_render.render_doc_preview(doc, acc_bb, acc_after)
        cad_render.render_doc_preview(orig, acc_bb, acc_before)
        acc_diff = _mean_abs(Image.open(acc_after), Image.open(acc_before))
        _ok("accessible-crop-unchanged", acc_diff < 6, f"mean_abs={acc_diff:.3f}")

        # Close-up stall contract still holds on a dedicated render (not the plan-preview crop scale).
        cad_render.render_doc_preview(doc, stall_bb, PREVIEW_DIR / "after-stall.png")
        stall_img = Image.open(PREVIEW_DIR / "after-stall.png")
        sw, sh = stall_img.size
        yellow_close = 0
        for y_world in [stall_xy[1] + d for d in range(-250, 251, 5)]:
            px, py = cad_render.world_to_pixel(2968111.6, y_world, stall_bb, sw, sh)
            for dx in range(-8, 9):
                xx, yy = int(px + dx), int(py)
                if 0 <= xx < sw and 0 <= yy < sh:
                    pix = stall_img.getpixel((xx, yy))
                    r, g, b = pix[0], pix[1], pix[2]
                    if r > 200 and g > 200 and b < 90:
                        yellow_close += 1
        _ok("pipe-visible-through-stall", yellow_close >= 80, f"yellow={yellow_close} size={sw}x{sh}")

        # Pending-plan PDF must be the full current sheet, not the dirty crop.
        pdf_path = PREVIEW_DIR / "after-plan-preview.pdf"
        projects.append_chat(
            proj,
            [
                {
                    "id": "pending-preview-1",
                    "role": "assistant",
                    "text": plan["reply"],
                    "plan": plan,
                    "region": REGION_ID,
                    "overlay": None,
                }
            ],
        )
        t_pdf0 = time.perf_counter()
        pdf_result = apply.export_sheet_pdf(proj, region, pdf_path)
        t_pdf = time.perf_counter() - t_pdf0
        info = _pdf_info(pdf_path)
        print(
            f"PDF pending={pdf_result.get('pending')} size={info['size']} mediabox={info['mediabox']} pixel={info['pixel']} time={t_pdf:.2f}s",
            flush=True,
        )
        _ok("pdf-pending-post-edit", pdf_result.get("pending") is True and pdf_result.get("stamp") is True)
        _ok("pdf-real-file", info["size"] > 20_000, str(info["size"]))
        px = info["pixel"] or (0, 0)
        box = info["mediabox"] or [0, 0, 0, 0]
        page_w = box[2] - box[0]
        page_h = box[3] - box[1]
        _ok(
            "pdf-page-size",
            (px[0] >= 800 and px[1] >= 400) or (page_w >= 400 and page_h >= 200),
            f"pixel={px} mediabox={box}",
        )
        # Full sheet is landscape; the dirty stall crop is a tall strip.
        full_w, full_h = Image.open(full_png).size
        crop_w, crop_h = crop8_img.size
        pdf_w, pdf_h = px if px[0] and px[1] else (page_w, page_h)
        _ok(
            "pdf-is-full-sheet-not-crop",
            pdf_w > pdf_h and pdf_w >= min(full_w * 0.7, 1200) and pdf_w > crop_w * 1.15,
            f"pdf={px} mediabox={box} full={full_w}x{full_h} crop8={crop_w}x{crop_h}",
        )
        _ok("pdf-source-untouched", _sha(src) == before)

        # SSE: done is emitted before the overlay file exists; preview comes after.
        leftover = cad_render.overlay_path_for("test", "a-sse")
        leftover.unlink(missing_ok=True)
        overlay_dest = {"path": None, "existed_at_call": None}

        def fake_render(_proj, _plan, _region, dest):
            dest = Path(dest)
            overlay_dest["existed_at_call"] = dest.is_file() and dest.stat().st_size > 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(crop8_png.read_bytes())
            overlay_dest["path"] = dest
            return {"ok": True, "path": str(dest), "crop": True}

        user_msg = {"id": "u-sse", "role": "user", "text": "把蹲便改成坐便", "region": REGION_ID}
        assistant = {
            "id": "a-sse",
            "role": "assistant",
            "text": plan["reply"],
            "plan": plan,
            "region": REGION_ID,
            "mode": "plan",
        }
        events = list(
            app_main.plan_chat_events(proj, user_msg, assistant, region, renderer=fake_render)
        )
        _ok("sse-first-is-done", events and events[0]["type"] == "done", str([e["type"] for e in events]))
        _ok("sse-done-no-overlay", events[0]["message"].get("overlay") is None)
        _ok("sse-preview-pending", events[0].get("preview_pending") is True)
        _ok("sse-has-preview", any(e.get("type") == "preview" for e in events), str([e["type"] for e in events]))
        _ok("sse-overlay-missing-at-render-start", overlay_dest["existed_at_call"] is False, str(overlay_dest))
        preview_ev = next(e for e in events if e.get("type") == "preview")
        _ok("sse-preview-id", preview_ev.get("overlay") == "a-sse")
        saved = projects.read_chat(proj)
        saved_asst = next(m for m in saved["messages"] if m.get("id") == "a-sse")
        _ok("sse-chat-overlay-updated", saved_asst.get("overlay") == "a-sse")
        _ok("sse-overlay-file", Path(overlay_dest["path"]).is_file() and Path(overlay_dest["path"]).stat().st_size > 0)


def main() -> int:
    test_reply_and_history()
    test_overlay_replace_and_undo()
    if MALL.is_file():
        test_mall_preview_and_writeback()
        test_incremental_composite_pdf_and_sse()
        print("ALL PASSED", flush=True)
    else:
        print(f"SKIP  mall fixture not found: {MALL}", flush=True)
        print("SKIP  set TUTENG_DXF_FIXTURE to a 万商城 DXF to run the mall tests", flush=True)
        print("DONE  remaining tests passed (2 mall tests skipped)", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(str(exc), flush=True)
        raise SystemExit(1)
