#!/usr/bin/env python3
"""文字清洗与原地改文字的回归测试（纯 ezdxf 自制图纸，不依赖真实图纸）。

覆盖业主反馈「替换的功能标点什么的都要准确点」对应的根因：

A. 文字清洗丢字/改标点：init_proj 旧贪婪清洗、indexing._clean_dxf_text 的
   反斜杠吞字、%%c/%%d/%%p 不解码、全角/半角标点被改等。
B. 没有「改已有文字」的能力：text_edits 必须原地改、保留字高/图层/样式/
   旋转/对齐，匹配逐字精确（标点有意义），歧义要拒绝，预览 == 正式写入。
C. 替换带 ATTRIB 的块时属性值要带过去。
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402

from app import apply, indexing, placement, projects, prompts, scene  # noqa: E402


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""), flush=True)


def _ezdxf_plain(ent) -> str:
    """用 ezdxf 自己的解码复核一遍（不经 app.dxftext）。"""
    kind = ent.dxftype()
    if kind == "MTEXT":
        from ezdxf.tools.text import fast_plain_mtext

        return fast_plain_mtext(ent.text)
    from ezdxf.tools.text import plain_text

    return plain_text(ent.dxf.text)


def _proj(root: Path, dxf: Path) -> dict:
    return {
        "id": "test-text",
        "name": "文字替换测试",
        "root": str(root),
        "dxf_path": str(dxf),
        "encoding": "utf-8",
        "chat_path": str(root / "chat.json"),
    }


def _roundtrip_texts(path: Path) -> list[tuple[str, str]]:
    """重新读盘，返回 [(类型, 可见文字)]。"""
    doc = ezdxf.readfile(str(path))
    out = []
    for e in doc.modelspace():
        if e.dxftype() == "TEXT":
            out.append(("TEXT", e.dxf.text))
        elif e.dxftype() == "MTEXT":
            out.append(("MTEXT", e.text))
    return out


# ---------------------------------------------------------------- A. 清洗


def test_percent_escape_exhaustive() -> None:
    """穷举评审给的字母表：转义后必须被 ezdxf 的 TEXT 与 MTEXT 解码逐字还原。"""
    import itertools

    from app import dxftext

    samples = ["坡度0.5%", "100% 满", "%%c 100%", "%50", "50%%", "Ø80 / ° / ±", "", "%"]
    alphabet = ["%", "%", "c", "d", "p", "u", "o", "k", "5", "a", " "]
    for n in range(1, 6):
        for tup in itertools.product(alphabet, repeat=n):
            samples.append("".join(tup))
    total = 0
    bad: list[tuple[str, str, str, str]] = []
    # 一张图里反复改同一个 TEXT/MTEXT，避免每个样本都建新文档
    doc = ezdxf.new("R2010")
    text_ent = doc.modelspace().add_text("x")
    mtext_ent = doc.modelspace().add_mtext("x")
    for plain in dict.fromkeys(samples):
        total += 1
        enc = dxftext.encode_text_control(plain)
        text_ent.dxf.text = enc
        mtext_ent.text = dxftext.rewrite_mtext_body("", plain)
        t_plain = _ezdxf_plain(text_ent)
        m_plain = _ezdxf_plain(mtext_ent)
        if t_plain != plain or m_plain != plain or dxftext.check_visible(enc, kind="txt") != plain:
            bad.append((plain, enc, t_plain, m_plain))
    _ok("percent-exhaustive-mtext", not [b for b in bad if b[3] != b[0]], f"{len(bad)} bad; e.g. {bad[:4]}")
    _ok("percent-exhaustive-text", not bad, f"{len(bad)} bad of {total}; e.g. {bad[:4]}")


def test_cleaner_reproduced_bugs() -> None:
    """两条已复现的旧清洗丢字；先证明旧算法确实坏，再证明新算法对。"""
    import re

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    m1 = msp.add_mtext(r"{\fSimSun|b0|i0;卫生间}")
    m2 = msp.add_mtext(r"{\H1.2x;主要设备表}；备注")

    # 旧 init_proj._collect_texts 的清洗（死代码已删，这里固化它的算法作对照）。
    def old_init_clean(raw: str) -> str:
        text = re.sub(r"\{\\.*?\}", "", str(raw or ""))
        text = re.sub(r"[\\][A-Za-z].*?;", "", text)
        text = re.sub(r"[{}]", "", text).strip()
        text = text.replace("\\P", " ").replace("\\p", " ")
        return re.sub(r"\s+", " ", text).strip()

    def old_index_clean(raw: str) -> str:
        text = str(raw or "")
        text = re.sub(r"\{\\([^;]*);([^{}]*)\}", r"\2", text)
        text = re.sub(r"\\[A-Za-z][^;]*;", "", text)
        text = text.replace("\\P", " ").replace("\\p", " ").replace("\\~", " ")
        text = re.sub(r"[{}]", "", text)
        text = re.sub(r"^[A-Za-z][^;\n]*\|[^;\n]*;", "", text)
        return re.sub(r"\s+", " ", text).strip()

    _ok("repro-old-init-loses-wrapped", old_init_clean(m1.text) == "", repr(old_init_clean(m1.text)))
    _ok("repro-old-init-eats-tail", old_init_clean(m2.text) == "；备注", repr(old_init_clean(m2.text)))

    from app import dxftext

    _ok("clean-wrapper-kept", dxftext.clean_entity_text(m1) == "卫生间", dxftext.clean_entity_text(m1))
    _ok("clean-wrapper-and-tail", dxftext.clean_entity_text(m2) == "主要设备表；备注", dxftext.clean_entity_text(m2))
    _ok(
        "clean-indexing-alias",
        indexing._clean_dxf_text(m2.text) == "主要设备表；备注",
        indexing._clean_dxf_text(m2.text),
    )
    # 旧 indexing 清洗的正则不认转义：\X; 会从反斜杠一路吃到后面半句的分号
    raw_tail = r"说明：主控间\H1.2;排水沟{检修口}深0.3;严禁堵塞"
    old_got = old_index_clean(raw_tail)
    _ok(
        "repro-old-index-eats-tail",
        old_got == "说明：主控间排水沟检修口深0.3;严禁堵塞",
        repr(old_got),
    )
    # 新清洗只去掉 \H1.2; 这个格式码：后面的文字和分号一个不少
    _ok(
        "clean-inline-code-keeps-body",
        dxftext.clean_mtext(raw_tail) == "说明：主控间排水沟检修口深0.3;严禁堵塞",
        dxftext.clean_mtext(raw_tail),
    )


def test_cleaner_punctuation_exact() -> None:
    from app import dxftext

    full = "卫生间（女）；给水：DN25，排水、通风“主风管”‘备用’《说明》。"
    half = 'B-1 / 2F, DN25; 50%, "实测"(±0.5)~3; #1'
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for s in (full, half):
        t = msp.add_text(s)
        m = msp.add_mtext(s)
        _ok(f"clean-text-fullwidth-roundtrip:{s[:6]}", dxftext.clean_entity_text(t) == s, dxftext.clean_entity_text(t))
        _ok(f"clean-mtext-fullwidth-roundtrip:{s[:6]}", dxftext.clean_entity_text(m) == s, dxftext.clean_entity_text(m))
    # 不做全角↔半角转换
    t_full_only = msp.add_text(full)
    _ok("no-width-conversion", dxftext.clean_entity_text(t_full_only) == full and "(" not in dxftext.clean_entity_text(t_full_only))
    # 只规整空白
    t = msp.add_text("  女卫\t  无障碍  ")
    _ok("whitespace-normalized", dxftext.clean_entity_text(t) == "女卫 无障碍", repr(dxftext.clean_entity_text(t)))
    m = msp.add_mtext(r"主要设备表\P1800×700")
    _ok("mtext-newline-normalized", dxftext.clean_mtext(m.text) == "主要设备表 1800×700", dxftext.clean_mtext(m.text))
    m2 = msp.add_mtext(r"\A1;{\fSimHei|b0;双孔回收台带车}")
    _ok("mtext-prefix-and-wrapper", dxftext.clean_mtext(m2.text) == "双孔回收台带车", dxftext.clean_mtext(m2.text))
    m3 = msp.add_mtext(r"{\fArial|b1;粗}普通")
    _ok("mtext-partial-wrapper", dxftext.clean_mtext(m3.text) == "粗普通", dxftext.clean_mtext(m3.text))


def test_cleaner_percent_codes() -> None:
    from app import dxftext

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    # TEXT 里 %%c / %%d / %%p 本身就是一个字符：%%c80 → Ø80
    t = msp.add_text("%%c80 管%%d 弯 %%p 高")
    _ok("text-decode-cdp", dxftext.clean_entity_text(t) == "Ø80 管° 弯 ± 高", dxftext.clean_entity_text(t))
    t3b = msp.add_text("%%C80 / %%D / %%P")
    _ok("text-decode-upper", dxftext.clean_entity_text(t3b) == "Ø80 / ° / ±", dxftext.clean_entity_text(t3b))
    t2 = msp.add_text("%%%50")
    _ok("text-decode-literal-percent", dxftext.clean_entity_text(t2) == "%50", dxftext.clean_entity_text(t2))
    # 最小化转义：单独一个 % 原样写；只有会成 %%x 控制码的地方才补一个 %%。
    for plain in ("%50 合格", "50% 合格", "%%c", "%%50", "100% 满"):
        enc = dxftext.encode_text_control(plain)
        t_rt = ezdxf.new("R2010").modelspace().add_text(enc)
        _ok(
            f"text-encode-roundtrip:{plain}",
            dxftext.percent_visible(enc) == plain and _ezdxf_plain(t_rt) == plain,
            f"{enc!r}->{dxftext.percent_visible(enc)!r}/{_ezdxf_plain(t_rt)!r}",
        )
    _ok("text-encode-percent", dxftext.encode_text_control("%50 合格") == "%50 合格", dxftext.encode_text_control("%50 合格"))
    _ok("text-encode-percent-escape", dxftext.encode_text_control("%%c") == "%%%%%%c", dxftext.encode_text_control("%%c"))
    _ok("text-encode-percent-keep", dxftext.encode_text_control("50% 合格") == "50% 合格", dxftext.encode_text_control("50% 合格"))
    _ok("text-encode-number-code-kept", dxftext.encode_text_control("%%50") == "%%50", dxftext.encode_text_control("%%50"))
    _ok("decode-number-code-literal", dxftext.percent_visible("%%50") == "%%50", dxftext.percent_visible("%%50"))
    m = msp.add_mtext("%%c80")
    _ok("mtext-decode-c", dxftext.clean_mtext(m.text) == "Ø80", dxftext.clean_mtext(m.text))
    m = msp.add_mtext(r"\U+00d8")
    _ok("mtext-unicode-escape", dxftext.clean_mtext(m.text) == "Ø", dxftext.clean_mtext(m.text))


# ---------------------------------------------------------------- A. 索引/场景


def test_collect_texts_exact() -> None:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_mtext(r"{\fSimSun|b0|i0;卫生间}", dxfattribs={"insert": (100, 200)})
    msp.add_mtext(r"{\H1.2x;主要设备表}；备注", dxfattribs={"insert": (300, 200)})
    msp.add_text("%%c80 管（PVC）", dxfattribs={"insert": (500, 200)})
    msp.add_text("女卫（无障碍）", dxfattribs={"insert": (700, 200)})
    recs = indexing.collect_texts(doc, msp)
    texts = [r["text"] for r in recs]
    _ok("collect-wrapper", "卫生间" in texts, str(texts))
    _ok("collect-wrapper-tail", "主要设备表；备注" in texts, str(texts))
    _ok("collect-decode-c", "Ø80 管（PVC）" in texts, str(texts))
    _ok("collect-fullwidth", "女卫（无障碍）" in texts, str(texts))

    # 摘录逐字照录：标点原样出现在送给模型的那一行里。
    line = indexing.format_text_line(
        {"text": "女卫（无障碍）", "xy": [700, 200], "layer": "房间功能"}
    )
    _ok("excerpt-exact", "女卫（无障碍）" in line and "女卫(无障碍)" not in line, line)
    try:
        from app import init_proj

        _ok("dead-init-collect-texts-removed", not hasattr(init_proj, "_collect_texts"))
    except ImportError:  # pragma: no cover - init_proj 依赖较重时跳过
        pass


def test_scene_text_payload_exact() -> None:
    from app import dxftext

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    t = msp.add_text("%%c80 管（PVC）", dxfattribs={"height": 100, "insert": (0, 0)})
    m = msp.add_mtext(r"{\fSimSun|b0|i0;卫生间}；备注", dxfattribs={"insert": (0, 500)})
    _ok("scene-text-decodes", scene._text_payload(t)["s"] == "Ø80 管（PVC）", scene._text_payload(t)["s"])
    _ok("scene-mtext-exact", scene._text_payload(m)["s"] == "卫生间；备注", scene._text_payload(m)["s"])
    raw_m = msp.add_mtext(r"{\C50; 女卫（无障碍）}")
    p = scene._text_payload(raw_m)
    _ok("scene-mtext-punctuation", p["s"] == "女卫（无障碍）", p["s"])
    _ok("scene-uses-shared-cleaner", dxftext.clean_mtext(raw_m.text) == p["s"])
    half = msp.add_text("女卫(无障碍)", dxfattribs={"insert": (0, 0)})
    _ok("scene-keeps-halfwidth", scene._text_payload(half)["s"] == "女卫(无障碍)", scene._text_payload(half)["s"])


# ---------------------------------------------------------------- B. 改文字


def _label_drawing(path: Path) -> dict:
    """一份带标注的小图：TEXT、包格式的 MTEXT、带样式的文字、重名的两处文字。"""
    doc = ezdxf.new("R2010")
    doc.styles.add("HZ", font="simsun.ttc")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (20000, 0), (20000, 15000), (0, 15000)], close=True, dxfattribs={"layer": "000PL"})
    t = msp.add_text("女卫（无障碍）", dxfattribs={"height": 300, "layer": "房间功能", "rotation": 90})
    t.set_placement((5000, 8000), align=ezdxf.enums.TextEntityAlignment.MIDDLE_CENTER)
    t.dxf.style = "HZ"
    t.dxf.width = 0.85
    t.dxf.color = 3
    handle = t.dxf.handle
    wrapped = msp.add_mtext(
        r"{\fSimSun|b0|i0;主要设备表；备注}",
        dxfattribs={"char_height": 200, "layer": "设备标注", "insert": (3000, 3000)},
    )
    prefixed = msp.add_mtext(
        r"\A1;地漏 DN50",
        dxfattribs={"char_height": 150, "layer": "设备标注", "insert": (8000, 3000), "rotation": 15},
    )
    msp.add_text("女卫", dxfattribs={"height": 300, "insert": (4000, 4000)})
    msp.add_text("女卫", dxfattribs={"height": 300, "insert": (12000, 11000)})
    msp.add_text("女卫(无障碍)", dxfattribs={"height": 300, "insert": (15000, 2000)})
    msp.add_text("疏散楼梯；不可堵", dxfattribs={"height": 300, "insert": (16000, 9000)})
    doc.saveas(str(path))
    return {
        "handle": handle,
        "mtext_handle": wrapped.dxf.handle,
        "prefix_handle": prefixed.dxf.handle,
        "bbox": [0.0, 0.0, 20000.0, 15000.0],
    }


def _find(doc, handle: str):
    for e in doc.modelspace():
        if e.dxf.handle == handle:
            return e
    return None


def _apply_in_memory(path: Path, plan: dict):
    doc = apply._read_doc(path)
    results = apply.apply_mutations(doc, plan, bbox=None, layer="图腾-新增-1")
    return doc, results


def test_text_edit_in_place() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "label.dxf"
        info = _label_drawing(src)

        plan = {
            "mode": "plan",
            "reply": "已把女卫改成女卫（无障碍）。",
            "text_edits": [
                {"from_text": "女卫", "xy": [4000, 4000], "text": "女卫（无障碍）"},
                {"from_text": "主要设备表；备注", "xy": [3000, 3000], "text": "主要设备表（更新）；见说明：1、2、3"},
                {"from_text": "地漏 DN50", "xy": [8000, 3000], "text": "地漏 DN75\\100 {改}%50"},
            ],
        }
        open_doc = apply._read_doc(src)
        tagged = _find(open_doc, info["handle"])
        tagged_plan = {
            "text_edits": [{"from_text": "女卫（无障碍）", "xy": [5000, 8000], "text": "女卫（无障碍）A"}]
        }
        open_doc, tagged_results = _apply_in_memory(src, tagged_plan)
        _ok(
            "edit-tagged-uses-align-point",
            tagged_results[0].get("ok") and tagged_results[0].get("handle") == info["handle"],
            str(tagged_results),
        )
        _ = tagged

        doc, results = _apply_in_memory(src, plan)
        _ok("edit-count", len(results) == 3 and all(r.get("ok") for r in results), str(results))
        r0, r1, r2 = results
        _ok(
            "edit-result-shape",
            r0.get("action") == "text_edit" and r0.get("from") == "女卫" and r0.get("text") == "女卫（无障碍）" and r0.get("match") == "exact",
            str(r0),
        )
        _ok("edit-keeps-handle", _find(doc, r0["handle"]) is not None, r0.get("handle"))
        ent = _find(doc, r0["handle"])
        _ok("edit-text", ent.dxf.text == "女卫（无障碍）", repr(ent.dxf.text))
        _ok("edit-is-the-nearby-one", ent.dxf.insert.x == 4000.0 and ent.dxf.insert.y == 4000.0, str(tuple(ent.dxf.insert)))

        m = _find(doc, r1["handle"])
        doc.saveas(str(root / "check.dxf"))
        m2 = next(e for e in ezdxf.readfile(str(root / "check.dxf")).modelspace() if e.dxftype() == "MTEXT" and e.dxf.layer == "设备标注")
        from app import dxftext

        _ok("mtext-wrapper-kept", m.text.startswith(r"{\fSimSun|b0|i0;"), m.text)
        _ok(
            "mtext-body-replaced",
            m.text.endswith("}") and dxftext.check_visible(m.text) == "主要设备表（更新）；见说明：1、2、3",
            m.text,
        )
        _ok("mtext-prefix-kept", m2 is not None)  # 仅确认文件可读

        p = _find(doc, r2["handle"])
        _ok("mtext-prefix-kept-2", p.text.startswith(r"\A1;"), p.text)
        _ok("mtext-escaped-braces", r"\{改\}" in p.text, p.text)
        _ok(
            "mtext-exact-visible",
            dxftext.check_visible(p.text) == "地漏 DN75\\100 {改}%50",
            dxftext.check_visible(p.text),
        )
        _ok("mtext-style-kept", abs(float(p.dxf.char_height) - 150) < 1e-6 and abs(float(p.dxf.rotation) - 15) < 1e-6)
        _ok("mtext-handle-same", r1.get("handle") == info["mtext_handle"] and r2.get("handle") == info["prefix_handle"])

        # 文件读回后逐字一致
        doc.saveas(str(root / "out.dxf"))
        doc2 = ezdxf.readfile(str(root / "out.dxf"))
        again = _find(doc2, r0["handle"])
        _ok("saved-exact-text", again.dxf.text == "女卫（无障碍）", repr(again.dxf.text))
        _ok("saved-keeps-fmt", again.dxf.layer == "0" and abs(float(again.dxf.height) - 300) < 1e-6, again.dxf.layer)


def test_text_edit_preserves_rich_format() -> None:
    """带样式的原文字：样式、字高、图层、旋转、宽度因子、对齐点、颜色全部保留。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "fmt.dxf"
        info = _label_drawing(src)
        doc = apply._read_doc(src)
        before = _find(doc, info["handle"])
        snap = {
            "style": before.dxf.style,
            "height": float(before.dxf.height),
            "layer": before.dxf.layer,
            "rotation": float(before.dxf.rotation),
            "width": float(before.dxf.width),
            "color": int(before.dxf.color),
            "halign": int(before.dxf.halign),
            "valign": int(before.dxf.valign),
            "align_point": tuple(before.dxf.align_point),
        }
        plan = {"text_edits": [{"from_text": "女卫（无障碍）", "xy": [5200, 7800], "text": "女卫"}]}
        doc2, results = _apply_in_memory(src, plan)
        res = results[0]
        _ok("fmt-edit-ok", res.get("ok") and res.get("handle") == info["handle"], str(results))
        ent = _find(doc2, info["handle"])
        _ok("fmt-style", ent.dxf.style == "HZ", ent.dxf.style)
        _ok("fmt-height", abs(float(ent.dxf.height) - snap["height"]) < 1e-6)
        _ok("fmt-layer", ent.dxf.layer == "房间功能")
        _ok("fmt-rotation", abs(float(ent.dxf.rotation) - 90) < 1e-6)
        _ok("fmt-width", abs(float(ent.dxf.width) - 0.85) < 1e-6, ent.dxf.width)
        _ok("fmt-color", int(ent.dxf.color) == 3)
        _ok("fmt-align", int(ent.dxf.halign) == snap["halign"] and int(ent.dxf.valign) == snap["valign"])
        _ok("fmt-align-point", tuple(ent.dxf.align_point) == snap["align_point"])


def test_text_edit_matching_rules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "match.dxf"
        _label_drawing(src)

        def run(item):
            # 每次都在原图的新副本上跑，前面的用例不污染后面的匹配
            doc = apply._read_doc(src)
            results = apply.apply_mutations(doc, {"text_edits": [item]}, bbox=None, layer="图腾-新增-1")
            return results[0]

        # 标点不同：精确匹配不成立；半角与全角只差宽度 → 唯一匹配，标为 width-normalized。
        # 图里「女卫(无障碍)」只有半角一处，即使 xy 点在别处，也能放宽找到。
        # （from_text 故意写成半角，图上是全角括号的那一处只差宽度。）
        r = run({"from_text": "疏散楼梯;不可堵", "xy": [16000, 9000], "text": "疏散楼梯；严禁堵塞"})
        _ok("width-normalized-unique", r.get("ok") and r.get("match") == "width-normalized", str(r))
        _ok("width-normalized-from", r.get("from") == "疏散楼梯；不可堵", str(r))
        r = run({"from_text": "主要设备表(无)", "xy": [0, 0], "text": "x"})
        _ok("no-match-plain", not r.get("ok"), str(r))
        r = run({"from_text": "女卫(无障隘)", "xy": [4300, 4200], "text": "x"})
        _ok("near-miss-rejected", not r.get("ok"), str(r))
        # 精确的「女卫（无障碍）」在图上 → 优先精确，不因 xy 靠近半角那处就放宽
        r = run({"from_text": "女卫（无障碍）", "xy": [15000, 2000], "text": "X"})
        _ok("exact-beats-width", r.get("ok") and r.get("match") == "exact" and r.get("from") == "女卫（无障碍）", str(r))

        # 图上有「女卫」两处：只给中心坐标，取最近的一处。
        r = run({"from_text": "女卫", "xy": [4100, 4100], "text": "女卫A"})
        _ok("nearest-of-two", r.get("ok") and r.get("xy") == [4000.0, 4000.0], str(r))
        # 两处一样近 → 歧义，拒绝。
        r = run({"from_text": "女卫", "xy": [8000, 7500], "text": "女卫A"})
        _ok("ambiguous-rejected", not r.get("ok") and "多处" in str(r.get("reason")), str(r))
        # 完全找不到 → 中文原因，带原文字。
        r = run({"from_text": "男卫", "xy": [4000, 4000], "text": "x"})
        _ok("missing-rejected", not r.get("ok") and "没有找到原文字「男卫」" in str(r.get("reason")), str(r))
        # 半角/全角差异且不止一个候选 → 不猜。
        _, results = _apply_in_memory(
            src,
            {"text_edits": [{"from_text": "女卫(无障隘)", "xy": [4300, 4200], "text": "x"}]},
        )
        _ok("near-miss-punct-rejected", not results[0].get("ok"), str(results[0]))

    # 子串永远不算：图上只有「女卫（无障碍）」，按「女卫」改必须失败（上面图里恰有独立的「女卫」，
    # 所以这里另起一张只含长文字的图）。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "sub.dxf"
        doc = ezdxf.new("R2010")
        doc.modelspace().add_text("女卫（无障碍）", dxfattribs={"height": 300, "insert": (1000, 1000)})
        doc.modelspace().add_text("女卫生间；主风管", dxfattribs={"height": 300, "insert": (1000, 2000)})
        doc.saveas(str(src))
        for probe in ("女卫", "女卫生间", "卫生间；主风管", "女卫（）"):
            _, results = _apply_in_memory(src, {"text_edits": [{"from_text": probe, "xy": [1000, 1500], "text": "x"}]})
            _ok(f"substring-never-matches:{probe}", not results[0].get("ok"), str(results[0]))


def test_text_edit_scope_current_partition() -> None:
    """改文字只在当前分区里找：别的分区/图幅上同样的字不能被改到。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "sheets.dxf"
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        # 本分区（左侧）里没有「设备编号」这几个字；右侧图幅上有一处精确的。
        msp.add_text("女卫", dxfattribs={"height": 300, "insert": (2000, 2000)})
        msp.add_text("设备编号", dxfattribs={"height": 300, "insert": (80000, 80000)})
        msp.add_mtext(r"{\C1;备餐间；详见说明}", dxfattribs={"char_height": 200, "insert": (90000, 80000)})
        doc.saveas(str(src))
        part = [0.0, 0.0, 20000.0, 15000.0]

        def run(item, bbox):
            doc = apply._read_doc(src)
            results = apply.apply_mutations(doc, {"text_edits": [item]}, bbox=bbox, layer="L")
            return results[0], doc

        # 1) 远处的精确匹配：不能跨分区改，而且就要说「当前分区里没有」。
        #    xy 落在本分区里（模型把别处的坐标当成了这里的），只凭坐标查不出问题。
        r, doc = run({"from_text": "设备编号", "xy": [2000, 2000], "text": "设备编号A"}, part)
        _ok("scope-exact-elsewhere-rejected", not r.get("ok"), str(r))
        _ok(
            "scope-reason-missing",
            "没有找到原文字「设备编号」" in str(r.get("reason")) and "当前分区" in str(r.get("reason")),
            str(r),
        )
        from app import dxftext as _dxf

        _ok(
            "scope-elsewhere-untouched",
            not any(_dxf.clean_entity_text(e) == "设备编号A" for e in doc.modelspace()),
            str([(e.dxftype(), _dxf.clean_entity_text(e)) for e in doc.modelspace()]),
        )
        # 2) 远处唯一一处只差宽度的：放宽第二遍也必须先过范围。
        r, _doc = run({"from_text": "备餐间;详见说明", "xy": [90000, 80000], "text": "备餐间"}, part)
        _ok("scope-width-elsewhere-rejected", not r.get("ok"), str(r))
        _ok("scope-width-reason", "当前分区" in str(r.get("reason")) or "没有找到原文字" in str(r.get("reason")), str(r))
        # 3) 分区内的照常改。
        r, _doc = run({"from_text": "女卫", "xy": [2000, 2000], "text": "女卫（无障碍）"}, part)
        _ok("scope-inside-ok", r.get("ok") and r.get("text") == "女卫（无障碍）", str(r))
        # 4) 不给分区（整图）时行为不变。
        r, _doc = run({"from_text": "设备编号", "xy": [80000, 80000], "text": "设备编号A"}, None)
        _ok("scope-no-bbox-whole-sheet", r.get("ok"), str(r))
        # 5) finder 本身也带 bbox。
        doc = apply._read_doc(src)
        found = apply._find_text_to_edit(doc.modelspace(), "设备编号", [80000, 80000], bbox=part)
        _ok("finder-accepts-bbox", not found.get("ok"), str({k: v for k, v in found.items() if k != "entity"}))


def test_text_edit_width_normalized_needs_unique() -> None:
    """放宽全/半角的第二遍只在唯一候选时采用；两处及以上归为歧义，不挑最近的。"""
    fw = chr(0xFF1B)  # 全角分号
    semi = chr(0x3B)  # 半角分号
    text_a = "备餐间" + fw + "A"
    text_b = "备餐间" + fw + "B"
    probe_a = "备餐间" + semi + "A"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "wide.dxf"
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        # 同一条文字在图上两处（都是全角分号）；from_text 用半角分号，精确匹配不成立，放宽后两处都命中。
        msp.add_text(text_a, dxfattribs={"height": 300, "insert": (1000, 1000)})
        msp.add_text(text_a, dxfattribs={"height": 300, "insert": (5000, 1000)})
        doc.saveas(str(src))

        def run(item):
            doc = apply._read_doc(src)
            results = apply.apply_mutations(doc, {"text_edits": [item]}, bbox=None, layer="L")
            return results[0]

        r = run({"from_text": probe_a, "xy": [1000, 1000], "text": "备餐间"})
        _ok("width-two-candidates-rejected", not r.get("ok"), str(r))
        _ok("width-two-candidates-ambiguous", "多处" in str(r.get("reason")), str(r))
        # 靠近另一处也一样拒绝：不允许按距离挑。
        r = run({"from_text": probe_a, "xy": [4900, 1000], "text": "备餐间"})
        _ok("width-two-candidates-rejected-near-other", not r.get("ok"), str(r))
        # 精确匹配仍然最近优先，不受影响。
        r = run({"from_text": text_a, "xy": [5100, 1100], "text": "备餐间" + fw + "C"})
        _ok("exact-nearest-still-wins", r.get("ok") and r.get("from") == text_a and r.get("xy") == [5000.0, 1000.0], str(r))

    # 只剩一处时才允许放宽，并标明 width-normalized。
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "wide1.dxf"
        doc = ezdxf.new("R2010")
        doc.modelspace().add_text(text_b, dxfattribs={"height": 300, "insert": (1000, 1000)})
        doc.modelspace().add_text("女卫", dxfattribs={"height": 300, "insert": (5000, 1000)})
        doc.saveas(str(src))
        doc = apply._read_doc(src)
        results = apply.apply_mutations(
            doc, {"text_edits": [{"from_text": "备餐间" + semi + "B", "xy": [1000, 1000], "text": "备餐间"}]}, bbox=None, layer="L"
        )
        _ok(
            "width-single-accepted",
            results[0].get("ok") and results[0].get("match") == "width-normalized",
            str(results[0]),
        )

def test_minimal_percent_escaping() -> None:
    """单独的 % 原样写，不补 %%%。回读（共享清洗器 + ezdxf）都必须逐字相等。"""
    from app import dxftext

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    t = msp.add_text("坡度占位(T)", dxfattribs={"height": 200, "insert": (1000, 1000)})
    m = msp.add_mtext(r"{\fSimSun;坡度占位(M)}", dxfattribs={"char_height": 200, "insert": (1000, 3000)})
    # 写入路径：把两条占位文字改成「坡度0.5%」。
    plan = {
        "text_edits": [
            {"from_text": "坡度占位(T)", "xy": [1000, 1000], "text": "坡度0.5%"},
            {"from_text": "坡度占位(M)", "xy": [1000, 3000], "text": "坡度0.5%"},
        ]
    }
    results = apply.apply_mutations(doc, plan, bbox=None, layer="L")
    _ok("percent-targets-right", [r.get("handle") for r in results] == [t.dxf.handle, m.dxf.handle], str(results))
    _ok("percent-edit-both-ok", all(r.get("ok") for r in results), str(results))
    _ok("percent-text-raw-exact", t.dxf.text == "坡度0.5%", repr(t.dxf.text))
    _ok("percent-mtext-no-padding", "%%%" not in m.text, repr(m.text))
    _ok("percent-mtext-visible", _ezdxf_plain(m) == "坡度0.5%", repr(_ezdxf_plain(m)))
    _ok("percent-text-ezdxf", _ezdxf_plain(t) == "坡度0.5%", repr(_ezdxf_plain(t)))
    _ok("percent-text-cleaner", dxftext.clean_entity_text(t) == "坡度0.5%", dxftext.clean_entity_text(t))
    _ok("percent-mtext-cleaner", dxftext.clean_entity_text(m) == "坡度0.5%", dxftext.clean_entity_text(m))
    _ok("percent-100-kept", dxftext.encode_text_control("100% 满") == "100% 满", dxftext.encode_text_control("100% 满"))
    # 会成控制码的字面 % 仍要转义并逐字回读。
    for plain in ("%%c 100%", "50%%", "100%", "%%"):
        enc = dxftext.encode_text_control(plain)
        t2 = msp.add_text(enc)
        _ok(
            f"percent-escape-roundtrip:{plain}",
            dxftext.clean_entity_text(t2) == plain and _ezdxf_plain(t2) == plain,
            f"{enc!r}->{dxftext.clean_entity_text(t2)!r}/{_ezdxf_plain(t2)!r}",
        )
    # MTEXT：整段外壳保留，字面 %%c 也不变。
    body = dxftext.rewrite_mtext_body(r"{\fSimSun;旧}", "%%c 100%")
    _ok("percent-mtext-wrapper-kept", body.startswith("{\\fSimSun;"), body)
    _ok("percent-mtext-roundtrip", dxftext.check_visible(body) == "%%c 100%", body)
    doc2 = ezdxf.new("R2010")
    mt = doc2.modelspace().add_mtext(body)
    _ok("percent-mtext-ezdxf-roundtrip", _ezdxf_plain(mt) == "%%c 100%", _ezdxf_plain(mt))
    # 共享清洗器与 ezdxf 自己的解码都要逐字相等。
    for plain in ("%%c", "%%p", "%%d", "%%u", "%%o", "%%k", "%%", "%50", "50% 合格", "Ø80 / ° / ±", "Ø80 管", "坡度0.5%", "100% 满", "a%%b"):
        enc = dxftext.encode_text_control(plain)
        t3 = msp.add_text(enc)
        visible = dxftext.clean_entity_text(t3)
        ezdxf_plain = _ezdxf_plain(t3)
        _ok(
            f"text-percent-roundtrip:{plain}",
            visible == plain and ezdxf_plain == plain,
            f"{enc!r}->cleaner={visible!r} ezdxf={ezdxf_plain!r}",
        )



def test_text_edit_newlines() -> None:
    """换行不能造成假失败：TEXT 换行写空格，MTEXT 写 \\P，均按同一套空白规整比对。"""
    from app import dxftext

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    t = msp.add_text("abc", dxfattribs={"height": 100, "insert": (0, 0)})
    m = msp.add_mtext("abc2", dxfattribs={"char_height": 100, "insert": (0, 500)})
    m_blank = msp.add_mtext("abc3", dxfattribs={"char_height": 100, "insert": (0, 1000)})
    m_edge = msp.add_mtext("abc4", dxfattribs={"char_height": 100, "insert": (0, 1500)})
    plan = {
        "text_edits": [
            {"from_text": "abc", "xy": [0, 0], "text": "a\nb"},
            {"from_text": "abc2", "xy": [0, 500], "text": "a \nb"},
            {"from_text": "abc3", "xy": [0, 1000], "text": "line1\n\nline3"},
            {"from_text": "abc4", "xy": [0, 1500], "text": "\n第一行\n"},
        ]
    }
    results = apply.apply_mutations(doc, plan, bbox=None, layer="L")
    brief = [(r.get("ok"), r.get("from"), r.get("reason")) for r in results]
    _ok("nl-all-ok", all(r.get("ok") for r in results), str(brief))
    # 被写入的原文（documented normalization：只压空白，不改标点）
    _ok("nl-text-raw-single-line", t.dxf.text == "a b" and "\n" not in t.dxf.text, repr(t.dxf.text))
    _ok("nl-text-visible", dxftext.clean_entity_text(t) == "a b" and _ezdxf_plain(t) == "a b", repr(t.dxf.text))
    _ok("nl-mtext-uses-P", "\\P" in m.text, repr(m.text))
    # 「a" + "空格" + "换行 b」：评审允许写成 a\\Pb 或 a \\Pb，两种都算过。
    # 写入后回读按同一口径逐行去行尾空白：a\\Pb -> 两行；a \\Pb -> 行尾空格规整后也是两行。
    _ok("nl-mtext-space-kept", m.text.count("\\P") == 1, repr(m.text))
    _ok(
        "nl-mtext-visible",
        dxftext.edit_visible_matches(m.text, "a \nb") == "a\nb",
        f"{m.text!r} -> {dxftext.edit_visible_matches(m.text, 'a \nb')!r}",
    )
    _ok(
        "nl-mtext-blank-line-kept",
        m_blank.text.count("\\P") == 2 and dxftext.edit_visible_matches(m_blank.text, "line1\n\nline3") == "line1\n\nline3",
        f"{m_blank.text!r} -> {dxftext.check_visible(m_blank.text)!r}",
    )
    _ok(
        "nl-mtext-edge-blank-lines",
        dxftext.edit_visible_matches(m_edge.text, "\n第一行\n") == "第一行",
        f"{m_edge.text!r} -> {dxftext.check_visible(m_edge.text)!r}",
    )
    # 落盘再读回，可见字依旧一致
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "nl.dxf"
        doc.saveas(str(out))
        back = ezdxf.readfile(str(out))
        ents = {e.dxf.handle: e for e in back.modelspace()}
        _ok(
            "nl-saved-mtext",
            dxftext.edit_visible_matches(ents[m.dxf.handle].text, "a \nb") == "a\nb"
            and "\\P" in ents[m.dxf.handle].text,
            ents[m.dxf.handle].text,
        )

    # 同一套规整：TEXT/MTEXT 只压空白，标点保持
    # 行尾空白与首尾空行去掉；行首缩进保留（那是排版内容，不是噪声）
    _ok("edit-visible-text", dxftext.edit_visible("a \nb\t c") == "a\nb\t c", repr(dxftext.edit_visible("a \nb\t c")))
    _ok("edit-visible-flat", dxftext.edit_visible_flat("a \nb\t c") == "a b c", repr(dxftext.edit_visible_flat("a \nb\t c")))
    _ok(
        "edit-visible-mtext",
        # 行尾空白去掉，空行保留：段落数不比原来少
        dxftext.edit_visible("line1 \n\nline3") == "line1\n\nline3"
        and dxftext.edit_visible_flat("line1 \n\nline3") == "line1 line3",
        repr(dxftext.edit_visible("line1 \n\nline3")),
    )
    _ok("edit-visible-punct", dxftext.edit_visible("女卫（无障碍）；见说明：1、2") == "女卫（无障碍）；见说明：1、2")


def test_text_edit_multirun_flattened() -> None:
    """多段格式（不同颜色）的 MTEXT：保留段首段落码，正文拍成一段，可见字逐字相等。"""
    from app import dxftext

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    m = msp.add_mtext("占位", dxfattribs={"char_height": 100, "insert": (0, 0)})
    m.text = r"\A1;{\C1;红}\C2;蓝；绿"
    results = apply.apply_mutations(
        doc, {"text_edits": [{"from_text": "红蓝；绿", "xy": [0, 0], "text": "红、蓝；绿。"}]}, bbox=None, layer="L"
    )
    _ok("multirun-ok", results[0].get("ok"), str(results[0]))
    _ok("multirun-keeps-A1", m.text.startswith("\\A1;"), repr(m.text))
    _ok("multirun-single-run", "\\C" not in m.text and "{" not in m.text, repr(m.text))
    _ok(
        "multirun-visible",
        dxftext.edit_visible_matches(m.text, "红、蓝；绿。") == "红、蓝；绿。"
        and dxftext.check_visible(m.text) == "红、蓝；绿。",
        f"{m.text!r} -> {dxftext.check_visible(m.text)!r}",
    )
    _ok("multirun-ezdxf", _ezdxf_plain(m) == "红、蓝；绿。", repr(_ezdxf_plain(m)))
    docstring = dxftext.rewrite_mtext_body.__doc__ or ""
    _ok(
        "multirun-documented",
        "多段" in docstring and "\\A1" in docstring and "字体" in docstring,
        docstring[:120],
    )
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    _ok("multirun-readme", "改文字" in readme and "多段" in readme and "\\A1" in readme)


def test_text_edits_prompts_and_plan_detection() -> None:
    """prompts 的空数组清单含 text_edits；is_edit_plan 照旧认「带改动数组的 critique」。"""
    from app import decision, maas, placement, prompts

    count = prompts.SYSTEM_PROMPT.count("inserts/deletes/replaces/moves/pipes/texts/text_edits 必须是空数组")
    _ok("prompt-empty-list", count >= 2, str(count))
    schema = [line for line in prompts.SYSTEM_PROMPT.splitlines() if '"text_edits"' in line and "from_text" in line]
    _ok("prompt-schema-present", bool(schema), str(schema))
    _ok("prompt-schema-no-layer", bool(schema) and '{"from_text":"","xy":[0,0],"text":""}' in schema[0], schema and schema[0][-120:])

    rejects = decision.reject_plan()
    _ok("decision-empty-text-edits", rejects.get("text_edits") == [] and rejects.get("texts") == [], str(rejects.get("text_edits")))

    # 模型常把「点评 + 顺手改」标成 critique：只要带改动数组，就是改图方案，不能被清空。
    critique = {"mode": "critique", "reply": "可以顺带改掉。", "text_edits": [{"from_text": "女卫", "xy": [0, 0], "text": "女卫（无障碍）"}]}
    _ok("is-edit-plan-critique-with-edits", prompts.is_edit_plan(critique) is True, str(critique))
    _ok("plan-has-edits-text-edits", prompts.plan_has_edits({"text_edits": critique["text_edits"]}) is True)
    plan_mode = {"mode": "plan", "text_edits": critique["text_edits"]}
    _ok("is-edit-plan-plan-text-edits", prompts.is_edit_plan(plan_mode) is True)
    _ok("is-edit-plan-answer-empty", prompts.is_edit_plan({"mode": "answer", "text_edits": []}) is False)
    _ok("is-edit-plan-answer-no-key", prompts.is_edit_plan({"mode": "answer", "reply": "这里有两处地漏。"}) is False)
    _ok("is-edit-plan-critique-plain", prompts.is_edit_plan({"mode": "critique", "text_edits": []}) is False)
    _ok("is-edit-plan-critique-texts", prompts.is_edit_plan({"mode": "critique", "texts": [{"text": "x", "xy": [0, 0]}]}) is True)

    # 抽取到空数组的方案（模型自带 text_edits: []）也要能被认出来
    payload = maas.extract_assistant_payload('{"mode":"plan","text_edits":[]}')
    _ok("maas-extract-empty-text-edits", isinstance(payload, dict) and payload.get("text_edits") == [], str(payload))

    # placement：xy 是必填（error），与 _edit_text 一致
    doc = ezdxf.new("R2010")
    doc.modelspace().add_text("女卫", dxfattribs={"insert": (1000, 1000), "height": 300})
    issues = placement.check_plan(doc, {"text_edits": [{"from_text": "女卫", "text": "女卫（无障碍）"}]})
    xy_issues = [i for i in issues if i.get("code") == "missing_xy"]
    _ok(
        "placement-xy-required",
        bool(xy_issues) and all(i.get("severity") == "error" for i in xy_issues) and "改文字缺少坐标（xy）" in str(xy_issues[0].get("message")),
        str(issues),
    )
    ok_issues = placement.check_plan(
        doc, {"text_edits": [{"from_text": "女卫", "xy": [1000.0, 1000.0], "text": "女卫（无障碍）"}]}
    )
    _ok("placement-xy-present-no-error", not [i for i in ok_issues if i.get("severity") == "error"], str(ok_issues))


def test_rewrite_mtext_never_invents_font() -> None:
    """正文没有 \\f 时绝不能凭空加字体码：字体会被覆盖掉，实体自带的文字样式就丢了。"""
    from ezdxf.tools.text import fast_plain_mtext

    from app import dxftext

    B = chr(92)  # 反斜杠：字面量里写 \\P 会被编辑器/这里折来折去
    p_ = B + "P"
    cases = [
        ("abc", "新", "新"),
        ("卫生间", "卫生间（女）", "卫生间（女）"),
        ("A" + p_ + "B", "C\nD", "C" + p_ + "D"),
        ("{" + B + "C1;红}" + "{" + B + "C2;蓝}", "红蓝", "红蓝"),
    ]
    for raw, new_text, want_raw in cases:
        got = dxftext.rewrite_mtext_body(raw, new_text)
        ent = ezdxf.new("R2010").modelspace().add_mtext("x")
        ent.text = got
        visible_want = new_text
        # 多段颜色那种：要么就是纯正文，要么沿用原文第一段的颜色码；两者都不许加字体。
        allowed = {want_raw}
        if want_raw == "红蓝":
            allowed.add(B + "C1;红蓝")
            allowed.add("{" + B + "C1;红蓝}")
        _ok(f"no-font-raw:{new_text}", got in allowed, f"{raw!r} -> {got!r}")
        _ok(f"no-font-injected:{new_text}", B + "f" not in got and B + "F" not in got, got)
        _ok(f"no-font-check-visible:{new_text}", dxftext.check_visible(got) == visible_want, repr(dxftext.check_visible(got)))
        _ok(f"no-font-ezdxf:{new_text}", fast_plain_mtext(got) == visible_want, repr(fast_plain_mtext(got)))

    # 原文确实有字体码时才沿用（且只有原文里出现过的那个）
    kept = dxftext.rewrite_mtext_body("{" + B + "fSimHei|b0;旧}", "新")
    _ok("font-kept-when-present", kept == "{" + B + "fSimHei|b0;新}", repr(kept))
    # 段首段落码保留，但多层组拍平后也不再塞字体
    para = dxftext.rewrite_mtext_body(B + "A1;{" + B + "C1;红}" + "{" + B + "C2;蓝}", "红蓝")
    _ok("para-code-kept-no-font", para == B + "A1;红蓝", repr(para))


def test_text_edit_keeps_text_style() -> None:
    """条目自身的文字样式（dxf.style）要保留：没有内嵌字体的 MTEXT 改完也不能带 \\f。"""
    from app import dxftext

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    m = msp.add_mtext("abc", dxfattribs={"char_height": 175, "insert": (0, 0)})
    m.dxf.style = "Standard"
    plain = msp.add_mtext("卫生间", dxfattribs={"char_height": 175, "insert": (0, 500)})
    plan = {
        "text_edits": [
            {"from_text": "abc", "xy": [0, 0], "text": "新注"},
            {"from_text": "卫生间", "xy": [0, 500], "text": "卫生间（女）"},
        ]
    }
    results = apply.apply_mutations(doc, plan, bbox=None, layer="L")
    _ok("style-edit-ok", all(r.get("ok") for r in results), str(results))
    _ok("style-standard-kept", m.dxf.style == "Standard", m.dxf.style)
    _ok("style-no-font-code", "\\f" not in m.text, repr(m.text))
    _ok("style-plain-raw", plain.text == "卫生间（女）", repr(plain.text))
    _ok("style-visible", dxftext.check_visible(m.text) == "新注", repr(m.text))
    _ok("style-ezdxf", _ezdxf_plain(m) == "新注" and _ezdxf_plain(plain) == "卫生间（女）", repr(_ezdxf_plain(m)))


def test_text_edit_preview_equals_saved() -> None:
    from app import cad_render
    from app import config as app_config

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "src" / "sheet.dxf"
        src.parent.mkdir(parents=True)
        _label_drawing(src)
        proj = _proj(root, src)
        region = {"id": "subview:test:labels", "bbox": [0.0, 0.0, 20000.0, 15000.0]}
        plan = {
            "mode": "plan",
            "reply": "已更正地漏管径。",
            "text_edits": [{"from_text": "地漏 DN50", "xy": [8000, 3000], "text": "地漏 DN75（通气管）"}],
        }
        preview_doc, results, ok_n, _src, _enc, _bbox = apply._preview_doc(proj, plan, region)
        _ok("preview-doc-ok", ok_n == 1 and results[0].get("ok"), str(results))
        handle = results[0]["handle"]
        pv = _find(preview_doc, handle)
        from app import dxftext as _dxftext

        _ok(
            "preview-exact",
            pv is not None and pv.text.startswith("\\A1;") and _dxftext.check_visible(pv.text) == "地漏 DN75（通气管）",
            getattr(pv, "text", None),
        )

        out = apply.apply_plan(proj, plan, region=region, message_id="m1")
        _ok("confirm-ok", out["ok"] and out["ok_count"] == 1, str(out))
        saved = ezdxf.readfile(str(apply.work_path(proj)))
        _ok("confirm-preview-same-result", out["results"][0].get("handle") == handle)
        _ok("saved-exact", _dxftext.check_visible(saved.entitydb[handle].text) == "地漏 DN75（通气管）")
        _ok("saved-preview-same-raw", saved.entitydb[handle].text == pv.text)
        _ok("saved-no-duplicate", sum(1 for e in saved.modelspace() if e.dxftype() == "MTEXT") == 2)
        _ok("history-has-ok", (apply.load_history(proj).get("entries") or [])[-1]["ok"] == 1)
        summary = apply.excerpt_working(proj)
        _ok("history-summary-mentions-edit", "改文字" in summary, summary)
        # 撤回后恢复原文
        old_env = app_config.SCENE_DIR
        apply.undo_last(proj)
        restored = ezdxf.readfile(str(src))
        try:
            _ok(
                "undo-restores",
                any(e.dxftype() == "MTEXT" and "地漏 DN50" in e.text for e in restored.modelspace()),
            )
        finally:
            app_config.SCENE_DIR = old_env


# ---------------------------------------------------------------- B. 接线


def test_scene_patch_for_edit() -> None:
    """右侧增量补丁：旧 handle 要重画，新字按 handle 叠上，旧字露出来的地方遮纸。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    t = msp.add_text("女卫", dxfattribs={"height": 300, "insert": (1000, 1000)})
    results = apply.apply_mutations(
        doc,
        {"text_edits": [{"from_text": "女卫", "xy": [1000, 1000], "text": "女卫（无障碍）"}]},
        bbox=None,
        layer="L",
    )
    patch = scene.build_scene_patch(doc, results, [0.0, 0.0, 5000.0, 5000.0])
    texts = [i for i in patch["items"] if i.get("t") == "text"]
    _ok("patch-text-edited", len(texts) == 1 and texts[0]["s"] == "女卫（无障碍）", str(texts))
    _ok("patch-same-handle", texts and texts[0].get("id") == t.dxf.handle, str(texts))
    _ok("patch-hides-old", t.dxf.handle in patch["hide"], str(patch["hide"]))
    _ok("patch-transient-cover", any(i.get("t") == "cover" and i.get("transient") for i in patch["items"]))
    _ok("patch-actions", patch["actions"].get("text_edit") == 1, str(patch["actions"]))
    _ok("patch-dirty", patch.get("dirty_bbox") is not None)

    # 旧字很长（超过 12 个字）时，遮旧字的纸必须跟着变宽，否则旧字尾巴会露在新字旁边。
    long_old = "说明：1、给水；2、排水；3、通风"
    doc2 = ezdxf.new("R2010")
    t2 = doc2.modelspace().add_text(long_old, dxfattribs={"height": 300, "insert": (1000, 1000)})
    plan = {"text_edits": [{"from_text": long_old, "xy": [1000, 1000], "text": "见说明"}]}
    res2 = apply.apply_mutations(doc2, plan, bbox=None, layer="L")
    _ok("patch-long-old-edit", res2 and res2[0].get("ok") and res2[0].get("from") == long_old, str(res2))

    def _cover_width(patch):
        boxes = []
        for it in patch["items"]:
            if it.get("t") == "cover" and it.get("transient"):
                xs = [it["p"][i] for i in range(0, len(it["p"]), 2)]
                boxes.append(max(xs) - min(xs))
        return max(boxes) if boxes else None

    patch_long = scene.build_scene_patch(doc2, res2, [0.0, 0.0, 20000.0, 15000.0])
    doc3 = ezdxf.new("R2010")
    doc3.modelspace().add_text("abc", dxfattribs={"height": 300, "insert": (1000, 1000)})
    res3 = apply.apply_mutations(
        doc3, {"text_edits": [{"from_text": "abc", "xy": [1000, 1000], "text": "见说明"}]}, bbox=None, layer="L"
    )
    patch_short = scene.build_scene_patch(doc3, res3, [0.0, 0.0, 20000.0, 15000.0])
    w_long, w_short = _cover_width(patch_long), _cover_width(patch_short)
    _ok("patch-cover-width-long", w_long is not None and w_long >= 0.7 * 300 * len(long_old) * 0.8, str(w_long))
    _ok("patch-cover-grows-with-old-text", w_long is not None and w_short is not None and w_long > w_short * 2, f"{w_long} vs {w_short}")


def test_text_edits_wired_everywhere() -> None:
    edit_plan = {"mode": "plan", "text_edits": [{"from_text": "女卫", "xy": [0, 0], "text": "女卫（无障碍）"}]}
    _ok("prompts-plan_has_edits", prompts.plan_has_edits(edit_plan))
    _ok("prompts-schema", '"text_edits"' in prompts.SYSTEM_PROMPT)
    _ok("prompts-rule-from_text", "from_text" in prompts.SYSTEM_PROMPT)
    _ok("prompts-summary", "改文字1处" in prompts.summarize_plan(edit_plan), prompts.summarize_plan(edit_plan))
    _ok("prompts-repair-label", "改文字" in prompts.build_repair_message([{"op": "text_edit", "index": 0}], None))
    from app import decision, maas

    _ok("decision-empty-plan", decision.EMPTY_EDITS.get("text_edits") == [], str(decision.EMPTY_EDITS))
    payload = maas.extract_assistant_payload(json.dumps({"mode": "plan", "text_edits": [{"from_text": "a", "text": "b"}]}, ensure_ascii=False))
    _ok("maas-extract", isinstance(payload, dict) and payload.get("text_edits"), str(payload))
    from app import cad_render

    _ok("cad_render-plan-marks", cad_render._plan_has_marks(edit_plan))
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    _ok("web-plan-bits", "text_edits" in js)

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_text("女卫", dxfattribs={"insert": (1000, 1000), "height": 300})
    issues = placement.check_plan(doc, {"text_edits": [{"from_text": "", "xy": None, "text": ""}]})
    codes = {i.get("code") for i in issues}
    _ok("placement-empty-issues", {"missing_text", "missing_xy"} <= codes, str(issues))
    ok_issues = placement.check_plan(doc, {"text_edits": [{"from_text": "女卫", "xy": [1000.0, 1000.0], "text": "女卫（无障碍）"}]})
    _ok("placement-ok-no-error", not [i for i in ok_issues if i.get("severity") == "error"], str(ok_issues))


# ---------------------------------------------------------------- C. ATTRIB


def test_replace_keeps_attribs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        src = root / "attrib.dxf"
        doc = ezdxf.new("R2010")
        old = doc.blocks.new("OLD_EQ")
        old.add_lwpolyline([(0, 0), (300, 0), (300, 300), (0, 300)], close=True)
        old.add_attdef("设备编号", (0, -80), dxfattribs={"height": 50})
        old.add_attdef("NAME", (0, 350), dxfattribs={"height": 50})
        new = doc.blocks.new("NEW_EQ")
        new.add_circle((150, 150), 150)
        new.add_attdef("NAME", (0, 350), dxfattribs={"height": 50})
        new.add_attdef("型号", (0, -80), dxfattribs={"height": 50})
        msp = doc.modelspace()
        ine = msp.add_blockref("OLD_EQ", (5000, 5000))
        ine.add_auto_attribs({"设备编号": "SB-001", "NAME": "立式泵"})
        msp.add_blockref("OLD_EQ", (9000, 5000)).add_auto_attribs({"设备编号": "SB-002", "NAME": "立式泵"})
        doc.saveas(str(src))

        plan = {
            "mode": "plan",
            "replaces": [
                {"from_block": "OLD_EQ", "from_xy": [5000, 5000], "block": "NEW_EQ", "xy": [5000, 5000], "layer": "设备"}
            ],
        }
        doc2, results = _apply_in_memory(src, plan)
        ins = [r for r in results if r.get("action") == "insert"]
        brief = [{k: v for k, v in r.items() if k != "found"} for r in results]
        _ok("replace-inserted", len(ins) == 1 and ins[0].get("ok"), str(brief))
        _ok("replace-attrib-carried", ins[0].get("attribs") == {"NAME": "立式泵"}, str(ins[0]))
        ref = doc2.entitydb[ins[0]["handle"]]
        vals = {a.dxf.tag: a.dxf.text for a in ref.attribs}
        _ok("replace-attrib-value", vals.get("NAME") == "立式泵", str(vals))
        _ok("replace-attrib-no-foreign-tag", "设备编号" not in vals and vals == {"NAME": "立式泵"}, str(vals))
        deletes = [r for r in results if r.get("action") == "delete"]
        _ok("replace-old-deleted", len(deletes) == 1 and deletes[0].get("ok"), str([{k: v for k, v in d.items() if k != "found"} for d in deletes]))
        others = [e for e in doc2.modelspace() if e.dxftype() == "INSERT" and e.dxf.name == "OLD_EQ"]
        _ok("replace-other-instance-kept", len(others) == 1 and {a.dxf.text for a in others[0].attribs} == {"SB-002", "立式泵"})
        doc2.saveas(str(root / "out.dxf"))
        back = ezdxf.readfile(str(root / "out.dxf"))
        back_vals = {
            a.dxf.tag: a.dxf.text
            for e in back.modelspace()
            if e.dxftype() == "INSERT" and e.dxf.name == "NEW_EQ"
            for a in e.attribs
        }
        _ok("replace-attrib-saved", back_vals.get("NAME") == "立式泵", str(back_vals))


def main() -> int:
    test_percent_escape_exhaustive()
    test_cleaner_reproduced_bugs()
    test_cleaner_punctuation_exact()
    test_cleaner_percent_codes()
    test_collect_texts_exact()
    test_scene_text_payload_exact()
    test_text_edit_in_place()
    test_text_edit_preserves_rich_format()
    test_text_edit_matching_rules()
    test_text_edit_scope_current_partition()
    test_text_edit_width_normalized_needs_unique()
    test_minimal_percent_escaping()
    test_text_edit_newlines()
    test_text_edit_multirun_flattened()
    test_rewrite_mtext_never_invents_font()
    test_text_edit_keeps_text_style()
    test_text_edits_prompts_and_plan_detection()
    test_text_edit_preview_equals_saved()
    test_scene_patch_for_edit()
    test_text_edits_wired_everywhere()
    test_replace_keeps_attribs()
    print("ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(str(exc), flush=True)
        raise SystemExit(1)
