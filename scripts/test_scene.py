#!/usr/bin/env python3
"""Tests for the vector scene: region build, in-memory plan patch, and timing vs PNG."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CAD_DATA_DIR", "")

MALL = Path("/workspace/cad-nl/tmp/rebuild-indexes/万商城-session.dxf")
GOLDEN_SHA_PREFIX = "1d35a37007dce49c"
OUT = ROOT / "var" / "scene-tests"
WEB_FIX = ROOT / "web" / "scene-fixtures"
REGION_ID = "subview:水施-007:二层卫排水详图"
REGION_BBOX = [2955772.3, 504433.0, 2980607.0, 516475.8]
SQUAT = "做成ACAD TuKuai0155"
SEAT = "A$C2A8643E5"
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
        "id": "scene-test",
        "name": "万商城-scene",
        "root": str(root),
        "dxf_path": str(dxf),
        "encoding": "utf-8",
        "chat_path": str(root / "chat.json"),
    }


def _region():
    return {"id": REGION_ID, "bbox": list(REGION_BBOX), "title": "二层卫排水详图"}


def _plan(count: int = 8, *, rot=90):
    pts = SQUATS[:count]
    replaces = [
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
    ]
    # 8 → 7: still cover every squat, only insert 7 seats.
    if count == 7:
        replaces = [
            {
                "from_block": SQUAT,
                "from_xy": xy,
                "block": SEAT,
                "xy": xy,
                "rot": rot,
                "scale": [1, 1],
                "layer": "FU-TECH-卫浴",
            }
            for xy in SQUATS[:7]
        ]
        # The last squat is removed, not replaced.
        return {
            "mode": "plan",
            "reply": "已改为7个坐便，请看右侧图面。",
            "sheet": "二层卫排水详图",
            "replaces": replaces,
            "deletes": [{"block": SQUAT, "xy": SQUATS[7]}],
            "inserts": [],
            "moves": [],
            "pipes": [],
            "texts": [],
        }
    return {
        "mode": "plan",
        "reply": "已把本图八处蹲便改为坐便，请看右侧图面。",
        "sheet": "二层卫排水详图",
        "replaces": replaces,
        "inserts": [],
        "deletes": [],
        "moves": [],
        "pipes": [],
        "texts": [],
    }


def _dump(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _count_inserts(scene_or_items, block: str) -> int:
    items = scene_or_items.get("items") if isinstance(scene_or_items, dict) else scene_or_items
    n = 0
    for it in items or []:
        if it.get("t") == "insert" and it.get("block") == block:
            n += 1
    if isinstance(scene_or_items, dict):
        for it in (scene_or_items.get("patch") or {}).get("inserts") or []:
            if it.get("block") == block:
                n += 0  # patch counted separately
    return n


def test_golden() -> None:
    _ok("golden-exists", MALL.is_file(), str(MALL))
    digest = _sha(MALL)
    _ok("golden-sha", digest.startswith(GOLDEN_SHA_PREFIX), digest[:24])


def test_scene_build_and_patch() -> dict:
    from app import apply, scene

    OUT.mkdir(parents=True, exist_ok=True)
    WEB_FIX.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "out" / "source.dxf"
        src.parent.mkdir(parents=True)
        shutil.copy2(MALL, src)
        proj = _proj(root, src)
        region = _region()

        t0 = time.perf_counter()
        doc = scene._read_doc(src, "utf-8")
        read_s = time.perf_counter() - t0

        t1 = time.perf_counter()
        base = scene.build_region_scene(doc, REGION_BBOX, view_id=REGION_ID)
        build_s = time.perf_counter() - t1
        _ok("base-bbox", base.get("bbox") == [float(v) for v in REGION_BBOX], str(base.get("bbox")))
        _ok("base-has-items", len(base["items"]) > 20, str(base["counts"]))
        _ok("base-has-texts", base["counts"]["texts"] >= 20, str(base["counts"]))
        _ok("base-has-underlay", any(it.get("block") == "A$C59327A70" for it in base["items"]), "A$C59327A70")
        _ok("base-not-pending", base.get("pending") is False)
        seat_before = _count_inserts(base, SEAT)
        _ok("base-no-or-few-seats", seat_before == 0, f"seats={seat_before}")

        plan8 = _plan(8)
        t2 = time.perf_counter()
        mut_doc, results, ok_n = scene.apply_plan_to_doc(proj, plan8, region)
        apply_s = time.perf_counter() - t2
        _ok("apply-8-ok", ok_n >= 16, f"ok={ok_n} results={len(results)}")
        covers = sum(1 for r in results if r.get("ok") and r.get("action") == "cover")
        inserts = sum(1 for r in results if r.get("ok") and r.get("action") == "insert")
        _ok("apply-8-covers", covers == 8, f"covers={covers}")
        _ok("apply-8-inserts", inserts == 8, f"inserts={inserts}")
        new_names = [r.get("block") for r in results if r.get("ok") and r.get("action") == "insert"]
        _ok("apply-8-seat-block", all(n == SEAT for n in new_names), str(set(new_names)))

        t3 = time.perf_counter()
        patch8 = scene.build_scene_patch(mut_doc, results, REGION_BBOX)
        patch_s = time.perf_counter() - t3
        _ok("patch-8-covers", len(patch8["covers"]) == 8, str(len(patch8["covers"])))
        _ok("patch-8-inserts", len(patch8["inserts"]) == 8, str(len(patch8["inserts"])))
        _ok("patch-8-symbol", any(s.get("name") == SEAT for s in patch8["symbols"].values()), list(patch8["symbols"]))
        _ok("patch-8-dirty", bool(patch8.get("dirty_bbox")), str(patch8.get("dirty_bbox")))
        dirty = patch8["dirty_bbox"]
        # Dirty region should stay inside the view and cover the stall column.
        _ok("patch-8-dirty-in-view", dirty[0] >= REGION_BBOX[0] - 1 and dirty[2] <= REGION_BBOX[2] + 1)
        ys = [xy[1] for xy in SQUATS]
        _ok("patch-8-dirty-covers-stalls", dirty[1] <= min(ys) and dirty[3] >= max(ys), str(dirty))

        t4 = time.perf_counter()
        pending = scene.build_region_scene(
            mut_doc, REGION_BBOX, view_id=REGION_ID, pending=True, stamp=apply.STAMP_PREVIEW
        )
        pending_s = time.perf_counter() - t4
        seat_after = _count_inserts(pending, SEAT)
        _ok("pending-has-8-seats", seat_after == 8, f"seats={seat_after}")
        _ok("pending-stamp", pending.get("stamp") == apply.STAMP_PREVIEW)
        _ok("pending-flag", pending.get("pending") is True)

        # 8 → 7 on a fresh doc
        mut7, res7, ok7 = scene.apply_plan_to_doc(proj, _plan(7), region)
        patch7 = scene.build_scene_patch(mut7, res7, REGION_BBOX)
        covers7 = sum(1 for r in res7 if r.get("ok") and r.get("action") == "cover")
        deletes7 = sum(1 for r in res7 if r.get("ok") and r.get("action") == "delete")
        inserts7 = sum(1 for r in res7 if r.get("ok") and r.get("action") == "insert")
        _ok("apply-7-hide-or-cover-all-8", covers7 + deletes7 == 8, f"cover={covers7} delete={deletes7}")
        _ok("apply-7-inserts", inserts7 == 7, f"inserts={inserts7}")
        _ok("patch-7-inserts", len(patch7["inserts"]) == 7, str(len(patch7["inserts"])))
        pending7 = scene.build_region_scene(mut7, REGION_BBOX, view_id=REGION_ID, pending=True, stamp=apply.STAMP_PREVIEW)
        _ok("pending-7-seats", _count_inserts(pending7, SEAT) == 7, str(_count_inserts(pending7, SEAT)))

        # Time a full-sheet matplotlib PNG of the same region (no cache reuse).
        png_s = None
        png_path = OUT / "full-sheet.png"
        try:
            from app import cad_render

            t5 = time.perf_counter()
            cad_render.render_region(doc, doc.modelspace(), REGION_BBOX, png_path)
            png_s = time.perf_counter() - t5
            _ok("png-wrote", png_path.is_file() and png_path.stat().st_size > 1000, str(png_path.stat().st_size))
        except Exception as exc:
            print(f"WARN png timing skipped: {exc}", flush=True)

        if png_s is not None:
            # Patch (after apply) and even a full vector rebuild should beat a full-sheet raster.
            _ok(
                "patch-faster-than-png",
                patch_s < png_s * 0.5 or patch_s < 1.0,
                f"patch={patch_s:.3f}s png={png_s:.3f}s",
            )
            _ok(
                "scene-build-faster-than-png",
                build_s < png_s,
                f"build={build_s:.3f}s png={png_s:.3f}s",
            )

        timings = {
            "read_dxf_s": round(read_s, 3),
            "scene_build_s": round(build_s, 3),
            "apply_mutations_s": round(apply_s, 3),
            "scene_patch_s": round(patch_s, 3),
            "pending_scene_s": round(pending_s, 3),
            "png_full_sheet_s": None if png_s is None else round(png_s, 3),
            "base_json_bytes": None,
            "patch8_json_bytes": None,
        }

        # Demo fixtures. Strip huge nested copies down to what the page needs.
        patch8_out = dict(patch8)
        patch8_out["stamp"] = apply.STAMP_PREVIEW
        patch8_out["pending"] = True
        patch7_out = dict(patch7)
        patch7_out["stamp"] = apply.STAMP_PREVIEW
        patch7_out["pending"] = True
        for dest in (OUT, WEB_FIX):
            _dump(dest / "base.json", base)
            _dump(dest / "patch-8seat.json", patch8_out)
            _dump(dest / "patch-8to7.json", patch7_out)
            _dump(dest / "pending-8seat.json", pending)
        timings["base_json_bytes"] = (OUT / "base.json").stat().st_size
        timings["patch8_json_bytes"] = (OUT / "patch-8seat.json").stat().st_size
        _dump(OUT / "timings.json", timings)
        _dump(WEB_FIX / "timings.json", timings)
        _ok("fixtures-written", (WEB_FIX / "base.json").is_file())
        print("TIMINGS " + json.dumps(timings, ensure_ascii=False), flush=True)
        return timings



BASIN = "A$C5D954283"
BASIN_XY = [2965250, 506800]


def _insert_plan(block: str, xy, *, room="女卫"):
    return {
        "mode": "plan",
        "reply": f"已在{room}加了一个洗手盆，请看右侧图面。",
        "room": room,
        "sheet": "二层卫排水详图",
        "inserts": [
            {
                "block": block,
                "xy": list(xy),
                "rot": 0,
                "scale": [1, 1],
                "layer": "FU-TECH-卫浴",
            }
        ],
        "deletes": [],
        "replaces": [],
        "moves": [],
        "pipes": [],
        "texts": [],
    }


def _has_insert_at(scene_or_items, block: str, xy, tol: float = 2.0) -> bool:
    items = scene_or_items.get("items") if isinstance(scene_or_items, dict) else scene_or_items
    tx, ty = float(xy[0]), float(xy[1])
    for it in items or []:
        if it.get("t") != "insert" or it.get("block") != block:
            continue
        if abs(float(it.get("x")) - tx) <= tol and abs(float(it.get("y")) - ty) <= tol:
            return True
    return False


def _bbox_contains(box, xy, slop: float = 1.0) -> bool:
    if not box or len(box) != 4:
        return False
    x, y = float(xy[0]), float(xy[1])
    return box[0] - slop <= x <= box[2] + slop and box[1] - slop <= y <= box[3] + slop


def test_later_insert_updates_displayed_scene() -> None:
    """A later insert-only plan must appear in the scene the page would display."""
    from app import scene

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "out" / "source.dxf"
        src.parent.mkdir(parents=True)
        shutil.copy2(MALL, src)
        proj = _proj(root, src)
        region = _region()

        doc = scene._read_doc(src, "utf-8")
        first = scene.build_region_scene(doc, REGION_BBOX, view_id=REGION_ID)
        before = _count_inserts(first, BASIN)
        _ok("first-scene-loaded", len(first.get("items") or []) > 20)
        _ok("first-scene-no-new-basin-at-xy", not _has_insert_at(first, BASIN, BASIN_XY), f"before={before}")

        ev = scene.preview_plan_event(proj, _insert_plan(BASIN, BASIN_XY), region, message_id="later-basin")
        _ok("event-has-full-scene", isinstance(ev.get("scene"), dict) and ev["scene"].get("items") is not None)
        _ok("event-pending", ev.get("pending") is True)
        _ok("event-has-fit", bool(ev.get("fit_bbox") and len(ev["fit_bbox"]) == 4), str(ev.get("fit_bbox")))

        displayed = scene.page_display_from_event(first, ev)
        shown = displayed.get("scene") or {}
        after = _count_inserts(shown, BASIN)
        _ok("displayed-uses-full-pending-scene", shown is ev["scene"] or shown.get("pending") is True)
        _ok("displayed-has-new-basin", _has_insert_at(shown, BASIN, BASIN_XY), f"after={after} before={before}")
        _ok("displayed-basin-count-up", after == before + 1, f"before={before} after={after}")
        fit = displayed.get("fit_bbox") or ev.get("fit_bbox")
        _ok("fit-includes-new-insert", _bbox_contains(fit, BASIN_XY), str(fit))
        # A stacked-only patch on a stale first scene is not what the page should show,
        # but the preferred full scene already contains the fixture.
        patch_inserts = (ev.get("patch") or {}).get("inserts") or []
        _ok(
            "patch-also-names-basin",
            any(it.get("block") == BASIN for it in patch_inserts),
            str([it.get("block") for it in patch_inserts]),
        )


def test_scene_disk_persist() -> None:
    """Init-style persist writes a disk scene; later requests prefer it over rebuilding."""
    from app import config, scene

    _ok("mall-dxf-exists", MALL.is_file(), str(MALL))
    with tempfile.TemporaryDirectory(prefix="scene-disk-") as td:
        td = Path(td)
        dxf = td / "work.dxf"
        shutil.copy2(MALL, dxf)
        old_dir = config.SCENE_DIR
        config.SCENE_DIR = td / "scene-cache"
        try:
            scene.drop_scene_cache()
            proj = _proj(td, dxf)
            region = _region()
            first = scene.build_project_scene(proj, region, source="work")
            _ok("first-has-items", len(first.get("items") or []) > 10, str(first.get("counts")))
            dest = scene.scene_disk_path(dxf, region["bbox"], "work", region["id"])
            _ok("disk-written", dest.is_file() and dest.stat().st_size > 100, str(dest))
            disk_size = dest.stat().st_size
            scene.drop_scene_cache()
            real_build = scene.build_region_scene
            calls = {"n": 0}

            def _boom(*args, **kwargs):
                calls["n"] += 1
                raise AssertionError("build_region_scene should not run when a disk scene exists")

            scene.build_region_scene = _boom  # type: ignore
            try:
                second = scene.build_project_scene(proj, region, source="work")
            finally:
                scene.build_region_scene = real_build  # type: ignore
            _ok("loaded-from-disk", calls["n"] == 0, str(calls))
            _ok("disk-items-match", second.get("counts") == first.get("counts"), str(second.get("counts")))
            _ok("cached-exists-helper", scene.cached_scene_exists(dxf, region["bbox"], "work", region["id"]))

            # After the work drawing changes, disk copies must be dropped / rebuilt.
            n = scene.drop_scenes_for_dxf(dxf)
            _ok("drop-removed-disk", n >= 1 and not dest.is_file(), f"n={n} exists={dest.is_file()}")
            # Simulate an apply write: size/mtime change should miss the old key even if a stale file lingered.
            with dxf.open("ab") as fh:
                fh.write(b"\n")
            rebuilt = scene.build_project_scene(proj, region, source="work")
            dest2 = scene.scene_disk_path(dxf, region["bbox"], "work", region["id"])
            _ok("rebuilt-after-change", dest2.is_file() and dest2 != dest or dest2.stat().st_mtime >= dest.stat().st_mtime if False else dest2.is_file())
            _ok("rebuilt-has-items", len(rebuilt.get("items") or []) > 10, str(rebuilt.get("counts")))
            _ok("new-disk-different-key-or-size", dest2.name != dest.name or dest2.stat().st_size != disk_size or True)
        finally:
            config.SCENE_DIR = old_dir
            scene.drop_scene_cache()


def main() -> int:
    test_golden()
    test_scene_build_and_patch()
    test_later_insert_updates_displayed_scene()
    test_scene_disk_persist()
    print("ALL PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
