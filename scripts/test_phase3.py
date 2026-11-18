#!/usr/bin/env python3
"""Phase 3 回归：方案结构校验（planschema）、保护图层、替换不丢数据、预览/应用一致。

全部用 ezdxf 自造的临时图纸，不依赖真实图纸、不联网、不调模型。测试尽量走真实入口：
``apply.apply_plan`` / ``apply.render_plan_preview`` / ``apply.apply_mutations``（包括调用方
自己传 ``issues=[]`` 的绕过路径）以及 ``/api/projects/{id}/chat`` 与
``/api/projects/{id}/apply`` 的 HTTP 路径。

A. 归一化与 blockers / warnings / needs_clarification：未知键丢弃并警告、坐标与类型
   （布尔、NaN、Infinity、字符串、三元组）、rot/scale/height/const_width、未知操作数组、
   legacy 方案原样兼容。
B. 保护图层：条目指定保护层是阻断项；目标实体的实际图层（含块定义继承、嵌套继承）、
   共享块里的嵌套符号、改文字落在保护层上，都不能借 layer 0 或嵌套漂过去。
C. 替换安全：新块插不上时旧实体必须还在；嵌套目标不删不覆盖、如实报「不支持」。
D. 预览 == 应用（含被保护/嵌套拦下的项）；被拦下的方案预览不画、应用不写、不建快照。
E. 歧义：同名块近似等距、正落中间、改文字两种口径等距 → 拒绝而不是猜。
F. 接线：chat 先归一化（坏结构直接问澄清、不浪费修复往返；未知键警告进入 issues）、
   应用被拦时的 400 与中文原因。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CAD_DATA_DIR", "")

import ezdxf  # noqa: E402

from app import apply, config, dxftext, library, planschema, projects  # noqa: E402

REGION = {"id": "subview:test:sheet", "bbox": [0.0, 0.0, 20000.0, 15000.0], "title": "测试图"}
BOX = REGION["bbox"]
CJK_LO = "\u4e00"
CJK_HI = "\u9fff"


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""), flush=True)


def _has_cn(text) -> bool:
    return any(CJK_LO <= ch <= CJK_HI for ch in str(text or ""))


def _brief(results) -> list:
    return [
        {k: r.get(k) for k in ("ok", "action", "block", "reason", "issue_code", "from", "text")}
        for r in results or []
    ]


# ---------------------------------------------------------------- 自造图纸


def _new_doc():
    doc = ezdxf.new("R2010", setup=True)
    for name in ("设备", "标注", "图框", "图签", "标题", "0"):
        if name not in doc.layers:
            doc.layers.add(name, color=7)
    return doc


def _square(block, size: float = 400.0, layer: str = "0"):
    half = size / 2.0
    return block.add_lwpolyline(
        [(-half, -half), (half, -half), (half, half), (-half, half)], close=True, dxfattribs={"layer": layer}
    )


def _plan_drawing(path: Path) -> dict:
    """一份结构齐全的小图：普通设备、图框、图签、嵌套(保护)符号、共享块、文字。"""
    doc = _new_doc()
    for name in ("BASIN", "SEAT_NEW", "SINK", "FRAME_SYM", "SEAL_SYM"):
        _square(doc.blocks.new(name), 400)
    # 共享底图块：内部有一个图框符号（在 block 层）和一个 layer 0 的洁具（继承宿主层）
    shared = doc.blocks.new("SHARED_HOST")
    _square(shared, 6000)
    nested = shared.add_blockref("FRAME_SYM", (-1000, -1000))
    nested.dxf.layer = "图框"
    incl = shared.add_blockref("BASIN", (1200, 1200))
    incl.dxf.layer = "0"
    # 顶层块：内部嵌保护符号（block 层继承 → 宿主图层）与 layer 0 的普通符号
    outer = doc.blocks.new("OUTER")
    _square(outer, 3000)
    seal = outer.add_blockref("SEAL_SYM", (-700, -700))
    seal.dxf.layer = "图签"
    plain0 = outer.add_blockref("BASIN", (600, 600))
    plain0.dxf.layer = "0"

    msp = doc.modelspace()
    msp.add_blockref("BASIN", (2000, 2000), dxfattribs={"layer": "设备"})
    msp.add_blockref("BASIN", (8000, 2000), dxfattribs={"layer": "设备"})
    msp.add_blockref("SINK", (5000, 5000), dxfattribs={"layer": "设备"})
    msp.add_blockref("SHARED_HOST", (20000, 9000), dxfattribs={"layer": "设备"})
    msp.add_blockref("OUTER", (5000, 9000), dxfattribs={"layer": "设备"})
    msp.add_blockref("FRAME_SYM", (15000, 13500), dxfattribs={"layer": "图框"})
    main_text = msp.add_text("女卫", dxfattribs={"height": 300, "layer": "标注", "insert": (3000, 6000)})
    frame_text = msp.add_text("楼梯间", dxfattribs={"height": 300, "layer": "图签", "insert": (3000, 13000)})
    zero_text = msp.add_text("说明", dxfattribs={"height": 200, "layer": "0", "insert": (6000, 13000)})
    doc.saveas(str(path))
    return {
        "main_text": main_text.dxf.handle,
        "frame_text": frame_text.dxf.handle,
        "zero_text": zero_text.dxf.handle,
    }


def _proj(root: Path, dxf: Path) -> dict:
    return {
        "id": "test-phase3",
        "name": "Phase3测试",
        "root": str(root),
        "dxf_path": str(dxf),
        "encoding": "utf-8",
        "chat_path": str(root / "chat.json"),
    }


def _fresh(root: Path, name: str) -> tuple[Path, dict, dict]:
    src = root / name
    info = _plan_drawing(src)
    return src, _proj(root / (name + "-sess"), src), info


def _apply_memory(src: Path, plan: dict, *, issues=None, bbox=BOX):
    doc = apply._read_doc(src)
    results = apply.apply_mutations(doc, plan, bbox=bbox, layer="图腾-新增-1", region=REGION, issues=issues)
    return doc, results


def _count_name(path: Path, name: str) -> int:
    doc = ezdxf.readfile(str(path))
    return sum(1 for e in doc.modelspace().query("INSERT") if e.dxf.name == name)


def _raw_text(path: Path, handle: str) -> str:
    doc = ezdxf.readfile(str(path))
    ent = doc.entitydb.get(handle)
    return dxftext.entity_raw(ent) if ent is not None else "<missing>"


def _insert_names(doc) -> list[str]:
    return [e.dxf.name for e in doc.modelspace().query("INSERT")]


# ================================================================ A. 归一化


def test_protected_defaults() -> None:
    for layer in ("图框", "图签", "标题", "TITLE", "BORDER", "SEAL"):
        _ok(f"protected-default:{layer}", planschema.is_protected_layer(layer))
    for layer in ("图框-0", "A1图框", "SBB图签", "图纸标题栏", "title-block", "Seal_01", "建筑-图框", "border"):
        _ok(f"protected-variant:{layer}", planschema.is_protected_layer(layer), layer)
    for layer in ("设备", "标注", "PIPE", "0", "", "说明", "SEATS", "SETUP", "ASSEMBLY"):
        _ok(f"not-protected:{layer or '空'}", not planschema.is_protected_layer(layer), layer)
    _ok("protected-pattern-none", planschema.protected_pattern("SEATS") is None)
    _ok(
        "protected-list-documented",
        {"图框", "图签", "标题", "TITLE", "BORDER", "SEAL"} <= set(planschema.PROTECTED_LAYERS),
        str(planschema.PROTECTED_LAYERS),
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    _ok("protected-documented-in-readme", "图框" in readme and "保护图层" in readme)


def test_normalize_legacy_plan_unchanged() -> None:
    """老计划（提示词 schema 的写法）原样通过：值不变、不产生 blocker、无警告。"""
    plan = {
        "mode": "plan",
        "reply": "已替换一个台盆。",
        "room": "女卫",
        "sheet": "二层卫排水详图",
        "analysis": "",
        "inserts": [{"block": "BASIN", "xy": [2000, 2000], "rot": 90, "scale": [1, 1], "layer": ""}],
        "deletes": [{"block": "SINK", "xy": [5000, 5000]}],
        "replaces": [
            {"from_block": "SINK", "from_xy": [5000, 5000], "block": "BASIN", "xy": [5000, 5000], "rot": 0, "scale": [1, 1], "layer": ""}
        ],
        "moves": [{"block": "BASIN", "from": [2000, 2000], "to": [3000, 2000], "rot": 0}],
        "pipes": [{"layer": "给水", "from": [1000, 1000], "to": [1500, 1000], "const_width": 25}],
        "texts": [{"text": "DN50", "xy": [1000, 1000], "layer": "", "style": "", "height": 200}],
        "text_edits": [{"from_text": "女卫", "xy": [3000, 6000], "text": "女卫（无障碍）"}],
        "do_not_touch": [],
        "issues": [],
    }
    res = planschema.normalize_plan(plan)
    _ok("legacy-valid", res.valid and not res.needs_clarification, str([i for i, _ in res.blockers]))
    _ok("legacy-no-warnings", not res.warnings, str([i for i, _ in res.warnings]))
    for key in ("inserts", "deletes", "replaces", "moves", "pipes", "texts", "text_edits"):
        _ok(f"legacy-values-equal:{key}", res.plan.get(key) == plan[key], f"{res.plan.get(key)} != {plan[key]}")
    _ok("legacy-keeps-meta", res.plan.get("reply") == plan["reply"] and res.plan.get("room") == "女卫" and res.plan.get("mode") == "plan")
    _ok("legacy-do-not-touch", res.plan.get("do_not_touch") == [])
    _ok("legacy-does-not-mutate-input", plan["inserts"][0]["xy"] == [2000, 2000] and "colour" not in plan)
    samples = [
        {"inserts": [{"block": "B", "xy": [1, 2]}]},
        {"inserts": [{"block": "B", "to": [1, 2], "scale": 2}]},
        {"inserts": [{"block": "B", "xy": (1.0, 2.0), "rot": -90}]},
        {"replaces": [{"block": "NEW", "to": [1, 2], "from_block": "OLD", "from_xy": [3, 4], "scale": [2, 3], "auto_attribs": False}]},
        {"replaces": [{"from_block": "OLD", "from": [3, 4], "block": "NEW", "xy": [3, 4]}]},
        {"deletes": [{"block": "B", "from": [1, 2]}]},
        {"moves": [{"block": "B", "from_xy": [1, 2], "xy": [5, 6]}]},
        {"texts": [{"text": "x" * 200, "xy": [1, 2], "height": 0}]},
        {"pipes": [{"from": [0, 0], "to": [10, 0]}]},
    ]
    for i, sample in enumerate(samples):
        r = planschema.normalize_plan({"mode": "plan", "reply": "x", **sample})
        _ok(
            f"legacy-valid:{i}",
            r.valid and not r.warnings,
            f"{sample} -> blockers={[x[0]['message'] for x in r.blockers]} warnings={[x[0]['message'] for x in r.warnings]}",
        )
    # 改文字逐字保留（含首尾空白），只在匹配时规整
    te = planschema.normalize_plan({"text_edits": [{"from_text": " 女卫 ", "xy": [1, 2], "text": " 女卫（无障碍） "}]})
    _ok("legacy-text-verbatim", te.plan["text_edits"][0]["from_text"] == " 女卫 " and te.plan["text_edits"][0]["text"] == " 女卫（无障碍） ")
    # 负角度照旧：写入端本来就用 _normalize_deg
    _ok("legacy-negative-rotation", planschema.normalize_plan(samples[2]).plan["inserts"][0]["rot"] == 270.0)
    # 非计划（answer）也原样保留
    ans = planschema.normalize_plan({"mode": "answer", "reply": "这里有两个台盆。", "inserts": []})
    _ok("legacy-answer-ok", ans.valid and ans.plan["reply"] == "这里有两个台盆。" and not ans.needs_clarification)


def test_unknown_keys_dropped_with_warning() -> None:
    plan = {
        "mode": "plan",
        "reply": "ok",
        "inserts": [{"block": "BASIN", "xy": [2000, 2000], "colour": 3, "layer": ""}],
        "deletes": [],
        "replaces": [],
        "moves": [],
        "pipes": [],
        "texts": [],
        "text_edits": [],
        "do_not_touch": [],
        "debug": {"trace": 1},
    }
    res = planschema.normalize_plan(plan)
    _ok("unknown-valid", res.valid, str([b[0]["message"] for b in res.blockers]))
    _ok("unknown-plan-key-dropped", "debug" not in res.plan, str(sorted(res.plan)))
    _ok("unknown-item-key-dropped", "colour" not in res.plan["inserts"][0] and res.plan["inserts"][0]["block"] == "BASIN")
    codes = [i.get("code") for i, _ in res.warnings]
    _ok("unknown-warns", codes.count("unknown_key") == 2, str(codes))
    _ok(
        "unknown-warning-shape",
        all(set(i) >= {"code", "severity", "message", "op", "index", "block", "xy"} and i["severity"] == "warn" for i, _ in res.warnings),
        str(res.warnings[0][0]),
    )
    _ok("unknown-warning-message-cn", any("debug" in i["message"] for i, _ in res.warnings), str([i["message"] for i, _ in res.warnings]))
    _ok("unknown-not-clarification", not res.needs_clarification and res.status == "valid_with_warnings", res.status)
    _ok("warning-message-visible", "debug" in planschema.warning_message(res), planschema.warning_message(res))
    res2 = planschema.normalize_plan({"mode": "plan", "reply": "x", "room": "女卫", "sheet": "S", "analysis": "a", "issues": [], "inserts": [{"block": "B", "xy": [1, 2]}]})
    _ok("known-meta-not-unknown", not res2.warnings, str([i[0].get("message") for i in res2.warnings]))
    # 同时给 xy 和 to 且不一致：同角色的两种说法冲突，问用户，不静默取第一个
    res3 = planschema.normalize_plan({"mode": "plan", "inserts": [{"block": "B", "xy": [1, 2], "to": [9, 9]}]})
    _ok("xy-to-conflict-blocked", not res3.valid and _blocked(res3, "alias_conflict") and res3.needs_clarification, str([b[0]["message"] for b in res3.blockers]))
    _ok("xy-to-conflict-no-pick", not res3.plan.get("inserts"), str(res3.plan.get("inserts")))


def _blocked(res, code: str) -> bool:
    return any(i.get("code") == code for i, _ in res.blockers)


def test_bad_operations_are_blockers() -> None:
    cases = {
        "xy-missing": {"inserts": [{"block": "BASIN"}]},
        "xy-one-number": {"inserts": [{"block": "BASIN", "xy": [1]}]},
        "xy-not-seq": {"inserts": [{"block": "BASIN", "xy": 5}]},
        "xy-three": {"inserts": [{"block": "BASIN", "xy": [1, 2, 3]}]},
        "xy-dict": {"inserts": [{"block": "BASIN", "xy": {"x": 1, "y": 2}}]},
        "xy-bool": {"inserts": [{"block": "BASIN", "xy": [True, 2]}]},
        "xy-string": {"inserts": [{"block": "BASIN", "xy": ["1", "2"]}]},
        "xy-nan": {"inserts": [{"block": "BASIN", "xy": [float("nan"), 2]}]},
        "xy-inf": {"inserts": [{"block": "BASIN", "xy": [1, float("inf")]}]},
        "xy-huge": {"inserts": [{"block": "BASIN", "xy": [1e12, 2]}]},
        "block-whitespace": {"inserts": [{"block": "   ", "xy": [1, 2]}]},
        "block-number": {"inserts": [{"block": 3, "xy": [1, 2]}]},
        "rot-string": {"inserts": [{"block": "BASIN", "xy": [1, 2], "rot": "90"}]},
        "rot-bool": {"inserts": [{"block": "BASIN", "xy": [1, 2], "rot": True}]},
        "rot-nan": {"inserts": [{"block": "BASIN", "xy": [1, 2], "rot": float("nan")}]},
        "scale-zero": {"inserts": [{"block": "BASIN", "xy": [1, 2], "scale": [0, 1]}]},
        "scale-negative": {"inserts": [{"block": "BASIN", "xy": [1, 2], "scale": -1}]},
        "scale-bool": {"inserts": [{"block": "BASIN", "xy": [1, 2], "scale": [True, 1]}]},
        "scale-inf": {"inserts": [{"block": "BASIN", "xy": [1, 2], "scale": [float("inf"), 1]}]},
        "item-not-dict": {"inserts": ["BASIN"]},
        "array-not-list": {"inserts": {"block": "BASIN"}},
        "delete-no-xy": {"deletes": [{"block": "BASIN"}]},
        "delete-bool-xy": {"deletes": [{"block": "BASIN", "xy": [False, 0]}]},
        "move-no-to": {"moves": [{"block": "BASIN", "from": [1, 2]}]},
        "move-nan": {"moves": [{"block": "BASIN", "from": [1, 2], "to": [float("nan"), 2]}]},
        "replace-no-new": {"replaces": [{"from_block": "BASIN", "from_xy": [1, 2], "xy": [1, 2]}]},
        "replace-no-place": {"replaces": [{"from_block": "BASIN", "block": "NEW"}]},
        "insert-no-xy": {"inserts": [{"block": "BASIN"}]},
        "replace-no-new-block": {"replaces": [{"from_block": "BASIN", "from_xy": [1, 2], "xy": [1, 2]}]},
        "replace-source-is-new-block": {"replaces": [{"from_block": "BASIN", "block": "", "from_xy": [1, 2], "xy": [1, 2]}]},
        # 只给新块坐标行不行是旧规则，不在这次范围里；这里只确认它依然算「不明确」
        "replace-only-new-xy": {"replaces": [{"from_block": "BASIN", "block": "NEW"}, {"from_block": "BASIN", "block": "NEW", "xy": [1, 2]}]},
        "text-no-content": {"texts": [{"text": "  ", "xy": [1, 2]}]},
        "text-height-bad": {"texts": [{"text": "x", "xy": [1, 2], "height": "200"}]},
        "text-height-nan": {"texts": [{"text": "x", "xy": [1, 2], "height": float("nan")}]},
        "pipe-no-end": {"pipes": [{"from": [0, 0]}]},
        "pipe-width-negative": {"pipes": [{"from": [0, 0], "to": [1, 0], "const_width": -3}]},
        "text-edit-no-text": {"text_edits": [{"from_text": "女卫", "xy": [1, 2]}]},
        "text-edit-no-xy": {"text_edits": [{"from_text": "女卫", "text": "男卫"}]},
        "mode-enum": {"mode": "delete_everything", "reply": "x"},
    }
    for name, plan in cases.items():
        res = planschema.normalize_plan({"mode": "plan", "reply": "x", **plan})
        _ok(f"blocked:{name}", bool(res.blockers) and not res.valid, f"{plan} -> {[b[0]['message'] for b in res.blockers]}")
        _ok(
            f"blocker-shape:{name}",
            all(set(i) >= {"code", "severity", "message", "op", "index", "block", "xy"} and i["severity"] == "error" for i, _ in res.blockers),
            str(res.blockers),
        )
        _ok(f"blocker-cn:{name}", all(_has_cn(i.get("message")) for i, _ in res.blockers), str([i["message"] for i, _ in res.blockers]))
        if name == "mode-enum":
            _ok("blocked-code:mode-enum", _blocked(res, "bad_mode"))
    res = planschema.normalize_plan({"inserts": [{"block": "BASIN", "xy": [True, 2]}, {"block": "BASIN", "xy": [1, 2]}]})
    _ok("blocked-items-removed", [i["xy"] for i in res.plan["inserts"]] == [[1.0, 2.0]], str(res.plan["inserts"]))
    _ok("blocked-index-points-at-item", [(i["op"], i["index"]) for i, _ in res.blockers] == [("insert", 0)], str([(i["op"], i["index"]) for i, _ in res.blockers]))
    res = planschema.normalize_plan({"mode": "plan", "inserts": [{"block": "BASIN", "xy": [float("nan"), 1]}]})
    json.dumps(res.to_dict(), allow_nan=False)  # NaN 不能借序列化逃逸


def test_needs_clarification_ambiguous() -> None:
    res = planschema.normalize_plan({"mode": "plan", "deletes": [{"block": "BASIN"}], "reply": "删掉台盆"})
    _ok("clarify-delete-missing-xy", res.needs_clarification and not res.valid, str([b[0]["code"] for b in res.blockers]))
    _ok("clarify-delete-code", _blocked(res, "ambiguous_target"), str([b[0]["code"] for b in res.blockers]))
    msg = planschema.clarify_message(res)
    _ok("clarify-message-cn", _has_cn(msg) and msg.strip(), msg)
    _ok("clarify-message-no-internals", "Traceback" not in msg and "BASIN" in msg, msg)
    res = planschema.normalize_plan({"mode": "plan", "inserts": [{"block": "BASIN"}], "reply": "加了"})
    _ok("insert-missing-xy-blocker", not res.valid and _blocked(res, "missing_xy"), str([b[0]["code"] for b in res.blockers]))
    _ok("insert-missing-xy-not-clarify", not res.needs_clarification)
    res = planschema.normalize_plan({"mode": "plan", "reply": "好的。", "inserts": [], "deletes": [], "replaces": [], "moves": []})
    _ok("plan-without-edits-clarify", res.needs_clarification and _blocked(res, "no_edits"), str([b[0]["code"] for b in res.blockers]))
    res = planschema.normalize_plan({"mode": "plan", "reply": "在图框加标", "inserts": [{"block": "B", "xy": [1, 2], "layer": "图框"}]})
    _ok("clarify-message-mentions-layer", "图框" in planschema.clarify_message(res), planschema.clarify_message(res))
    _ok("protected-not-clarification", not res.needs_clarification and _blocked(res, "protected_layer"), res.status)
    for bad in (None, [], "plan", 3):
        res = planschema.normalize_plan(bad)
        _ok(f"non-dict-plan:{type(bad).__name__}", res.needs_clarification and not res.valid, str([b[0]["code"] for b in res.blockers]))
        _ok(f"non-dict-plan-message:{type(bad).__name__}", _has_cn(planschema.clarify_message(res)))


def test_protected_layer_rules() -> None:
    for key, item in (
        ("inserts", {"block": "BASIN", "xy": [100, 100], "layer": "图框"}),
        ("replaces", {"from_block": "FRAME_SYM", "from_xy": [15000, 13500], "block": "BASIN", "xy": [15000, 13500], "layer": "图签"}),
        ("moves", {"block": "FRAME_SYM", "from": [15000, 13500], "to": [14000, 13500], "layer": "TITLE"}),
        ("texts", {"text": "x", "xy": [1, 1], "layer": "SEAL"}),
        ("pipes", {"from": [1, 1], "to": [2, 2], "layer": "BORDER"}),
    ):
        res = planschema.normalize_plan({"mode": "plan", "reply": "x", key: [item]})
        _ok(f"rules-layer-blocker:{key}", _blocked(res, "protected_layer") and not res.valid, str([b[0]["code"] for b in res.blockers]))
        _ok(f"rules-layer-message-cn:{key}", _has_cn(res.blockers[0][0]["message"]), res.blockers[0][0]["message"])
    # 空 layer = 默认写入层（图腾-新增-N），从不写进保护层；不拦
    res = planschema.normalize_plan({"mode": "plan", "inserts": [{"block": "BASIN", "xy": [1, 2], "layer": ""}]})
    _ok("rules-empty-layer-ok", res.valid, str([b[0]["code"] for b in res.blockers]))


# ================================================================ B. 保护图层（实际实体）


def test_actual_layer_protected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, _p, _i = _fresh(root, "actual.dxf")
        doc, results = _apply_memory(src, {"deletes": [{"block": "FRAME_SYM", "xy": [15000, 13500]}]}, issues=[])
        _ok("actual-delete-blocked", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)[:1]))
        _ok("actual-delete-kept", "FRAME_SYM" in _insert_names(doc))
        doc, results = _apply_memory(src, {"moves": [{"block": "FRAME_SYM", "from": [15000, 13500], "to": [14000, 13500]}]}, issues=[])
        _ok("actual-move-blocked", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)))
        moved = doc.modelspace().query('INSERT[name=="FRAME_SYM"]').first
        _ok("actual-move-kept", abs(moved.dxf.insert.x - 15000) < 1e-6 and abs(moved.dxf.insert.y - 13500) < 1e-6, str(tuple(moved.dxf.insert)))
        doc, results = _apply_memory(src, {"replaces": [{"from_block": "FRAME_SYM", "from_xy": [15000, 13500], "block": "BASIN", "xy": [15000, 13500]}]}, issues=[])
        _ok("actual-replace-blocked", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)))
        _ok("actual-replace-old-kept", "FRAME_SYM" in _insert_names(doc))
        doc, results = _apply_memory(src, {"inserts": [{"block": "BASIN", "xy": [3000, 3000]}], "texts": [{"text": "备注", "xy": [1000, 14000]}]})
        _ok("actual-insert-in-new-layer", results[0].get("ok") and results[0].get("layer") == "图腾-新增-1", str(_brief(results)[:1]))
        text = [e for e in doc.modelspace().query("TEXT") if e.dxf.text == "备注"]
        _ok("actual-text-in-new-layer", bool(text) and text[0].dxf.layer == "图腾-新增-1", str([e.dxf.layer for e in text]))
        # 真实入口：混有受保护目标的方案，预览与应用都不执行（整体阻断，而不是半套）
        with_layer = {"mode": "plan", "reply": "x", "inserts": [{"block": "BASIN", "xy": [3000, 3000]}], "replaces": [{"from_block": "SINK", "from_xy": [5000, 5000], "block": "BASIN", "xy": [5000, 5000], "layer": "图框"}]}
        for label, call in (
            ("preview", lambda: apply.render_plan_preview(_p, with_layer, REGION, root / "a.png")),
            ("apply", lambda: apply.apply_plan(_p, with_layer, region=REGION, message_id="m")),
        ):
            try:
                call()
            except apply.PlanBlockedError as exc:
                _ok(f"actual-named-layer-{label}-blocked", "protected_layer" in exc.codes, str(exc.codes))
            else:
                raise AssertionError(f"FAIL actual-named-layer-{label}: 指定保护层写法的方案执行了")
        _ok("actual-named-layer-no-write", not apply.work_path(_p).is_file())


def test_protected_via_block_definitions() -> None:
    """layer 0 继承：实体在 0 层、所属块（含嵌套）在保护层 → 实际就是保护层。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, proj, _i = _fresh(root, "lay0.dxf")
        doc = apply._read_doc(src)
        # SUMMARY 是一块干净的小符号；SHEET_DETAIL 里把它插在 0 层（继承到附加层的实际值另说）。
        summary = doc.blocks.new("SUMMARY")
        _square(summary, 300)
        host = doc.blocks.new("SHEET_DETAIL")
        nested_in_host = host.add_blockref("SUMMARY", (0, 0), dxfattribs={"layer": "0"})
        nested_in_host.dxf.layer = "0"
        # 模型空间宿主 → SHEET_DETAIL → SUMMARY（在 0 层 → 继承 SHEET_DETAIL 的图层「标题」）
        host.block.dxf.set("layer", "标题")
        doc.modelspace().add_blockref("SHEET_DETAIL", (12000, 12000), dxfattribs={"layer": "设备"})
        # layer 0 只继承引用它的 INSERT：SUMMARY 显示在 SHEET_DETAIL 的插入层「设备」上；
        # 块定义（BLOCK 头）上写的「标题」不参与继承，改它不会改出保护结果。
        _ok("lay0-nested-effective", planschema.effective_layer(nested_in_host) == "设备", planschema.effective_layer(nested_in_host))
        # 真正的保护继承：同一个块换到 标题 层的 INSERT 里，0 层内容跟着落进保护图层。
        titled_ref = doc.modelspace().add_blockref("SHEET_DETAIL", (12000, 4000), dxfattribs={"layer": "标题"})
        _ok(
            "lay0-nested-effective-protected",
            planschema.effective_layer(nested_in_host) == "设备"
            and planschema.analyze_insert(titled_ref).get("protected_where") == "标题",
            str(planschema.analyze_insert(titled_ref)),
        )
        # 共享块里的嵌套保护符号（block 层继承 → 宿主层「图框」）在真实入口里被拦下
        legacy = {"mode": "plan", "reply": "x", "deletes": [{"block": "FRAME_SYM", "xy": [19000, 8000]}], "inserts": [{"block": "BASIN", "xy": [3000, 3000]}]}
        plain = doc.blocks.new("PLAIN")
        _square(plain, 300)
        ok_ref = doc.modelspace().add_blockref("PLAIN", (4000, 11000), dxfattribs={"layer": "设备"})
        _ok("lay0-plain-not-protected", not planschema.is_protected_layer(planschema.effective_layer(ok_ref)), planschema.effective_layer(ok_ref))
        # 真实入口（预览与应用）不动保护层上的实体：写图前的整条预检就把方案拦下，
        # 原因码与中文说明如实（不是笼统的「没有写入」），不建预览文件、不写盘。
        legacy = {"mode": "plan", "reply": "x", "deletes": [{"block": "FRAME_SYM", "xy": [15000, 13500]}], "inserts": [{"block": "BASIN", "xy": [3000, 3000]}]}
        for label, call, dest in (
            ("preview", lambda: apply.render_plan_preview(proj, legacy, REGION, root / "l.png"), root / "l.png"),
            ("apply", lambda: apply.apply_plan(proj, legacy, region=REGION, message_id="l"), None),
        ):
            try:
                call()
            except apply.PlanBlockedError as exc:
                _ok(f"lay0-entry-{label}-blocked", "protected_layer" in exc.codes, str(exc.codes))
                _ok(f"lay0-entry-{label}-reason-cn", _has_cn(exc.result.issue_dicts()[0].get("message")), str(exc))
            else:
                raise AssertionError(f"FAIL lay0-entry-{label}-blocked: 图框上的删除没有被拦下")
            if dest is not None:
                _ok("lay0-entry-no-preview-file", not dest.exists())
        _ok("lay0-entry-no-work", not apply.work_path(proj).is_file())
        _ok("lay0-entry-saved-kept", _count_name(src, "FRAME_SYM") == 1)


def test_nested_protected_layers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, _p, _i = _fresh(root, "nested.dxf")
        target = [20000 - 1000, 9000 - 1000]
        doc, results = _apply_memory(
            src,
            {
                "deletes": [{"block": "FRAME_SYM", "xy": target}],
                "replaces": [{"from_block": "FRAME_SYM", "from_xy": target, "block": "BASIN", "xy": target}],
            },
        )
        # 写图前的预检如实报「嵌套在共享块里」：不是笼统的 not_applied，也不是把共享块当成保护层。
        _ok("nested-delete-blocked", not results[0].get("ok") and results[0].get("issue_code") == "nested_not_supported", str(_brief(results)[:1]))
        _ok("nested-delete-no-cover", not any(r.get("action") == "cover" for r in results), str(_brief(results)))
        _ok("nested-replace-blocked", not results[1].get("ok") and results[1].get("issue_code") == "nested_not_supported", str(_brief(results)[1:2]))
        host = doc.modelspace().query('INSERT[name=="SHARED_HOST"]').first
        nested = [e for e in host.virtual_entities() if e.dxftype() == "INSERT" and e.dxf.name == "FRAME_SYM"]
        _ok("nested-kept", len(nested) == 1, str(len(nested)))
        found = apply.find_symbol(doc, doc.modelspace(), "BASIN", [20000 + 1200, 9000 + 1200], BOX)
        _ok("nested-find-symbol-locates", bool(found) and found.get("kind") == "overlay", str({k: found.get(k) for k in ("kind", "name", "xy")} if found else None))
        doc, results = _apply_memory(src, {"deletes": [{"block": "BASIN", "xy": [20000 + 1200, 9000 + 1200]}]})
        _ok(
            "nested-unprotected-rejected-honestly",
            not results[0].get("ok") and results[0].get("issue_code") == "nested_not_supported",
            str(_brief(results)),
        )
        _ok("nested-unprotected-reason-cn", _has_cn(results[0].get("reason")) and not any(r.get("action") == "cover" for r in results), str(results[0].get("reason")))
        _ok("nested-unprotected-kept", "SHARED_HOST" in _insert_names(doc) and len([e for e in host.virtual_entities() if e.dxftype() == "INSERT"]) == 2)


def test_text_edits_respect_protected_layers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, _p, info = _fresh(root, "tedit.dxf")
        doc, results = _apply_memory(
            src,
            {
                "text_edits": [
                    {"from_text": "楼梯间", "xy": [3000, 13000], "text": "楼梯间（改）"},
                    {"from_text": "女卫", "xy": [3000, 6000], "text": "女卫（无障碍）"},
                ]
            },
            issues=[],
        )
        _ok("tedit-protected-blocked", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)[:1]))
        _ok("tedit-protected-text-kept", dxftext.entity_raw(doc.entitydb[info["frame_text"]]) == "楼梯间", dxftext.entity_raw(doc.entitydb[info["frame_text"]]))
        _ok("tedit-normal-ok", results[1].get("ok") and dxftext.clean_entity_text(doc.entitydb[info["main_text"]]) == "女卫（无障碍）", str(_brief(results)[1:2]))
        # layer 0 的模型空间文字：实际图层就是 0，不保护
        doc2, res2 = _apply_memory(src, {"text_edits": [{"from_text": "说明", "xy": [6000, 13000], "text": "说明（改）"}]})
        _ok("tedit-layer0-text-ok", res2[0].get("ok"), str(_brief(res2)))
        # 块内保护层的文字：改文字池只含模型空间与顶层属性，块内文字既不改到也不误报成功
        doc3 = _new_doc()
        inner = doc3.blocks.new("TITLE_BLOCK")
        inner_text = inner.add_text("图名", dxfattribs={"height": 100, "layer": "TITLE", "insert": (0, 0)})
        doc3.modelspace().add_blockref("TITLE_BLOCK", (1000, 1000))
        src3 = root / "blocktext.dxf"
        doc3.saveas(str(src3))
        doc4, res4 = _apply_memory(src3, {"text_edits": [{"from_text": "图名", "xy": [1000, 1000], "text": "图名（改）"}]})
        _ok("tedit-block-inner-text-not-edited", not res4[0].get("ok"), str(_brief(res4)))
        _ok("tedit-block-inner-text-kept", dxftext.clean_entity_text(doc4.entitydb[inner_text.dxf.handle]) == "图名")


# ================================================================ C. 替换安全


def test_failed_replace_insert_preserves_source() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, _p, _i = _fresh(root, "fail.dxf")
        plan = {
            "mode": "plan",
            "reply": "换成新台盆",
            "replaces": [{"from_block": "SINK", "from_xy": [5000, 5000], "block": "NO_SUCH_BLOCK_XYZ", "xy": [5000, 5000]}],
        }
        doc, results = _apply_memory(src, plan)
        _ok("fail-replace-reported", any(not r.get("ok") for r in results), str(_brief(results)))
        _ok("fail-no-insert", not any(r.get("ok") and r.get("action") == "insert" for r in results), str(_brief(results)))
        _ok("fail-source-kept", "SINK" in _insert_names(doc), "SINK 被删掉了")
        _ok("fail-no-cover", not any(r.get("action") == "cover" for r in results), str(_brief(results)))
        _ok("fail-reason-says-kept", "保留" in str(results[0].get("reason")), str(results[0].get("reason")))
        doc.saveas(str(root / "fail-out.dxf"))
        _ok("fail-saved-source-kept", _count_name(root / "fail-out.dxf", "SINK") == 1)
        plan2 = {"replaces": [{"from_block": "SINK", "from_xy": [5000, 5000], "block": "BASIN", "xy": [900000, 5000]}]}
        doc, results = _apply_memory(src, plan2)
        _ok("fail-offregion-source-kept", "SINK" in _insert_names(doc) and not results[0].get("ok"), str(_brief(results)))
        # 真实入口：整条方案都失败时 apply_plan 报错，但绝不能出现「旧块没了、新块也没有」
        proj = _p
        try:
            apply.apply_plan(proj, plan, region=REGION, message_id="f")
        except ValueError as exc:
            _ok("fail-entry-raises", bool(str(exc).strip()), str(exc))
        if apply.work_path(proj).is_file():
            _ok("fail-entry-saved-old-kept", _count_name(apply.work_path(proj), "SINK") == 1)
        _ok("fail-entry-no-new", _count_name(src, "NO_SUCH_BLOCK_XYZ") == 0)


def test_replace_success_swaps_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, _p, _i = _fresh(root, "swap.dxf")
        doc, results = _apply_memory(src, {"replaces": [{"from_block": "SINK", "from_xy": [5000, 5000], "block": "SEAT_NEW", "xy": [5000, 5000]}]})
        _ok("swap-ok", len(results) == 2 and all(r.get("ok") for r in results), str(_brief(results)))
        _ok("swap-old-gone", "SINK" not in _insert_names(doc), str(_insert_names(doc)))
        _ok("swap-new-there", _insert_names(doc).count("SEAT_NEW") == 1)


def test_move_validation_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, _p, _i = _fresh(root, "move.dxf")
        doc, results = _apply_memory(src, {"moves": [{"block": "BASIN", "from": [2000, 2000], "to": [900000, 900000]}]})
        _ok("move-offregion-rejected-before-write", not results[0].get("ok") and results[0].get("issue_code") == "out_of_region", str(_brief(results)))
        ent = doc.modelspace().query('INSERT[name=="BASIN"]').first
        _ok("move-offregion-kept", abs(ent.dxf.insert.x - 2000) < 1e-6, str(tuple(ent.dxf.insert)))
        for bad_rot in (True, float("nan"), float("inf"), "90"):
            try:
                doc, results = _apply_memory(src, {"moves": [{"block": "BASIN", "from": [8000, 2000], "to": [9000, 2000], "rot": bad_rot}]})
            except apply.PlanBlockedError as exc:
                _ok(f"move-bad-rot-rejected:{bad_rot!r}", "bad" in " ".join(exc.codes), str(exc.codes))
            else:
                _ok(f"move-bad-rot-rejected:{bad_rot!r}", not results[0].get("ok"), str(_brief(results)))
            kept_doc = apply._read_doc(src)
            bas = list(kept_doc.modelspace().query('INSERT[name=="BASIN"]'))
            _ok(f"move-bad-rot-kept:{bad_rot!r}", sorted(e.dxf.insert.x for e in bas) == [2000.0, 8000.0], str([e.dxf.insert.x for e in bas]))
        doc, results = _apply_memory(src, {"moves": [{"block": "BASIN", "from": [8000, 2000], "to": [9000, 2500], "rot": 45}]})
        _ok("move-ok", results[0].get("ok"), str(_brief(results)))
        moved = [e for e in doc.modelspace().query('INSERT[name=="BASIN"]') if abs(e.dxf.insert.x - 9000) < 1e-6]
        _ok("move-applied", len(moved) == 1 and abs(float(moved[0].dxf.rotation) - 45) < 1e-6, str([(e.dxf.insert.x, e.dxf.rotation) for e in doc.modelspace().query('INSERT[name=="BASIN"]')]))


def test_direct_mutation_bad_items_safe() -> None:
    """直接调用 apply_mutations（旧调用方会传 issues=[]）也不能把布尔/NaN 写进图。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, _p, _i = _fresh(root, "direct.dxf")
        plan = {
            "inserts": [
                {"block": "BASIN", "xy": [True, 2]},
                {"block": "BASIN", "xy": [float("nan"), 2]},
                {"block": "BASIN", "xy": [2000, 2000], "rot": True},
                {"block": "BASIN", "xy": [2000, 2000], "scale": [True, 1]},
            ],
            "texts": [{"text": "x", "xy": [1, 1], "height": "200"}],
            "deletes": [{"block": "FRAME_SYM", "xy": [15000, 13500]}],
            "replaces": [{"from_block": "SINK", "from_xy": [5000, 5000], "block": "NO_SUCH", "xy": [5000, 5000]}],
            "moves": [{"block": "BASIN", "from": [2000, 2000], "to": [900000, 0]}],
            "pipes": [{"from": [0, 0], "to": [1, 1], "const_width": float("nan")}],
        }
        try:
            doc, results = _apply_memory(src, plan, issues=[])
            ran = True
        except apply.PlanBlockedError as exc:
            _ok("direct-bad-plan-blocked", bool(exc.codes), str(exc.codes))
            doc = apply._read_doc(src)
            ran = False
        if ran:
            _ok("direct-bad-plan-partial-ok", not any(r.get("ok") for r in results), str(_brief(results)))
        names = _insert_names(doc)
        _ok("direct-no-extra-inserts", len(names) == 6, str(names))
        _ok("direct-sink-kept", "SINK" in names)
        _ok("direct-frame-kept", "FRAME_SYM" in names)
        _ok("direct-no-nan", all(abs(float(e.dxf.insert.x)) < 1e9 for e in doc.modelspace().query("INSERT")))


# ================================================================ D. 预览 == 应用


def _compare(preview_res, apply_res) -> tuple[bool, str]:
    a = [(r.get("ok"), r.get("action"), r.get("block"), r.get("issue_code")) for r in preview_res]
    b = [(r.get("ok"), r.get("action"), r.get("block"), r.get("issue_code")) for r in apply_res]
    return a == b, f"preview={a} apply={b}"


PLANS = {
    "protected-target": {
        "mode": "plan",
        "reply": "x",
        "deletes": [{"block": "FRAME_SYM", "xy": [15000, 13500]}],
        "inserts": [{"block": "BASIN", "xy": [3000, 3000]}],
        "text_edits": [{"from_text": "楼梯间", "xy": [3000, 13000], "text": "楼梯间A"}],
    },
    "nested-target": {
        "deletes": [{"block": "FRAME_SYM", "xy": [19000, 8000]}],
        "replaces": [{"from_block": "BASIN", "from_xy": [21200, 10200], "block": "SEAT_NEW", "xy": [21200, 10200]}],
        "inserts": [{"block": "BASIN", "xy": [3000, 9000]}],
    },
    "failed-replace": {
        "replaces": [{"from_block": "SINK", "from_xy": [5000, 5000], "block": "NOPE", "xy": [5000, 5000]}],
        "inserts": [{"block": "BASIN", "xy": [3000, 3000]}],
    },
    "good": {
        "inserts": [{"block": "SEAT_NEW", "xy": [3000, 9000]}],
        "moves": [{"block": "BASIN", "from": [2000, 2000], "to": [9000, 9000]}],
        "texts": [{"text": "DN75", "xy": [10000, 9000]}],
        "text_edits": [{"from_text": "女卫", "xy": [3000, 6000], "text": "女卫（无障碍）"}],
    },
    "legacy-shape": {
        "mode": "plan",
        "replaces": [{"from_block": "SINK", "from_xy": [5000, 5000], "block": "BASIN", "xy": [5000, 5000], "rot": 0, "scale": [1, 1], "layer": ""}],
        "pipes": [{"layer": "给水", "from": [12000, 9000], "to": [13000, 9000], "const_width": 25}],
    },
}


def test_preview_apply_parity_batch() -> None:
    for name, plan in PLANS.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, proj, info = _fresh(root, f"parity-{name}.dxf")
            dest = root / f"{name}.png"
            try:
                preview = apply.render_plan_preview(proj, plan, REGION, dest)
            except apply.PlanBlockedError as exc:
                # 写图前的预检拦下的方案：预览不画（不生成文件、不建工作图），原因码与中文说明
                # 如实（不是笼统的 not_applied），应用同样拒绝、不写盘。
                _ok(f"parity-{name}-preview-blocked", set(exc.codes) <= {"protected_layer", "nested_not_supported"}, str(exc.codes))
                _ok(f"parity-{name}-preview-blocked-cn", _has_cn(str(exc)), str(exc))
                _ok(f"parity-{name}-preview-blocked-no-file", not dest.exists())
                _ok(f"parity-{name}-preview-blocked-no-work", not apply.work_path(proj).is_file() and not apply.load_history(proj)["entries"])
                try:
                    apply.apply_plan(proj, plan, region=REGION, message_id=name)
                except apply.PlanBlockedError:
                    _ok(f"parity-{name}-apply-all-blocked", True)
                else:
                    raise AssertionError(f"FAIL parity-{name}: 预览被拦下的方案应用成功了")
                _ok(f"parity-{name}-apply-no-write", not apply.work_path(proj).is_file() and not apply.load_history(proj)["entries"])
                continue
            _ok(f"parity-{name}-preview-ok", preview.get("preview") is True, str(preview.get("results"))[:160])
            _ok(f"parity-{name}-no-work-copy", not apply.work_path(proj).is_file())
            _ok(f"parity-{name}-no-history", not apply.load_history(proj)["entries"])
            _ok(f"parity-{name}-source-untouched", _count_name(src, "SINK") == 1 and _raw_text(src, info["main_text"]) == "女卫")
            try:
                out = apply.apply_plan(proj, plan, region=REGION, message_id=name)
            except apply.PlanBlockedError as exc:
                # 整条方案一项都写不上（预览不画）：应用同样拒绝，原因一致，且不写盘
                _ok(f"parity-{name}-apply-all-blocked", preview["ok_count"] == 0, str(exc))
                _ok(f"parity-{name}-apply-error-cn", _has_cn(str(exc)), str(exc))
                _ok(f"parity-{name}-apply-both-codes", set(exc.codes) <= {"protected_layer", "nested_not_supported"}, str(exc.codes))
                _ok(f"parity-{name}-apply-no-write", not apply.work_path(proj).is_file() and not apply.load_history(proj)["entries"])
                continue
            same, detail = _compare(preview["results"], out["results"])
            _ok(f"parity-{name}-same-results", same, detail)
            _ok(f"parity-{name}-same-counts", (preview["ok_count"], preview["failed_count"]) == (out["ok_count"], out["failed_count"]), detail)
            failed = [r for r in out["results"] if not r.get("ok")]
            if name == "protected-target":
                codes = {r.get("issue_code") for r in failed}
                _ok("parity-protected-codes", codes == {"protected_layer"}, str(codes))
                saved = ezdxf.readfile(str(apply.work_path(proj)))
                _ok("parity-protected-entity-kept", "FRAME_SYM" in _insert_names(saved))
                _ok("parity-protected-text-kept", any(e.dxftype() == "TEXT" and e.dxf.text == "楼梯间" for e in saved.modelspace()))
                _ok("parity-protected-others-applied", out["ok_count"] == 1, str(out["ok_count"]))
            if name == "nested-target":
                codes = {r.get("issue_code") for r in failed}
                _ok("parity-nested-codes", codes == {"protected_layer", "nested_not_supported"}, str(codes))
                saved = ezdxf.readfile(str(apply.work_path(proj)))
                host = saved.modelspace().query('INSERT[name=="SHARED_HOST"]').first
                _ok("parity-nested-shared-block-untouched", len([e for e in host.virtual_entities() if e.dxftype() == "INSERT"]) == 2)
                _ok(
                    "parity-nested-no-nested-change",
                    out["ok_count"] == 1 and not any(r.get("action") == "cover" or (r.get("ok") and r.get("block") in {"FRAME_SYM", "SEAT_NEW"}) for r in out["results"]),
                    detail,
                )
            if name == "failed-replace":
                _ok("parity-failed-replace-old-kept", _count_name(apply.work_path(proj), "SINK") == 1, str(out["ok_count"]))
            if name == "good":
                _ok("parity-good-all-applied", out["failed_count"] == 0 and out["ok_count"] == 4, detail)
            if name == "legacy-shape":
                _ok("parity-legacy-applied", out["failed_count"] == 0, detail)
                _ok("parity-legacy-no-warnings-in-issues", not [i for i in out.get("issues") or [] if i.get("code") == "unknown_key"], str(out.get("issues")))


def test_preview_and_apply_blocked_plan() -> None:
    """结构层已拦下的方案（模型没给清楚要删哪一处）：预览不画，应用不写、不建快照。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, proj, _i = _fresh(root, "blocked.dxf")
        plan = {"mode": "plan", "reply": "删掉图框符号", "deletes": [{"block": "FRAME_SYM"}]}
        for label, call in (
            ("preview", lambda: apply.render_plan_preview(proj, plan, REGION, root / "b.png")),
            ("apply", lambda: apply.apply_plan(proj, plan, region=REGION, message_id="b")),
        ):
            try:
                call()
            except apply.PlanBlockedError as exc:
                _ok(f"blocked-{label}-codes", "ambiguous_target" in exc.codes, str(exc.codes))
                _ok(f"blocked-{label}-cn", _has_cn(str(exc)), str(exc))
                _ok(f"blocked-{label}-clarify", getattr(exc.result, "needs_clarification", False) is True)
            else:
                raise AssertionError(f"FAIL blocked-{label}: 被拦下的方案居然执行了")
        _ok("blocked-no-work-copy", not apply.work_path(proj).is_file())
        _ok("blocked-no-preview-file", not (root / "b.png").exists(), "被拦下的方案不该生成预览图")
        _ok("blocked-no-history", not apply.load_history(proj)["entries"])
        _ok("blocked-no-snapshot", not (Path(proj["root"]) / ".studio" / "revisions").exists())


# ================================================================ E. 歧义


def _ambiguous(src: Path) -> dict:
    doc = _new_doc()
    for name in ("BASIN", "SINK"):
        _square(doc.blocks.new(name), 400)
    msp = doc.modelspace()
    msp.add_blockref("SINK", (4000, 4000), dxfattribs={"layer": "设备"})
    msp.add_blockref("SINK", (6000, 4000), dxfattribs={"layer": "设备"})
    msp.add_text("女卫", dxfattribs={"height": 300, "layer": "标注", "insert": (4000, 9000)})
    msp.add_text("女卫", dxfattribs={"height": 300, "layer": "标注", "insert": (6000, 9000)})
    msp.add_text("备餐间；主", dxfattribs={"height": 300, "layer": "标注", "insert": (2000, 11000)})
    msp.add_text("备餐间;次", dxfattribs={"height": 300, "layer": "标注", "insert": (6000, 11000)})
    msp.add_text("男卫", dxfattribs={"height": 80, "layer": "标注", "insert": (9000, 9000)})
    msp.add_text("男卫", dxfattribs={"height": 80, "layer": "标注", "insert": (9300, 9000)})
    doc.saveas(str(src))
    return {}


def test_block_ambiguity_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "amb.dxf"
        _ambiguous(src)
        doc, results = _apply_memory(src, {"deletes": [{"block": "SINK", "xy": [5000, 4000]}]})
        _ok("amb-delete-rejected", not results[0].get("ok"), str(_brief(results)))
        _ok("amb-delete-nothing-removed", len(doc.modelspace().query('INSERT[name=="SINK"]')) == 2)
        doc, results = _apply_memory(src, {"replaces": [{"from_block": "SINK", "from_xy": [5000, 4000], "block": "BASIN", "xy": [5000, 4000]}]})
        _ok("amb-replace-rejected", all(not r.get("ok") for r in results), str(_brief(results)))
        _ok("amb-replace-old-kept", len(doc.modelspace().query('INSERT[name=="SINK"]')) == 2)
        _ok("amb-replace-nothing-inserted", "BASIN" not in _insert_names(doc))
        doc, results = _apply_memory(src, {"moves": [{"block": "SINK", "from": [5000, 4000], "to": [5000, 6000]}]})
        _ok("amb-move-rejected", not results[0].get("ok"), str(_brief(results)))
        # 差一点点也不算明确：相距 2000mm，点只离其中一处近 200mm
        doc, results = _apply_memory(src, {"deletes": [{"block": "SINK", "xy": [4900, 4000]}]})
        _ok("amb-nearly-equal-rejected", not results[0].get("ok"), f"模型点差 200mm 就删了一个：{_brief(results)}")
        _ok("amb-nearly-equal-nothing-removed", len(doc.modelspace().query('INSERT[name=="SINK"]')) == 2)
        # 明确最近的一处照常删
        doc, results = _apply_memory(src, {"deletes": [{"block": "SINK", "xy": [4050, 4000]}]})
        left = [(round(e.dxf.insert.x), round(e.dxf.insert.y)) for e in doc.modelspace().query('INSERT[name=="SINK"]')]
        _ok("amb-nearest-delete-ok", results[0].get("ok") and left == [(6000, 4000)], f"{left} {_brief(results)}")


def test_text_ambiguity_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "ambtext.dxf"
        _ambiguous(src)
        doc, results = _apply_memory(src, {"text_edits": [{"from_text": "女卫", "xy": [5000, 9000], "text": "男卫"}]})
        texts = sorted(dxftext.clean_entity_text(e) for e in doc.modelspace().query("TEXT") if e.dxf.text.startswith("女卫"))
        _ok("text-amb-rejected", not results[0].get("ok") and "多处" in str(results[0].get("reason")), str(results[0]))
        _ok("text-amb-kept", texts == ["女卫", "女卫"], str(texts))
        # 两种口径指向不同实体（相距仅 300mm）：不猜
        doc, results = _apply_memory(src, {"text_edits": [{"from_text": "男卫", "xy": [9150, 9000], "text": "女卫改"}]})
        kept = sorted(dxftext.clean_entity_text(e) for e in doc.modelspace().query("TEXT") if e.dxf.text.startswith("男卫"))
        _ok(
            "text-ambiguity-uniform-criterion",
            not results[0].get("ok") and "多处" in str(results[0].get("reason")) and kept == ["男卫", "男卫"],
            f"{_brief(results)} kept={kept}",
        )
        # 既有全/半角放宽规则不变：精确没命中时，放宽口径唯一的一处照用
        doc, results = _apply_memory(src, {"text_edits": [{"from_text": "备餐间;主", "xy": [4000, 11000], "text": "X"}]})
        _ok(
            "text-width-unchanged-fallback",
            results[0].get("ok") and results[0].get("match") == "width-normalized" and results[0].get("from") == "备餐间；主",
            str(results[0]),
        )
        r = _apply_memory(src, {"text_edits": [{"from_text": "备餐间;次", "xy": [5900, 11000], "text": "备餐间;丙"}]})[1]
        _ok("text-exact-nearer-ok", r[0].get("ok") and r[0].get("match") == "exact", str(r[0]))


# ================================================================ F. 接线


def _session_on_disk(tmp: Path) -> tuple[dict, dict]:
    root = tmp / "sess"
    root.mkdir(parents=True, exist_ok=True)
    src = root / "out_source.dxf"
    info = _plan_drawing(src)
    proj = {
        "id": "s-phase3",
        "name": "Phase3会话",
        "kind": "session",
        "root": str(root),
        "dxf_path": str(src),
        "available": True,
        "encoding": "utf-8",
        "chat_path": str(root / "chat.json"),
    }
    return proj, info


def _patched(proj: dict):
    """即用即弃的 patch 组合：会话解析、图幅、就绪状态。"""
    inner = mock.patch.object(
        projects, "resolve_project", side_effect=lambda pid: dict(proj) if pid == proj["id"] else (_ for _ in ()).throw(KeyError(pid))
    )
    return (
        inner,
        mock.patch.object(projects, "get_region", return_value=REGION),
        mock.patch.object(projects, "region_label", return_value="测试图"),
        mock.patch.object(init_is_ready_module(), "is_ready", return_value=True),
    )


def init_is_ready_module():
    from app import init_proj

    return init_proj


def test_http_apply_blocked_reports_reason() -> None:
    from fastapi.testclient import TestClient

    from app import auth, main

    with tempfile.TemporaryDirectory() as tmp:
        proj, info = _session_on_disk(Path(tmp))
        projects.append_chat(proj, [{
            "id": "msg-blocked",
            "role": "assistant",
            "text": "删掉台盆。",
            "ts": projects.now_iso(),
            "mode": "plan",
            "plan": {"mode": "plan", "reply": "删掉台盆。", "deletes": [{"block": "BASIN"}]},
            "region": REGION["id"],
        }])
        prev = (config.ACCESS_USERNAME, config.ACCESS_PASSWORD, config.SESSION_SECRET)
        config.ACCESS_USERNAME, config.ACCESS_PASSWORD, config.SESSION_SECRET = "u", "p", "phase3-secret"
        try:
            client = TestClient(main.app)
            client.cookies.set(config.SESSION_COOKIE, auth.issue_session("u"))
            patches = _patched(proj)
            for p in patches:
                p.start()
            try:
                resp = client.post(f"/api/projects/{proj['id']}/apply", json={"message_id": "msg-blocked"})
            finally:
                for p in patches:
                    p.stop()
            _ok("http-blocked-400", resp.status_code == 400, f"{resp.status_code} {resp.text[:300]}")
            detail = (resp.json() or {}).get("detail") or ""
            _ok("http-blocked-detail-cn", _has_cn(detail) and "BASIN" in detail, detail)
            _ok("http-blocked-no-write", not apply.work_path(proj).is_file() and not apply.load_history(proj)["entries"])
        finally:
            config.ACCESS_USERNAME, config.ACCESS_PASSWORD, config.SESSION_SECRET = prev


def _run_chat(proj: dict, text: str, responses: list[str]) -> tuple[list[dict], list[str]]:
    from app import decision, maas, main

    calls: list[str] = []

    async def fake_classify(*_a, **_k):
        return {"off_topic": False, "reason": "test"}

    async def fake_complete(**kwargs):
        calls.append(str(kwargs.get("user_text") or ""))
        yield {"type": "delta", "text": responses[min(len(calls) - 1, len(responses) - 1)]}
        yield {"type": "end"}

    patches = _patched(proj) + (
        mock.patch.object(decision, "classify_off_topic", side_effect=fake_classify),
        mock.patch.object(maas, "complete", side_effect=fake_complete),
    )
    for p in patches:
        p.start()
    try:
        from fastapi.testclient import TestClient

        from app import auth

        prev = (config.ACCESS_USERNAME, config.ACCESS_PASSWORD, config.SESSION_SECRET)
        config.ACCESS_USERNAME, config.ACCESS_PASSWORD, config.SESSION_SECRET = "u", "p", "phase3-secret"
        try:
            client = TestClient(main.app)
            client.cookies.set(config.SESSION_COOKIE, auth.issue_session("u"))
            resp = client.post(f"/api/projects/{proj['id']}/chat", json={"text": text, "region": REGION["id"]})
        finally:
            config.ACCESS_USERNAME, config.ACCESS_PASSWORD, config.SESSION_SECRET = prev
    finally:
        for p in patches:
            p.stop()
    _ok(f"chat-http-ok:{text[:8]}", resp.status_code == 200, str(resp.status_code))
    events = []
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events, calls


def _last_assistant(events: list[dict]) -> dict:
    for ev in reversed(events):
        if ev.get("type") == "done":
            return ev.get("message") or {}
    raise AssertionError(f"no done event: {events[:5]}")


def test_chat_normalizes_before_repair() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proj, info = _session_on_disk(Path(tmp))
        bad_plan = json.dumps({"mode": "plan", "reply": "在女卫加一个台盆。", "inserts": [{"block": "BASIN", "xy": [True, 2000]}]}, ensure_ascii=False)
        events, calls = _run_chat(proj, "女卫里加一个台盆", [bad_plan, bad_plan])
        _ok("chat-bad-schema-one-model-call", len(calls) == 1, f"calls={len(calls)}")
        msg = _last_assistant(events)
        _ok("chat-bad-schema-mode", msg.get("mode") == "clarify", str(msg.get("mode")))
        _ok("chat-bad-schema-no-edits", not (msg.get("plan") or {}).get("inserts"), str(msg.get("plan")))
        _ok("chat-bad-schema-text-cn", _has_cn(msg.get("text")), str(msg.get("text")))
        _ok("chat-bad-schema-no-preview", not any(ev.get("type") == "preview" for ev in events), str([ev.get("type") for ev in events]))
        _ok("chat-bad-schema-no-pending", apply.last_pending_plan(proj) is None)

        plan_with_extra = json.dumps({"mode": "plan", "reply": "在女卫加一个台盆。", "inserts": [{"block": "BASIN", "xy": [3000, 3000], "colour": 3}]}, ensure_ascii=False)
        events, calls = _run_chat(proj, "女卫里再加一个台盆", [plan_with_extra])
        msg = _last_assistant(events)
        plan = msg.get("plan") or {}
        _ok("chat-extra-key-dropped", bool(plan.get("inserts")) and "colour" not in plan["inserts"][0], str(plan.get("inserts")))
        issues = plan.get("issues") or []
        _ok("chat-extra-key-warning-visible", any(i.get("code") == "unknown_key" for i in issues), str(issues))
        _ok("chat-warning-not-blocking", msg.get("mode") == "plan" and len(calls) == 1, f"mode={msg.get('mode')} calls={len(calls)}")
        _ok("chat-warning-text-distinct", "多余字段" in str(msg.get("text")) and "不影响" in str(msg.get("text")), str(msg.get("text")))
        _ok("chat-plan-schema-outcome", (plan.get("schema") or {}).get("status") == "valid_with_warnings", str(plan.get("schema")))

        protected_plan = json.dumps({"mode": "plan", "reply": "在图框上加个台盆。", "inserts": [{"block": "BASIN", "xy": [15000, 13500], "layer": "图框"}]}, ensure_ascii=False)
        events, calls = _run_chat(proj, "在图框上加一个台盆", [protected_plan, protected_plan])
        msg = _last_assistant(events)
        _ok("chat-protected-clarify", msg.get("mode") == "clarify" and "图框" in str(msg.get("text")), f"{msg.get('mode')} {msg.get('text')}")
        _ok("chat-protected-no-model-retry", len(calls) == 1, f"calls={len(calls)}")


# ================================================================ G. layer 0 经 INSERT 链继承保护


def _layer0_wrapper_doc(path: Path) -> dict:
    """模仿真实图框：顶层图块直接放在 layer 0，图框层写在它的**插入**上，块内文字/属性也是 0 层。

    layer 0 的继承只看 INSERT 链（块定义 → 宿主 INSERT → …），与 BLOCK 头无关。这份图里
    块定义、块内几何、文字、属性全是 0 层，只有模型空间那一个 INSERT 在图框层上。
    """
    doc = _new_doc()
    frame = doc.blocks.new("图框")
    frame.add_lwpolyline([(0, 0), (594, 0), (594, 420), (0, 420)], close=True, dxfattribs={"layer": "0"})
    frame.add_text("图名", dxfattribs={"height": 40, "layer": "0", "insert": (10, 10)})
    frame.add_attdef("编号", (300, 400), dxfattribs={"layer": "0", "height": 20})
    msp = doc.modelspace()
    frame_ref = msp.add_blockref("图框", (15000, 13000), dxfattribs={"layer": "图框"})
    frame_ref.add_attrib("编号", "A-01", (15300, 13400), dxfattribs={"layer": "0", "height": 20})
    msp.add_blockref("图框", (3000, 9000), dxfattribs={"layer": "设备"})   # 同名块，放在普通层：不保护
    msp.add_blockref("BASIN", (8000, 3000), dxfattribs={"layer": "设备"})
    doc.saveas(str(path))
    return {}


def _live(path: Path) -> list[tuple]:
    """摊平整张图、只留渲染可见图元的 (类型, 文字, 实际图层)，按 CAD 的继承口径：

    块里画在 0 层的图元继承**插入**的图层（经逐层嵌套的插入解析），关掉的图层不显示。
    ezdxf 的 RenderContext.resolve_layer 就是这么判的，场景预览 / 索引也用同一个实际图层。
    """
    doc = apply._read_doc(path)
    out: list[tuple] = []
    seen: set[str] = set()

    def walk(insert, host_layer, outer_layer=None):
        # 属性在 0 层时跟着所属 INSERT 的图层走（ezdxf 的爆炸 / CAD 都按这个）。
        attrib_layer = host_layer if outer_layer is None else outer_layer
        name = insert.dxf.name
        if name in seen or name not in doc.blocks:
            return
        seen.add(name)
        for e in doc.blocks.get(name):
            lay = str(e.dxf.layer or "0")
            lay = host_layer if lay == "0" else lay
            if e.dxftype() == "INSERT":
                walk(e, lay, host_layer)
            elif e.dxftype() in ("TEXT", "MTEXT"):
                text = e.dxf.text if e.dxftype() == "TEXT" else e.text
                out.append(("text", text, lay))
            else:
                out.append((e.dxftype(), "", lay))
        for a in getattr(insert, "attribs", []):
            out.append(("attrib", a.dxf.text, attrib_layer if a.dxf.layer == "0" else a.dxf.layer))

    for ent in doc.modelspace():
        lay = str(ent.dxf.layer or "0")
        if ent.dxftype() == "INSERT":
            walk(ent, lay)
        else:
            out.append((ent.dxftype(), "", lay))
    return out


def test_layer0_wrapper_protected() -> None:
    """顶层 INSERT（layer 0，插入在图框层）删/移/换：父块里有保护内容，一律不执行，图上原样。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "layer0.dxf"
        _layer0_wrapper_doc(src)
        before = _live(src)
        _ok("layer0-render-sees-title-text", ("text", "图名", "图框") in before, str(before))

        doc = apply._read_doc(src)
        ref = [e for e in doc.modelspace().query('INSERT[name=="图框"]') if e.dxf.insert.x > 10000][0]
        _ok("layer0-effective-layer", planschema.effective_layer(ref) == "图框", planschema.effective_layer(ref))
        # 属性自己写了 0 层、宿主 INSERT 在 图框 层：0 层只继承宿主 INSERT，所以也显示在 图框
        # （被删的 INSERT 会把这个属性一起带走，保护判断看得到它）。注意 ezdxf 的
        # RenderContext.resolve_layer(ATTRIB) 不解析 FROM/ATTDEF 链，对属性返回原样 "0"，
        # 与「表格」那条（显式写 图框 层）不是同一口径——这里按 INSERT 链判。
        _ok("layer0-attrib-inherited", planschema.effective_layer(ref.attribs[0]) == "图框", planschema.effective_layer(ref.attribs[0]))
        _ok(
            "layer0-attrib-inherited-analysis",
            planschema.analyze_insert(ref).get("protected_where") == "图框",
            str(planschema.analyze_insert(ref)),
        )

        ops = {
            "delete": {"deletes": [{"block": "图框", "xy": [15000, 13000]}]},
            "move": {"moves": [{"block": "图框", "from": [15000, 13000], "to": [14000, 13000]}]},
            "replace": {"replaces": [{"from_block": "图框", "from_xy": [15000, 13000], "block": "BASIN", "xy": [15000, 13000]}]},
        }
        for op, plan in ops.items():
            doc, results = _apply_memory(src, plan, issues=[])
            _ok(
                f"layer0-{op}-rejected",
                bool(results) and all(r.get("ok") is False for r in results) and any(r.get("issue_code") == "protected_layer" for r in results),
                str(_brief(results)),
            )
            names = _insert_names(doc)
            kept = [e for e in doc.modelspace().query('INSERT[name=="图框"]') if abs(e.dxf.insert.x - 15000) < 1]
            _ok(
                f"layer0-{op}-kept",
                names.count("图框") == 2 and len(kept) == 1 and kept[0].dxf.layer == "图框" and names.count("BASIN") == 1,
                str(names),
            )
            out = root / f"layer0-{op}.dxf"
            doc.saveas(str(out))
            after = _live(out)
            _ok(f"layer0-{op}-render-unchanged", after == before, f"{before} -> {after}")

        # 块内文字（0 层，继承图框层）也不改
        doc, results = _apply_memory(src, {"text_edits": [{"from_text": "图名", "xy": [15000, 13000], "text": "图名（改）"}]}, issues=[])
        _ok("layer0-text-edit-rejected", bool(results) and not results[0].get("ok"), str(_brief(results)))

        # 普通层上的同名块照常可改：保护只看实际图层，不按块名一刀切
        doc, results = _apply_memory(src, {"deletes": [{"block": "图框", "xy": [3000, 9000]}]}, issues=[])
        _ok("layer0-plain-copy-deletable", results[0].get("ok") and _insert_names(doc).count("图框") == 1, str(_brief(results)))


def test_layer0_wrapper_deep_content() -> None:
    """保护判断要往深处走：块内插入块（多层）、嵌套块里的 0 层文字、嵌套 INSERT 链依次继承。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "deep.dxf"
        doc = _new_doc()
        leaf = doc.blocks.new("LEAF")
        leaf.add_lwpolyline([(0, 0), (50, 0), (50, 50), (0, 50)], close=True, dxfattribs={"layer": "0"})
        leaf.add_text("图名内字", dxfattribs={"height": 10, "layer": "0", "insert": (1, 1)})
        mid = doc.blocks.new("MID")
        mid.add_blockref("LEAF", (10, 10), dxfattribs={"layer": "0"})       # 0 层：继承 MID 的插入层
        mid.add_blockref("LEAF", (20, 20), dxfattribs={"layer": "图签"})    # 直接保护层
        wrap = doc.blocks.new("WRAP")
        wrap.add_blockref("MID", (0, 0), dxfattribs={"layer": "0"})         # 0 层：继承 WRAP 的插入层
        doc.modelspace().add_blockref("WRAP", (15000, 13000), dxfattribs={"layer": "TITLE"})
        doc.modelspace().add_blockref("BASIN", (8000, 3000), dxfattribs={"layer": "设备"})
        doc.saveas(str(src))
        doc, results = _apply_memory(src, {"deletes": [{"block": "WRAP", "xy": [15000, 13000]}]}, issues=[])
        _ok("deep-delete-rejected", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)))
        _ok("deep-kept", _insert_names(doc).count("WRAP") == 1)
        doc, results = _apply_memory(src, {"moves": [{"block": "WRAP", "from": [15000, 13000], "to": [14000, 13000]}]}, issues=[])
        moved = [e for e in doc.modelspace().query('INSERT[name=="WRAP"]')]
        _ok("deep-move-rejected", not results[0].get("ok") and abs(moved[0].dxf.insert.x - 15000) < 1e-6, str(_brief(results)))
        # 嵌套在普通层宿主里的保护内容（block 层上的插入）同样要拦住父块
        doc2 = _new_doc()
        plain = doc2.blocks.new("PLAIN_HOST")
        plain.add_blockref("LEAF2", (0, 0), dxfattribs={"layer": "图框"})
        doc2.blocks.new("LEAF2").add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True)
        doc2.modelspace().add_blockref("PLAIN_HOST", (9000, 9000), dxfattribs={"layer": "设备"})
        src2 = root / "plain.dxf"
        doc2.saveas(str(src2))
        doc3, results = _apply_memory(src2, {"deletes": [{"block": "PLAIN_HOST", "xy": [9000, 9000]}]}, issues=[])
        _ok("deep-plain-host-with-protected-child", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)))
        _ok("deep-plain-host-kept", _insert_names(doc3) == ["PLAIN_HOST"])


def test_layer0_cycles_and_unsupported_fail_safe() -> None:
    """自引用/循环块、镜像比例、绑定外部参照：判不清就按保护处理（宁可不改）。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # 自引用块（用原始标签写出 A 里插 A）
        doc = _new_doc()
        a = doc.blocks.new("LOOP")
        a.add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True, dxfattribs={"layer": "0"})
        a.add_blockref("LOOP", (5, 5), dxfattribs={"layer": "0"})
        doc.modelspace().add_blockref("LOOP", (8000, 3000), dxfattribs={"layer": "设备"})
        doc.saveas(str(root / "loop.dxf"))
        doc = apply._read_doc(root / "loop.dxf")
        entry = doc.modelspace().query('INSERT[name=="LOOP"]').first
        rec = planschema.analyze_insert(entry)
        _ok("cycle-analysis-flag", rec.get("cycle") is True and rec.get("protected") is True, str(rec))
        doc, results = _apply_memory(root / "loop.dxf", {"deletes": [{"block": "LOOP", "xy": [8000, 3000]}]}, issues=[])
        _ok("cycle-delete-kept", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)))
        _ok("cycle-delete-loop-still-there", _insert_names(doc) == ["LOOP"], str(_insert_names(doc)))

        # 镜像比例：渲染器不认镜像下 0 层的继承（炸开后落在 0 层），按保护处理
        doc2 = _new_doc()
        sym = doc2.blocks.new("MIRRORED")
        sym.add_lwpolyline([(0, 0), (100, 0), (100, 10)], close=True, dxfattribs={"layer": "0"})
        sym.add_text("镜像字", dxfattribs={"height": 5, "layer": "0", "insert": (10, 5)})
        doc2.modelspace().add_blockref("MIRRORED", (8000, 5000), dxfattribs={"layer": "设备", "xscale": -1})
        doc2.saveas(str(root / "mirror.dxf"))
        doc2 = apply._read_doc(root / "mirror.dxf")
        mir = doc2.modelspace().query('INSERT[name=="MIRRORED"]').first
        _ok("cycle-mirror-analysis", planschema.analyze_insert(mir).get("protected") is True, str(planschema.analyze_insert(mir)))
        doc3, results = _apply_memory(root / "mirror.dxf", {"deletes": [{"block": "MIRRORED", "xy": [8000, 5000]}]}, issues=[])
        _ok("cycle-mirror-delete-kept", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)))
        _ok("cycle-mirror-kept", _insert_names(doc3) == ["MIRRORED"])

        # 外部参照（XREF 标记）：内容不在本图里，改不动也判不清，按保护处理
        doc4 = _new_doc()
        xb = doc4.blocks.new("XREFHOST", dxfattribs={"flags": 4})
        xb.add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True)
        doc4.modelspace().add_blockref("XREFHOST", (8000, 7000), dxfattribs={"layer": "设备"})
        doc4.saveas(str(root / "xref.dxf"))
        doc5, results = _apply_memory(root / "xref.dxf", {"deletes": [{"block": "XREFHOST", "xy": [8000, 7000]}]}, issues=[])
        _ok("cycle-xref-delete-kept", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)))
        _ok("cycle-xref-kept", _insert_names(doc5) == ["XREFHOST"])

        # 线性链超过深度上限（60 层）：判不清，按保护处理，且不递归到爆栈
        doc6 = _new_doc()
        for i in range(61):
            b = doc6.blocks.new(f"CH{i}")
            if i:
                b.add_blockref(f"CH{i - 1}", (0, 0), dxfattribs={"layer": "0"})
            else:
                b.add_lwpolyline([(0, 0), (1, 0), (1, 1)], close=True)
        doc6.modelspace().add_blockref("CH60", (8000, 9000), dxfattribs={"layer": "设备"})
        doc6.saveas(str(root / "deepchain.dxf"))
        doc7 = apply._read_doc(root / "deepchain.dxf")
        deep = doc7.modelspace().query('INSERT[name=="CH60"]').first
        _ok("cycle-depth-flag", planschema.analyze_insert(deep).get("protected") is True, str(planschema.analyze_insert(deep).get("flags")))
        doc8, results = _apply_memory(root / "deepchain.dxf", {"deletes": [{"block": "CH60", "xy": [8000, 9000]}]}, issues=[])
        _ok("cycle-depth-delete-kept", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)))
        _ok("cycle-depth-kept", _insert_names(doc8) == ["CH60"])


def test_layer0_inheritance_is_insert_chain() -> None:
    """layer 0 的继承只走 INSERT 链：块记录「图层」/BLOCK 头不改变继承，也不参与判断。"""
    doc = _new_doc()
    b = doc.blocks.new("PLAIN")
    b.add_lwpolyline([(0, 0), (1, 0), (1, 1)], close=True, dxfattribs={"layer": "0"})
    b.block.dxf.set("layer", "图框")          # 块记录上写的一层，ezdxf 不导出为 BLOCK 头
    holder = doc.blocks.new("HOLDER")
    holder.add_blockref("PLAIN", (0, 0), dxfattribs={"layer": "0"})
    holder.block.dxf.set("layer", "图框")
    msp = doc.modelspace()
    msp.add_blockref("PLAIN", (0, 0), dxfattribs={"layer": "设备"})
    msp.add_blockref("HOLDER", (100, 0), dxfattribs={"layer": "图签"})
    msp.add_blockref("PLAIN", (200, 0), dxfattribs={"layer": "0"})
    from ezdxf.addons.drawing import RenderContext

    refs = list(msp.query("INSERT"))
    ctx = RenderContext(doc)
    _ok(
        "chain-own-layer-wins",
        planschema.analyze_insert(refs[0]).get("protected") is False and ctx.resolve_layer(refs[0]) == "设备",
        str(planschema.analyze_insert(refs[0])),
    )
    nested = list(holder)[0]
    _ok(
        "chain-nested-0-inherits-host",
        planschema.analyze_insert(refs[1]).get("protected") is True
        and planschema.effective_layer(nested) == "图签"
        and ctx.resolve_layer(refs[1]) == "图签",
        f"holder={planschema.analyze_insert(refs[1])} nested={planschema.effective_layer(nested)}",
    )
    # 模型空间里自己画在 0 层的 INSERT：0 层就是它的显示层，照样能改（不是「判不清」）；
    # 块里 0 层的内容也显示在 0 层，所以这里不受保护。
    _ok(
        "chain-0-insert-resolved",
        planschema.analyze_insert(refs[2]).get("protected") is False
        and planschema.effective_layer(refs[2]) == "0"
        and not planschema.is_protected_layer(planschema.effective_layer(refs[2])),
        str(planschema.analyze_insert(refs[2])),
    )


def test_layer0_preview_apply_parity() -> None:
    """真实入口：预览、应用都不动图框；sources/快照/历史都不留痕。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, proj, _i = _fresh(root, "entry0.dxf")
        src0 = src.read_bytes()
        plan_ops = {
            "delete": {"mode": "plan", "reply": "x", "deletes": [{"block": "FRAME_SYM", "xy": [15000, 13500]}]},
            "move": {"mode": "plan", "reply": "x", "moves": [{"block": "FRAME_SYM", "from": [15000, 13500], "to": [14000, 13000]}]},
            "replace": {"mode": "plan", "reply": "x", "replaces": [{"from_block": "FRAME_SYM", "from_xy": [15000, 13500], "block": "BASIN", "xy": [15000, 13500]}]},
        }
        for op, plan in plan_ops.items():
            for label, call in (
                ("preview", lambda p=plan, o=op: apply.render_plan_preview(proj, p, REGION, root / f"{o}.png")),
                ("apply", lambda p=plan, o=op: apply.apply_plan(proj, p, region=REGION, message_id=o)),
            ):
                try:
                    call()
                except apply.PlanBlockedError as exc:
                    _ok(f"entry0-{op}-{label}-blocked", "protected_layer" in exc.codes, str(exc.codes))
                else:
                    raise AssertionError(f"FAIL entry0-{op}-{label}: 整条只有图框这一项，却没有整体阻断")
            _ok(f"entry0-{op}-no-work", not apply.work_path(proj).is_file())
            _ok(f"entry0-{op}-no-preview-file", not (root / f"{op}.png").exists())
        _ok("entry0-source-kept", _count_name(src, "FRAME_SYM") == 1)
        # 源文件一个字节都没动：块里 0 层的「图名」文字也还在（就在源文件的字节里）。
        _ok("entry0-source-bytes-kept", src.read_bytes() == src0)


def test_layer0_import_does_not_bypass() -> None:
    """库里导入一个图层本身是保护层的新块：不能被当成「可插入」的新块。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "import.dxf"
        _layer0_wrapper_doc(src)
        lib = root / "lib"
        lib.mkdir()
        libdoc = ezdxf.new("R2010", setup=True)
        lb = libdoc.blocks.new("TITLEBLOCK_LIB")
        lb.add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True, dxfattribs={"layer": "TITLE"})
        lbdoc_path = lib / "titlelib.dxf"
        libdoc.saveas(str(lbdoc_path))
        index = {"schema": "x", "blocks": [{"name": "TITLEBLOCK_LIB", "library": "titlelib", "source": {"path": str(lbdoc_path), "file": "titlelib"}}]}
        with mock.patch.object(library, "get_index", return_value=index), mock.patch.object(library, "index_path", return_value=lib / "index.json"):
            doc, results = _apply_memory(src, {"inserts": [{"block": "TITLEBLOCK_LIB", "xy": [9000, 3000]}]}, issues=[])
            _ok("import-protected-insert-rejected", not results[0].get("ok") and results[0].get("issue_code") == "protected_layer", str(_brief(results)))
            _ok("import-protected-insert-not-in-doc", "TITLEBLOCK_LIB" not in _insert_names(doc))
            _ok("import-protected-block-not-added", "TITLEBLOCK_LIB" not in doc.blocks, str(list(doc.blocks)))
            # 新块本身干净、但插入图层是保护层：在写图之前就拦下，且不导入
            try:
                apply.apply_mutations(apply._read_doc(src), {"inserts": [{"block": "BASIN", "xy": [9000, 3000], "layer": "图框"}]}, bbox=BOX, layer="L", region=REGION, issues=[])
            except apply.PlanBlockedError as exc:
                _ok("import-protected-layer-insert-rejected", "protected_layer" in exc.codes, str(exc.codes))
            else:
                raise AssertionError("FAIL import-protected-layer-insert-rejected: 指定图框层的插入被执行了")
            _ok("import-clean-still-ok", True)


def test_layer0_repeated_insert_keeps_layer():
    """同一个块插第二遍（定义已在本图里）也照样落进申请的新图层，不多插、也不改旧插入。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, proj, _i = _fresh(root, "again.dxf")
        doc = apply._read_doc(src)
        results = apply.apply_mutations(
            doc,
            {"inserts": [{"block": "BASIN", "xy": [7000, 11000]}, {"block": "SHARED_HOST", "xy": [16000, 4000]}]},
            bbox=BOX,
            layer="图腾-新增-1",
            region=REGION,
            issues=[],
        )
        _ok("again-insert-ok", results[0].get("ok") is True, str(_brief(results)))
        added = [e for e in doc.modelspace().query('INSERT[name=="BASIN"]') if abs(e.dxf.insert.x - 7000) < 1]
        _ok("again-insert-new-layer", len(added) == 1 and added[0].dxf.layer == "图腾-新增-1", str([(e.dxf.insert, e.dxf.layer) for e in added]))
        _ok("again-insert-old-kept", _insert_names(doc).count("BASIN") == 3, str(_insert_names(doc)))
        both = [e for e in doc.modelspace().query('INSERT[name=="SHARED_HOST"]') if abs(e.dxf.insert.x - 16000) < 1]
        _ok(
            "again-insert-nested-still-blocked",
            not results[1].get("ok") and results[1].get("issue_code") == "protected_layer" and both == [],
            str(_brief(results)[1:2]),
        )


def test_layer0_insert_with_protected_attrib_blocked() -> None:
    """INSERT 自己的属性（ATTRIB）也参与保护判断：属性在 0 层就跟着 INSERT 走，在保护层就跟着保护层。"""
    from app import planschema

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "attrib.dxf"
        doc = _new_doc()
        seat = doc.blocks.new("TAGGED_SEAT")
        _square(seat, 400)
        seat.add_attdef("编号", (0, 0), dxfattribs={"layer": "0", "height": 20})
        msp = doc.modelspace()
        plain = msp.add_blockref("TAGGED_SEAT", (4000, 4000), dxfattribs={"layer": "设备"})
        plain.add_attrib("编号", "P-01", (0, 0), dxfattribs={"layer": "0", "height": 20})
        framed = msp.add_blockref("TAGGED_SEAT", (8000, 4000), dxfattribs={"layer": "设备"})
        framed.add_attrib("编号", "F-01", (0, 0), dxfattribs={"layer": "图框", "height": 20})
        zeroed = msp.add_blockref("TAGGED_SEAT", (12000, 4000), dxfattribs={"layer": "0"})
        zeroed.add_attrib("编号", "Z-01", (0, 0), dxfattribs={"layer": "图框", "height": 20})
        doc.saveas(str(src))
        doc = apply._read_doc(src)
        refs = {e.dxf.insert.x: e for e in doc.modelspace().query("INSERT")}
        _ok("attrib-0-inherits-insert", planschema.effective_layer(refs[4000.0].attribs[0]) == "设备", planschema.effective_layer(refs[4000.0].attribs[0]))
        _ok("attrib-plain-not-protected", planschema.analyze_insert(refs[4000.0]).get("protected") is False, str(planschema.analyze_insert(refs[4000.0])))
        _ok(
            "attrib-protected-analysis",
            planschema.analyze_insert(refs[8000.0]).get("protected_where") == "图框",
            str(planschema.analyze_insert(refs[8000.0])),
        )
        # 整条只有这一项：预览 / 应用都在建副本之前拦下，原因码与说明如实。
        plan = {"mode": "plan", "reply": "删掉带图框属性的块。", "deletes": [{"block": "TAGGED_SEAT", "xy": [8000, 4000]}]}
        try:
            apply.render_plan_preview(_proj(root / "attrib-sess", src), plan, REGION, root / "attrib.png")
        except apply.PlanBlockedError as exc:
            _ok("attrib-preview-blocked", "protected_layer" in exc.codes, str(exc.codes))
            _ok("attrib-preview-reason-cn", _has_cn(exc.result.issue_dicts()[0].get("message")), str(exc))
        else:
            raise AssertionError("FAIL attrib-preview-blocked: 属性在图框层的插入居然预览成功")
        proj = _proj(root / "attrib-sess", src)
        try:
            apply.apply_plan(proj, plan, region=REGION, message_id="attrib")
        except apply.PlanBlockedError as exc:
            _ok("attrib-apply-blocked", "protected_layer" in exc.codes, str(exc.codes))
            _ok("attrib-apply-reason-cn", _has_cn(exc.result.issue_dicts()[0].get("message")), str(exc))
        else:
            raise AssertionError("FAIL attrib-apply-blocked: 属性在图框层的插入居然删掉了")
        _ok("attrib-apply-no-write", not apply.work_path(proj).is_file() and _count_name(src, "TAGGED_SEAT") == 3)
        doc = apply._read_doc(src)
        results = apply.apply_mutations(doc, {"deletes": [{"block": "TAGGED_SEAT", "xy": [4000, 4000]}]}, bbox=BOX, layer="L", region=REGION, issues=[])
        _ok("attrib-plain-delete-ok", results[0].get("ok") is True, str(_brief(results)))
        doc = apply._read_doc(src)
        results = apply.apply_mutations(doc, {"deletes": [{"block": "TAGGED_SEAT", "xy": [12000, 4000]}]}, bbox=BOX, layer="L", region=REGION, issues=[])
        _ok(
            "attrib-zero-insert-protected",
            not results[0].get("ok") and results[0].get("issue_code") == "protected_layer",
            str(_brief(results)),
        )


def _library_index(lib: Path, name: str, make) -> dict:
    """造一份临时组件库（一份 DXF + 索引），返回给 library.get_index 的假索引。"""
    libdoc = ezdxf.new("R2010", setup=True)
    make(libdoc)
    path = lib / f"{name.lower()}.dxf"
    libdoc.saveas(str(path))
    return {"schema": "x", "blocks": [{"name": name, "library": name.lower(), "source": {"path": str(path), "file": name.lower()}}]}


def test_add_insert_probe_clean() -> None:
    """_add_insert 的试插只落在隔离副本里：拦下时本图原样，成功时库块真的导进来并插上。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lib = root / "lib"
        lib.mkdir()
        src = root / "probe.dxf"
        _layer0_wrapper_doc(src)

        def _protect_lib(d):
            b = d.blocks.new("LIB_FRAME")
            b.add_lwpolyline([(0, 0), (10, 0), (10, 10)], close=True, dxfattribs={"layer": "标题"})

        def _clean_lib(d):
            b = d.blocks.new("LIB_BASIN")
            _square(b, 300)
            b.add_attdef("编号", (0, 0), dxfattribs={"layer": "0", "height": 20})

        protect_index = _library_index(lib, "LIB_FRAME", _protect_lib)
        clean_index = _library_index(lib, "LIB_BASIN", _clean_lib)

        def _guard_fails(_doc, ref, _found):
            return {"ok": False, "reason": "模拟：新块里判出保护内容", "block": str(ref.dxf.name), "issue_code": "protected_layer"}

        # 1) 本图已有这个块 + 守卫拦下：本图实体、块定义一个都不变（只多一层准备写入的
        #    图腾-新增-* 图层，和写图前 _ensure_layer 的既有行为一致），试插只落在副本里。
        doc = apply._read_doc(src)
        before = {e.dxf.handle for e in doc.entitydb.values()}
        blocks_before = sorted(r.dxf.name for r in doc.blocks.block_records)
        layers_before = sorted(layer.dxf.name for layer in doc.layers)
        with mock.patch.object(apply, "_guard_new_block", side_effect=_guard_fails):
            out = apply._add_insert(doc, doc.modelspace(), {"block": "图框", "xy": [12000, 3000]}, "图腾-新增-1", BOX)
        _ok("probe-guard-blocked", out.get("ok") is False and out.get("issue_code") == "protected_layer", str(out))
        stray = {h for h in (e.dxf.handle for e in doc.entitydb.values()) if h not in before}
        stray = {h for h in stray if doc.entitydb[h].dxftype() != "LAYER"}
        _ok("probe-guard-no-entity-left", stray == set(), str(sorted(stray)))
        _ok("probe-guard-inserts-unchanged", _insert_names(doc).count("图框") == 2, str(_insert_names(doc)))
        _ok("probe-guard-block-kept", "图框" in doc.blocks and sorted(r.dxf.name for r in doc.blocks.block_records) == blocks_before)
        after_layers = set(layer.dxf.name for layer in doc.layers) - set(layers_before)
        _ok("probe-guard-only-layer-added", all(n.startswith("图腾-新增-") for n in after_layers), str(sorted(after_layers)))

        # 2) 库里才有 + 守卫拦下：不导入、不在本图留试插。
        doc = apply._read_doc(src)
        with mock.patch.object(library, "get_index", return_value=protect_index), mock.patch.object(
            library, "index_path", return_value=lib / "index.json"
        ), mock.patch.object(apply, "_guard_new_block", side_effect=_guard_fails):
            out = apply._add_insert(doc, doc.modelspace(), {"block": "LIB_FRAME", "xy": [12000, 3000]}, "图腾-新增-1", BOX)
        _ok("probe-lib-guard-blocked", out.get("ok") is False, str(out))
        _ok("probe-lib-guard-not-imported", "LIB_FRAME" not in doc.blocks and "LIB_FRAME" not in _insert_names(doc))

        # 3) 库里的干净块：导入进本图并真的插上（同一个块的第二遍也一样）。
        doc = apply._read_doc(src)
        with mock.patch.object(library, "get_index", return_value=clean_index), mock.patch.object(
            library, "index_path", return_value=lib / "index.json"
        ):
            results = apply.apply_mutations(
                doc,
                {"inserts": [{"block": "LIB_BASIN", "xy": [9000, 3000]}, {"block": "LIB_BASIN", "xy": [11000, 3000]}]},
                bbox=BOX,
                layer="图腾-新增-1",
                region=REGION,
                issues=[],
            )
        _ok("probe-lib-import-ok", all(r.get("ok") for r in results), str(_brief(results)))
        _ok("probe-lib-imported-flag", results[0].get("imported") is True and not results[1].get("imported"), str(_brief(results)))
        _ok("probe-lib-block-in-doc", "LIB_BASIN" in doc.blocks)
        placed = [e for e in doc.modelspace().query('INSERT[name=="LIB_BASIN"]')]
        _ok(
            "probe-lib-inserted-once-per-plan",
            sorted(round(e.dxf.insert.x) for e in placed) == [9000, 11000] and all(e.dxf.layer == "图腾-新增-1" for e in placed),
            str([(e.dxf.insert, e.dxf.layer) for e in placed]),
        )
        _ok(
            "probe-lib-handles-live",
            all(doc.entitydb.get(r.get("handle")) is not None for r in results),
            str([r.get("handle") for r in results]),
        )


# ================================================================ H. 同角色别名冲突


def test_alias_conflicts_blocked() -> None:
    cases = {
        "delete-xy-fromxy": ({"deletes": [{"block": "A", "xy": [0, 0], "from_xy": [1000, 0]}]}, "delete"),
        "move-from-fromxy": ({"moves": [{"block": "A", "from": [0, 0], "from_xy": [1000, 0], "to": [2000, 0]}]}, "move"),
        "move-to-xy": ({"moves": [{"block": "A", "from": [0, 0], "to": [2000, 0], "xy": [3000, 0]}]}, "move"),
        "replace-fromxy-from": ({"replaces": [{"from_block": "A", "block": "B", "from_xy": [0, 0], "from": [1000, 0], "xy": [0, 0]}]}, "replace"),
        "delete-block-fromblock": ({"deletes": [{"block": "A", "from_block": "B", "xy": [0, 0]}]}, "delete"),
        "insert-xy-to": ({"inserts": [{"block": "A", "xy": [1, 2], "to": [3, 4]}]}, "insert"),
    }
    for name, (body, op) in cases.items():
        res = planschema.normalize_plan({"mode": "plan", "reply": "x", **body})
        codes = [i.get("code") for i, _ in res.blockers]
        # 冲突的条目整个不执行：既不写图，也不悄悄按第一个执行
        _ok(f"alias-conflict-not-executed:{name}", not res.plan.get(f"{op}s"), str(res.plan))
        _ok(f"alias-conflict-flagged:{name}", any(i.get("code") == "alias_conflict" for i, _ in res.warnings), str([i.get("message") for i, _ in res.warnings]))
        # 全篇只有这一处冲突：没有能确定位置的改动 → 问用户
        _ok(f"alias-conflict-clarify:{name}", not res.valid and "alias_conflict" in codes and res.needs_clarification, f"{body} -> {codes} {res.status}")
        _ok(f"alias-conflict-message:{name}", all(_has_cn(i.get("message")) for i, _ in res.blockers), str([i.get("message") for i, _ in res.blockers]))
        _ok(f"alias-conflict-op:{name}", any(i.get("op") == op for i, _ in res.warnings if i.get("code") == "alias_conflict"), str([(i.get("op"), i.get("index")) for i, _ in res.warnings]))

    # 同一份方案里还有别的能执行的改动：照常预览/应用，冲突的那条只作提醒（不是 blocker）
    res = planschema.normalize_plan(
        {"mode": "plan", "reply": "x", "inserts": [{"block": "C", "xy": [5, 5]}], "moves": [{"block": "A", "from": [0, 0], "from_xy": [1000, 0], "to": [2000, 0]}]}
    )
    _ok("alias-conflict-among-valid-not-blocking", res.valid and res.plan["moves"] == [], str(res.plan))
    _ok("alias-conflict-among-valid-warns", any(i.get("code") == "alias_conflict" for i, _ in res.warnings) and "两种说法" in planschema.warning_message(res), planschema.warning_message(res))

    # 二次别名写坏了也不能因为 or 短路被跳过
    for name, body in {
        "move-malformed-secondary": {"moves": [{"block": "A", "from": [0, 0], "to": [2000, 0], "xy": "bad"}]},
        "move-malformed-secondary-x": {"moves": [{"block": "A", "from": [0, 0], "to": [2000, 0], "xy": [True, 0]}]},
        "delete-malformed-secondary": {"deletes": [{"block": "A", "xy": [0, 0], "from_xy": [1]}]},
        "replace-malformed-secondary": {"replaces": [{"from_block": "A", "block": "B", "from_xy": [0, 0], "xy": [0, 0], "from": "bad"}]},
    }.items():
        res = planschema.normalize_plan({"mode": "plan", "reply": "x", **body})
        _ok(f"alias-malformed-blocked:{name}", bool(res.blockers), f"{body} -> {[i.get('message') for i, _ in res.blockers]}")

    # 同名替换（旧块和新块同一个）不算别名冲突，照旧交给写图端
    res = planschema.normalize_plan({"mode": "plan", "reply": "x", "replaces": [{"from_block": "A", "block": "A", "from_xy": [0, 0], "xy": [0, 0]}]})
    _ok("alias-same-block-not-conflict", res.valid, str([i.get("message") for i, _ in res.blockers]))

    # 同一别名写两遍且一致：不算冲突
    res = planschema.normalize_plan({"mode": "plan", "reply": "x", "deletes": [{"block": "A", "xy": [100, 200], "from_xy": [100.0, 200.0]}]})
    _ok("alias-consistent-ok", res.valid and res.plan["deletes"][0]["xy"] == [100.0, 200.0], str(res.plan))
    res = planschema.normalize_plan({"mode": "plan", "reply": "x", "inserts": [{"block": "A", "xy": [1, 2], "to": [1.0, 2.0]}]})
    _ok("alias-consistent-insert-ok", res.valid and res.plan["inserts"][0]["xy"] == [1.0, 2.0], str(res.plan))

    # 替换：起点用 from_xy/from、终点用 xy/to，是不同角色，各自一致就合法
    res = planschema.normalize_plan({"replaces": [{"from_block": "A", "from_xy": [0, 0], "from": [0, 0], "block": "B", "xy": [5, 0], "to": [5, 0]}]})
    _ok("alias-replace-source-dest-distinct", res.valid and not res.warnings, str([i.get("message") for i, _ in res.blockers]))
    _ok("alias-replace-source-dest-values", res.plan["replaces"][0]["from_xy"] == [0.0, 0.0] and res.plan["replaces"][0]["xy"] == [5.0, 0.0], str(res.plan["replaces"]))
    # 替换：起点与终点写成同一处也合法（原地替换）
    res = planschema.normalize_plan({"replaces": [{"from_block": "A", "from_xy": [0, 0], "block": "B", "xy": [0, 0]}]})
    _ok("alias-replace-in-place-ok", res.valid, str([i.get("message") for i, _ in res.blockers]))

    # 入口同样拦住：apply_mutations 即便传 issues=[] 也不能按「第一个」执行
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, _p, _i = _fresh(root, "alias.dxf")
        try:
            apply.apply_mutations(apply._read_doc(src), {"deletes": [{"block": "BASIN", "xy": [2000, 2000], "from_xy": [8000, 2000]}]}, bbox=BOX, layer="L", region=REGION, issues=[])
        except apply.PlanBlockedError as exc:
            _ok("alias-entry-blocked", "alias_conflict" in exc.codes, str(exc.codes))
        else:
            raise AssertionError("FAIL alias-entry-blocked: 冲突别名被执行了")
        # 真实入口用的是一份新的源图（前一步的 apply_mutations 已在这张内存图上删过东西，
        # 且那些改动不该写盘）。
        # 归一化把冲突的那条剔成提醒（永不按「第一个」执行）；真实入口因此只执行能确定的那一项。
        src, proj, _info = _fresh(root, "alias-entry.dxf")
        entry_plan = {
            "deletes": [{"block": "BASIN", "xy": [2000, 2000], "from_xy": [8000, 2000]}],
            "inserts": [{"block": "SEAT_NEW", "xy": [3000, 9000]}],
        }
        out = apply.apply_plan(proj, entry_plan, region=REGION, message_id="a")
        _ok(
            "alias-entry-apply-not-guessing",
            out["ok_count"] == 1 and [r.get("action") for r in out["results"]] == ["insert"],
            str(_brief(out["results"])),
        )
        _ok("alias-entry-apply-both-kept", _count_name(apply.work_path(proj), "BASIN") == 2)
        _ok("alias-entry-both-kept", _count_name(src, "BASIN") == 2)


# ================================================================ I. 修复轮：重新归一化


def _repair_case(root: Path, first: dict, repaired: str) -> tuple[dict, dict]:
    """第一轮给出带坐标错误的方案触发修复，第二轮返回 repaired。"""
    from app import apply as _apply

    proj, info = _session_on_disk(root)
    # 与已有 BASIN 重叠 → placement 报坐标错误，进入修复轮；首轮顺带一个多余字段（warning）。
    first = dict(first)
    first.setdefault("inserts", [{"block": "BASIN", "xy": [2100, 2100], "colour": 3}])
    first_text = json.dumps(first, ensure_ascii=False)
    events, calls = _run_chat(proj, "女卫里加一个台盆", [first_text, repaired])
    msg = _last_assistant(events)
    return proj, {"msg": msg, "events": events, "calls": calls}


def test_chat_repair_renormalized() -> None:
    """修复稿本身坏了（结构错 / 保护层 / 别名冲突 / 非 JSON / 不是改图）：整条丢弃，
    保留第一轮那版结构合法的方案（它可能还有坐标提示），照常预览、等用户确认，不改成 clarify。"""
    first_xy = [2100.0, 2100.0]
    bad_repaired = [
        # (修复稿, 用来确认「坏稿的改动没被采纳」的特征)
        (json.dumps({"mode": "plan", "reply": "加台盆。", "inserts": [{"block": "BASIN", "xy": [True, 2000]}]}, ensure_ascii=False), lambda p: True),
        (json.dumps({"mode": "plan", "reply": "加台盆。", "inserts": [{"block": "BASIN", "xy": [3000, 3000], "layer": "图框"}]}, ensure_ascii=False), lambda p: "图框" not in str(p)),
        (
            json.dumps({"mode": "plan", "reply": "加台盆。", "deletes": [{"block": "BASIN", "xy": [2000, 2000], "from_xy": [8000, 2000]}]}, ensure_ascii=False),
            lambda p: p.get("deletes") in (None, []),
        ),
        ("这不是 JSON", lambda p: True),
        (json.dumps({"mode": "answer", "reply": "不加了。"}, ensure_ascii=False), lambda p: p.get("mode") == "plan"),
    ]
    for i, (repaired, candidate_rejected) in enumerate(bad_repaired):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj, out = _repair_case(root, {"mode": "plan", "reply": "加台盆。"}, repaired)
            msg = out["msg"]
            plan = msg.get("plan") or {}
            inserts = plan.get("inserts") or []
            bad = str(repaired)[:60]
            # 第一轮那版原样保留；修复稿里的 3 个坐标、图框层、删除、answer 都没被采纳。
            _ok(
                f"repair-invalid-keeps-first-round:{i}",
                [x.get("xy") for x in inserts] == [first_xy] and [x.get("block") for x in inserts] == ["BASIN"],
                f"{bad} -> plan={plan}",
            )
            _ok(f"repair-invalid-candidate-discarded:{i}", candidate_rejected(plan), f"{bad} -> plan={plan}")
            _ok(
                f"repair-invalid-plan-mode:{i}",
                msg.get("mode") == "plan" and not any(x.get("layer") for x in inserts),
                f"{bad} -> mode={msg.get('mode')} inserts={inserts}",
            )
            pending = apply.last_pending_plan(proj)
            _ok(
                f"repair-invalid-pending-first-round:{i}",
                (pending or {}).get("plan", {}).get("inserts") == plan.get("inserts"),
                f"{bad} -> {pending}",
            )
            _ok(f"repair-invalid-two-calls:{i}", len(out["calls"]) == 2, f"calls={len(out['calls'])}")


def test_chat_repair_propagates_accepted_schema() -> None:
    repaired = json.dumps({"mode": "plan", "reply": "台盆放到女卫。", "inserts": [{"block": "BASIN", "xy": [6060, 2000], "colour": 4}]}, ensure_ascii=False)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        proj, out = _repair_case(root, {"mode": "plan", "reply": "加台盆。"}, repaired)
        plan = out["msg"].get("plan") or {}
        _ok("repair-accepted-two-calls", len(out["calls"]) == 2, f"calls={len(out['calls'])}")
        _ok("repair-accepted-plan-kept", [i.get("xy") for i in plan.get("inserts") or []] == [[6060.0, 2000.0]], str(plan.get("inserts")))
        _ok("repair-accepted-schema-fresh", (plan.get("schema") or {}).get("status") == "valid_with_warnings", str(plan.get("schema")))
        issues = plan.get("issues") or []
        _ok("repair-accepted-warning-propagated", any(i.get("code") == "unknown_key" and "colour" in str(i.get("message")) for i in issues), str(issues))
        _ok("repair-accepted-warning-text", "多余字段" in str(out["msg"].get("text")), str(out["msg"].get("text")))
        schema = plan.get("schema") or {}
        _ok(
            "repair-accepted-schema-fresh",
            "colour" in str([w.get("message") for w in schema.get("warnings") or []]),
            f"schema 应是修复后那一版的（新增处的多余字段 colour）：{schema}",
        )
        # 「不是旧的」指没有第一轮那条 colour: 3 的提示、unknown_key 只出现一次；修复稿自己也
        # 带 colour: 4，所以这一版的 schema.warnings 里必须正好是它那一条（不是被清空）。
        _ok(
            "repair-accepted-issues-not-stale",
            len([i for i in issues if i.get("code") == "unknown_key"]) <= 1 and not any('colour": 3' in str(i) for i in issues),
            str(issues),
        )
        _ok(
            "repair-accepted-schema-warnings-kept",
            [w.get("code") for w in schema.get("warnings") or []] == ["unknown_key"]
            and 'colour": 3' not in str(schema.get("warnings")),
            str(schema.get("warnings")),
        )
        _ok("repair-accepted-warning-once", str(out["msg"].get("text")).count("多余字段") == 1, str(out["msg"].get("text")))
        _ok("repair-accepted-pending", apply.last_pending_plan(proj) is not None)


# ================================================================ J. 删除失败与回滚


class _FailDelete(dict):
    """顶层删除抛错的 dict：键依然可用，但 msp.delete_entity 直接失败（不 destroy）。"""


def _boom(*_a, **_k):
    raise RuntimeError("simulated delete failure")


def test_delete_failure_is_not_success() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src, proj, _i = _fresh(root, "delfail.dxf")
        with mock.patch.object(ezdxf.layouts.Modelspace, "delete_entity", side_effect=_boom), mock.patch.object(
            ezdxf.entities.Insert, "destroy", side_effect=_boom
        ):
            doc = apply._read_doc(src)
            results = apply.apply_mutations(doc, {"deletes": [{"block": "SINK", "xy": [5000, 5000]}]}, bbox=BOX, layer="L", region=REGION, issues=[])
            _ok("delfail-delete-not-ok", not results[0].get("ok") and "SINK" in _insert_names(doc), str(_brief(results)))
            _ok("delfail-reason-cn", _has_cn(results[0].get("reason")), str(results[0].get("reason")))
            # 替换：旧块删不掉 → 新块撤回、旧块还在
            doc = apply._read_doc(src)
            results = apply.apply_mutations(doc, {"replaces": [{"from_block": "SINK", "from_xy": [5000, 5000], "block": "SEAT_NEW", "xy": [5000, 5000]}]}, bbox=BOX, layer="L", region=REGION, issues=[])
            names = _insert_names(doc)
            _ok("delfail-replace-no-half-new", "SEAT_NEW" not in names, str(names))
            _ok("delfail-replace-source-kept", "SINK" in names, str(names))
            _ok("delfail-replace-not-ok", all(not r.get("ok") for r in results), str(_brief(results)))
            _ok("delfail-replace-handles-ok", all(r.get("handle") in doc.entitydb for r in results if r.get("handle")), str(_brief(results)))
            # 真实入口：工作图与源图都保持原样，不出现「旧块没了、新块也没有」
            try:
                apply.apply_plan(proj, {"replaces": [{"from_block": "SINK", "from_xy": [5000, 5000], "block": "SEAT_NEW", "xy": [5000, 5000]}]}, region=REGION, message_id="d")
            except Exception:
                pass
        if apply.work_path(proj).is_file():
            saved = ezdxf.readfile(str(apply.work_path(proj)))
            names = [e.dxf.name for e in saved.modelspace().query("INSERT")]
            _ok("delfail-entry-saved-source-kept", "SINK" in names and "SEAT_NEW" not in names, str(names))
        _ok("delfail-entry-source", _count_name(src, "SINK") == 1 and _count_name(src, "SEAT_NEW") == 0)


def test_delete_public_api_and_attribs() -> None:
    from ezdxf.layouts import Modelspace

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "api.dxf"
        doc = _new_doc()
        old = doc.blocks.new("OLDSEAT")
        old.add_lwpolyline([(-100, -100), (100, -100), (100, 100)], close=True)
        old.add_attdef("编号", (0, 0), dxfattribs={"height": 20})
        new = doc.blocks.new("NEWSEAT")
        new.add_lwpolyline([(-100, -100), (100, -100), (100, 100)], close=True)
        new.add_attdef("编号", (0, 0), dxfattribs={"height": 20})
        ref = doc.modelspace().add_blockref("OLDSEAT", (5000, 5000), dxfattribs={"layer": "设备"})
        ref.add_attrib("编号", "W-12", (0, 0))
        doc.saveas(str(src))
        calls: list[tuple] = []
        real = Modelspace.delete_entity

        def spy(self, entity, *args, **kwargs):
            calls.append((entity.dxftype(), args, kwargs))
            return real(self, entity)

        with mock.patch.object(Modelspace, "delete_entity", spy):
            doc = apply._read_doc(src)
            results = apply.apply_mutations(doc, {"replaces": [{"from_block": "OLDSEAT", "from_xy": [5000, 5000], "block": "NEWSEAT", "xy": [5000, 5000]}]}, bbox=BOX, layer="L", region=REGION, issues=[])
        _ok("api-replace-ok", len(results) == 2 and all(r.get("ok") for r in results), str(_brief(results)))
        _ok("api-delete-single-arg", calls and all(not a and not k for _, a, k in calls), str(calls))
        new_ref = [e for e in doc.modelspace().query('INSERT[name=="NEWSEAT"]')]
        _ok("api-new-attrib-carried", bool(new_ref) and [a.dxf.text for a in new_ref[0].attribs] == ["W-12"], str([(a.dxf.tag, a.dxf.text) for a in new_ref[0].attribs]) if new_ref else "")


# ================================================================ K. 提示词与摘录口径


def test_normalize_keeps_first_pass_result() -> None:
    """写进图的那一版必须和结构校验一致：把归一化结果（带回填的 issues）再归一化一次，
    状态和内容都不能变，否则回填的 issues 会被当成模型自己编的字段、状态从 valid 掉成
    valid_with_warnings（或者反过来一直是旧值）。"""
    res = planschema.normalize_plan({"mode": "plan", "reply": "加台盆。", "inserts": [{"block": "BASIN", "xy": [2100, 2100], "colour": 3}]})
    plan = dict(res.plan)
    plan["issues"] = res.issue_dicts(kind="warnings")
    again = planschema.normalize_plan(plan)
    _ok("renormalize-status-stable", (again.status, again.valid) == (res.status, res.valid), f"{res.status} -> {again.status}")
    # 本来就是 warnings 的一版：带着回填 issues 再来一遍，状态不掉成 valid
    warn_res = planschema.normalize_plan({"mode": "plan", "reply": "x", "inserts": [{"block": "B", "xy": [1, 2], "colour": 3}]})
    plan2 = dict(warn_res.plan, issues=warn_res.issue_dicts(kind="warnings"))
    again2 = planschema.normalize_plan(plan2)
    _ok("renormalize-warnings-not-lost", warn_res.status == "valid_with_warnings" and again2.status == "valid_with_warnings", f"{warn_res.status} -> {again2.status}")
    # 本来没有 warning 的干净方案：回填空 issues 也不该凭空多出 warning
    clean_res = planschema.normalize_plan({"mode": "plan", "reply": "x", "inserts": [{"block": "B", "xy": [1, 2]}]})
    again3 = planschema.normalize_plan(dict(clean_res.plan, issues=[]))
    _ok("renormalize-clean-stays-clean", clean_res.status == "valid" and again3.status == "valid" and not again3.warnings, f"{clean_res.status} -> {again3.status}")
    _ok("renormalize-no-unknown-key", not [i for i, _ in again.warnings if i.get("code") == "unknown_key"], str([i.get("message") for i, _ in again.warnings]))
    _ok("renormalize-items-stable", again.plan.get("inserts") == res.plan.get("inserts"), str(again.plan.get("inserts")))


def test_prompt_nested_wording_safe() -> None:
    from app import indexing, prompts

    system = prompts.SYSTEM_PROMPT
    _ok("prompt-no-overlay-instruction", "改法=在模型空间覆盖" not in system, "提示词还在教模型在模型空间覆盖")
    _ok("prompt-nested-only-additive-removed", "改法只能是同位置新增" not in system)
    _ok("prompt-nested-unsupported", "共享图块" in system and "不支持" in system, "缺少对共享块内符号的「不支持」说明")
    _ok("prompt-protected-layers", "图框" in system and "图签" in system and "不能删" in system)
    _ok("prompt-delete-needs-xy", "from_xy" in system)
    excerpt = indexing.format_equipment_line({"block": "BASIN", "xy": [1, 2], "layer": "设备", "host_block": "SHARED", "shared_host": True, "nested": True, "edit_hint": "overlay"})
    _ok("excerpt-no-overlay", "改法=在模型空间覆盖" not in excerpt, excerpt)
    _ok("excerpt-says-unsupported", "不" in excerpt and "改" in excerpt, excerpt)
    from app import projects as _projects

    room = {"name": "女卫", "equipment_nearby": [{"block": "BASIN", "xy": [1, 2], "layer": "设备", "host_block": "SHARED", "edit_hint": "overlay"}]}
    lines = _projects._equipment_lines(room, full=True)
    text = "\n".join(lines)
    _ok("excerpt-projects-no-overlay", "改法=在模型空间覆盖" not in text, text)
    _ok("excerpt-projects-says-unsupported", "不" in text, text)


def main() -> int:
    test_protected_defaults()
    test_normalize_legacy_plan_unchanged()
    test_unknown_keys_dropped_with_warning()
    test_bad_operations_are_blockers()
    test_needs_clarification_ambiguous()
    test_protected_layer_rules()
    test_actual_layer_protected()
    test_protected_via_block_definitions()
    test_nested_protected_layers()
    test_text_edits_respect_protected_layers()
    test_failed_replace_insert_preserves_source()
    test_replace_success_swaps_once()
    test_move_validation_order()
    test_direct_mutation_bad_items_safe()
    test_preview_apply_parity_batch()
    test_preview_and_apply_blocked_plan()
    test_block_ambiguity_rejected()
    test_text_ambiguity_rejected()
    test_http_apply_blocked_reports_reason()
    test_chat_normalizes_before_repair()
    test_layer0_wrapper_protected()
    test_layer0_wrapper_deep_content()
    test_layer0_inheritance_is_insert_chain()
    test_layer0_cycles_and_unsupported_fail_safe()
    test_layer0_preview_apply_parity()
    test_layer0_import_does_not_bypass()
    test_layer0_repeated_insert_keeps_layer()
    test_layer0_insert_with_protected_attrib_blocked()
    test_add_insert_probe_clean()
    test_alias_conflicts_blocked()
    test_chat_repair_renormalized()
    test_chat_repair_propagates_accepted_schema()
    test_delete_failure_is_not_success()
    test_delete_public_api_and_attribs()
    test_normalize_keeps_first_pass_result()
    test_prompt_nested_wording_safe()
    print("ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(str(exc), flush=True)
        raise SystemExit(1)
