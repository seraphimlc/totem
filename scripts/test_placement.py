#!/usr/bin/env python3
"""坐标校验—吸附—回喂闭环的自测。

用 ezdxf 在临时目录里造一张最小图纸，不依赖网络、不依赖 data/ 下的真实图纸。
沿用其它脚本的写法：直接 assert，失败抛 AssertionError。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402

from app import apply, placement  # noqa: E402
from app.prompts import build_repair_message  # noqa: E402


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""), flush=True)


FIXTURE_BBOX = [0.0, 0.0, 10000.0, 8000.0]
ROOM_BBOX = [1000.0, 1000.0, 9000.0, 7000.0]
# 房间分区：bbox 就是房间净空（与 projects.iter_regions 的做法一致）。
REGION = {"id": "room:test:女卫", "kind": "room", "title": "女卫", "bbox": ROOM_BBOX}
INDEX = {"rooms": [{"name": "女卫", "world_xy": [5000.0, 4000.0], "bbox": ROOM_BBOX}]}


def _make_doc():
    """最小图纸：图框、房间名、LINE + LWPOLYLINE 两段墙、两个同块名 INSERT、
    一条管线、一段文字。块内图形中心故意偏离基点（基点 0,0，图形在 +x 一侧）。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (10000, 0), (10000, 8000), (0, 8000)], close=True, dxfattribs={"layer": "图框"}
    )
    msp.add_text("女卫", dxfattribs={"layer": "房间名", "height": 300}).set_placement((5000, 7500))
    # 两段墙：LINE 走横向，LWPOLYLINE 走纵向。
    msp.add_line((1000, 4000), (9000, 4000), dxfattribs={"layer": "WALL-内墙"})
    msp.add_lwpolyline([(5000, 1000), (5000, 7000)], dxfattribs={"layer": "墙-隔墙"})
    # 同块名符号：块内图形中心在 (300, 0)，基点 (0, 0)，所以插入点比图形中心偏左 300。
    block = doc.blocks.new("TOILET")
    block.add_lwpolyline([(100, -150), (500, -150), (500, 150), (100, 150)], close=True)
    msp.add_blockref("TOILET", insert=(2000, 2000), dxfattribs={"layer": "FF-洁具"})
    msp.add_blockref("TOILET", insert=(3000, 2000), dxfattribs={"layer": "FF-洁具"})
    # 管线与文字，校验时只核坐标。
    msp.add_lwpolyline([(1500, 5500), (4000, 5500)], dxfattribs={"layer": "PIPE-污水", "const_width": 25})
    return doc


def _codes(issues):
    return [(i["code"], i["severity"]) for i in issues]


def test_symbol_bbox_and_clear_rect() -> None:
    doc = _make_doc()
    bbox = placement.symbol_world_bbox(doc, "TOILET", (2000, 2000))
    _ok("world-bbox", bbox is not None and abs(bbox[0] - 2100) < 1e-6 and abs(bbox[2] - 2500) < 1e-6, str(bbox))
    rot = placement.symbol_world_bbox(doc, "TOILET", (2000, 2000), rot=90)
    _ok(
        "world-bbox-rotated",
        rot is not None and abs((rot[2] - rot[0]) - 300) < 1e-6 and abs((rot[3] - rot[1]) - 400) < 1e-6,
        str(rot),
    )
    _ok("world-bbox-missing-block", placement.symbol_world_bbox(doc, "NOPE", (0, 0)) is None)

    clear = placement.room_clear_rect(REGION, INDEX)
    _ok("clear-uses-room-bbox", clear == tuple(ROOM_BBOX), str(clear))
    # 拿不到房间记录时退回分区 bbox。
    _ok(
        "clear-fallback-region",
        placement.room_clear_rect({"kind": "sheet", "bbox": FIXTURE_BBOX}, {}) == tuple(FIXTURE_BBOX)
    )
    # 连 bbox 都没有时按 world_xy ± 6000 退化。
    fallback = placement.room_clear_rect({"kind": "room", "title": "洗手间", "world_xy": [100, 200]}, None)
    _ok("clear-fallback-world-xy", fallback == (-5900.0, -5800.0, 6100.0, 6200.0), str(fallback))


def test_snap() -> None:
    doc = _make_doc()
    bbox = FIXTURE_BBOX

    xy, notes = placement.snap_xy(
        doc, {}, block="TOILET", xy=[10500.0, 1000.0], bbox=bbox, region=REGION, index=INDEX
    )
    _ok("snap-clamp-moves", xy != [10500.0, 1000.0] and notes, f"{xy} {notes}")
    _ok("snap-clamp-inside-room", ROOM_BBOX[0] <= xy[0] <= ROOM_BBOX[2] and ROOM_BBOX[1] <= xy[1] <= ROOM_BBOX[3], str(xy))
    _ok("snap-clamp-note-has-number", any("收回" in n and str(round(xy[0])) in n for n in notes), str(notes))

    far, far_notes = placement.snap_xy(
        doc, {}, block="TOILET", xy=[90000.0, 1000.0], bbox=bbox, region=REGION, index=INDEX
    )
    _ok("snap-far-untouched", far == [90000.0, 1000.0] and far_notes == [], f"{far} {far_notes}")

    # 图形中心在插入点 +300；新点落在已有符号 300mm 内 → 按同类基点口径反推。
    off, off_notes = placement.snap_xy(
        doc, {}, block="TOILET", xy=[2300.0, 2050.0], bbox=bbox, region=REGION, index=INDEX
    )
    _ok("snap-same-kind-offset", off == [2300.0, 2000.0] and off_notes, f"{off} {off_notes}")
    _ok("snap-same-kind-note", any("同类符号" in n for n in off_notes), str(off_notes))

    # 离得很远时不吸附。
    clean, clean_notes = placement.snap_xy(
        doc, {}, block="TOILET", xy=[7000.0, 6000.0], bbox=bbox, region=REGION, index=INDEX
    )
    _ok("snap-noop", clean == [7000.0, 6000.0] and clean_notes == [], f"{clean} {clean_notes}")

    # 模块级开关关掉后一律不动。
    placement.SNAP_ENABLED = False
    try:
        raw, raw_notes = placement.snap_xy(
            doc, {}, block="TOILET", xy=[10500.0, 1000.0], bbox=bbox, region=REGION, index=INDEX
        )
    finally:
        placement.SNAP_ENABLED = True
    _ok("snap-switch-off", raw == [10500.0, 1000.0] and raw_notes == [], f"{raw} {raw_notes}")


def test_check_plan() -> None:
    doc = _make_doc()
    plan = {
        "inserts": [
            {"block": "TOILET", "xy": [3000.0, 4000.0]},   # 压 LINE 墙
            {"block": "TOILET", "xy": [2000.0, 2000.0]},   # 与已有符号重叠
            {"block": "TOILET", "xy": [30000.0, 2000.0]},  # 跑到分区外
            {"block": "NOPE", "xy": [7000.0, 6000.0]},     # 块不存在 → 只 warn
        ],
        "texts": [{"text": "地漏", "xy": [500.0, 500.0]}],
    }
    issues = placement.check_plan(doc, plan, bbox=FIXTURE_BBOX, region=REGION, index=INDEX)
    by_index = {}
    for item in issues:
        by_index.setdefault((item["op"], item["index"]), []).append(item)
    _ok("issue-shape", all({"code", "severity", "message", "op", "index", "block", "xy"} <= set(i) for i in issues))
    _ok(
        "issue-over-wall",
        any(i["code"] == "over_wall" and i["severity"] == "error" for i in by_index.get(("insert", 0), [])),
        str(_codes(by_index.get(("insert", 0), []))),
    )
    _ok(
        "issue-overlap-symbol",
        any(i["code"] == "overlap_symbol" and i["severity"] == "error" for i in by_index.get(("insert", 1), [])),
        str(_codes(by_index.get(("insert", 1), []))),
    )
    _ok(
        "issue-out-of-region",
        any(i["code"] == "out_of_region" and i["severity"] == "error" for i in by_index.get(("insert", 2), [])),
        str(_codes(by_index.get(("insert", 2), []))),
    )
    warn = by_index.get(("insert", 3), [])
    _ok("issue-missing-size-warn", warn and all(i["severity"] == "warn" for i in warn), str(_codes(warn)))
    _ok("issue-missing-size-note", any("缺少块尺寸" in i["message"] for i in warn), str(warn))
    # 文字落在房间净空外也要报。
    text_issues = by_index.get(("text", 0), [])
    _ok(
        "issue-text-outside-room",
        any(i["code"] == "outside_room" and i["severity"] == "error" for i in text_issues),
        str(_codes(text_issues)),
    )

    # 同一方案里同块名的重复落点。
    dup_plan = {
        "inserts": [
            {"block": "TOILET", "xy": [7000.0, 6000.0]},
            {"block": "TOILET", "xy": [7000.0, 6000.0]},
        ]
    }
    dup = placement.check_plan(doc, dup_plan, bbox=FIXTURE_BBOX, region=REGION, index=INDEX)
    _ok("issue-duplicate", any(i["code"] == "duplicate" for i in dup), str(_codes(dup)))

    # 替换：把已有符号原位替换不应该被判成重叠（旧符号会被移走）。
    replace_plan = {
        "replaces": [
            {"from_block": "TOILET", "from_xy": [2000.0, 2000.0], "block": "TOILET", "xy": [2000.0, 2000.0]}
        ]
    }
    replace_issues = placement.check_plan(doc, replace_plan, bbox=FIXTURE_BBOX, region=REGION, index=INDEX)
    _ok(
        "replace-in-place-not-overlap",
        not any(i["code"] == "overlap_symbol" for i in replace_issues),
        str(_codes(replace_issues)),
    )

    # 外框算不出来时不抛异常。
    weird = placement.check_plan(
        None, {"inserts": [{"block": "X", "xy": [1, 2]}]}, bbox=FIXTURE_BBOX, region=REGION, index=INDEX
    )
    _ok("check-plan-no-doc", isinstance(weird, list), str(weird))


def _proj(root: Path, dxf: Path) -> dict:
    return {
        "id": "placement-test",
        "name": "placement-test",
        "root": str(root),
        "dxf_path": str(dxf),
        "encoding": "utf-8",
        "chat_path": str(root / "chat.json"),
    }


def test_apply_end_to_end() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "out" / "source.dxf"
        src.parent.mkdir(parents=True)
        doc = _make_doc()
        doc.saveas(str(src))

        # 1) apply_mutations：越界点被夹回，结果里带 snapped / snap_notes / xy_planned。
        doc2 = ezdxf.readfile(str(src))
        plan = {
            "mode": "plan",
            "reply": "加两处地漏。",
            "inserts": [
                {"block": "TOILET", "xy": [10500.0, 1500.0], "rot": 0},
                {"block": "TOILET", "xy": [2300.0, 2050.0], "rot": 0},
                {"block": "TOILET", "xy": [7000.0, 6000.0], "rot": 0},
            ],
        }
        results = apply.apply_mutations(doc2, plan, bbox=FIXTURE_BBOX, layer="图腾-新增-1", region=REGION)
        inserts = [r for r in results if r.get("action") == "insert"]
        _ok("apply-three-inserts", len(inserts) == 3, str(len(inserts)))
        _ok("apply-all-ok", all(r.get("ok") for r in inserts), str([r.get("reason") for r in inserts]))
        clamped = inserts[0]
        _ok("apply-clamped-snapped", clamped.get("snapped") is True and clamped.get("snap_notes"), str(clamped))
        _ok("apply-clamped-xy-planned", clamped.get("xy_planned") == [10500.0, 1500.0], str(clamped.get("xy_planned")))
        _ok("apply-clamped-xy-inside", ROOM_BBOX[0] <= clamped["xy"][0] <= ROOM_BBOX[2], str(clamped["xy"]))
        offset = inserts[1]
        _ok("apply-offset-snapped", offset.get("snapped") is True and offset["xy"] == [2300.0, 2000.0], str(offset))
        _ok("apply-offset-xy-planned", offset.get("xy_planned") == [2300.0, 2050.0], str(offset.get("xy_planned")))
        plain = inserts[2]
        _ok("apply-plain-not-snapped", not plain.get("snapped") and "xy_planned" not in plain, str(plain))

        # 落进图里的 insert 坐标要和 results 自洽。
        placed = {
            (round(e.dxf.insert.x, 3), round(e.dxf.insert.y, 3))
            for e in doc2.modelspace().query("INSERT")
            if str(e.dxf.layer).startswith("图腾-新增")
        }
        _ok(
            "apply-doc-matches-results",
            placed == {(round(r["xy"][0], 3), round(r["xy"][1], 3)) for r in inserts},
            str(placed),
        )

        # 2) apply_plan 返回体向后兼容：老字段还在，新增 issues / snapped_count。
        proj = _proj(root, src)
        region = {"id": REGION["id"], "kind": "room", "title": "女卫", "bbox": FIXTURE_BBOX}
        result = apply.apply_plan(proj, plan, region=region, message_id="m1")
        for key in ("ok", "rev", "layer", "ok_count", "failed_count", "results", "dirty", "can_undo"):
            _ok(f"apply-plan-keeps-{key}", key in result, str(list(result)))
        _ok("apply-plan-issues", isinstance(result.get("issues"), list), str(type(result.get("issues"))))
        _ok("apply-plan-snapped-count", result.get("snapped_count") == 2, str(result.get("snapped_count")))
        apply.undo_last(proj)

        # 3) 失败项对应 error issue 时补 issue_code。
        doc3 = ezdxf.readfile(str(src))
        bad_plan = {"moves": [{"block": "TOILET", "from": [2000.0, 2000.0], "to": [40000.0, 2000.0]}]}
        bad_results = apply.apply_mutations(doc3, bad_plan, bbox=FIXTURE_BBOX, layer="图腾-新增-1", region=REGION)
        _ok(
            "failed-item-issue-code",
            bad_results and bad_results[0].get("ok") is False
            and bad_results[0].get("issue_code") == "out_of_region",
            str(bad_results),
        )


def test_repair_message() -> None:
    issues = [
        {
            "code": "over_wall",
            "severity": "error",
            "message": "「TOILET」落在墙线上",
            "op": "insert",
            "index": 0,
            "block": "TOILET",
            "xy": [3000.0, 4000.0],
        }
    ]
    msg = build_repair_message(issues, {"room": "女卫"})
    _ok("repair-message-op", "新增" in msg and "第 1 项" in msg, msg)
    _ok("repair-message-xy", "3000" in msg and "4000" in msg, msg)
    _ok("repair-message-schema", "JSON" in msg or "schema" in msg, msg)
    _ok("repair-message-only-coords", "只改坐标" in msg, msg)
    _ok("repair-message-keeps-room", "女卫" in msg, msg)


def main() -> int:
    test_symbol_bbox_and_clear_rect()
    test_snap()
    test_check_plan()
    test_apply_end_to_end()
    test_repair_message()
    print("ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(str(exc), flush=True)
        raise SystemExit(1)
