#!/usr/bin/env python3
"""Index must flatten nested fixtures, label them, and put them in the chat excerpt."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CAD_DATA_DIR", "")


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""))


def test_flatten_and_excerpt() -> None:
    import ezdxf
    from app import indexing, projects

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    fixtures = doc.blocks.new("做成ACAD TuKuai0155")
    fixtures.add_lwpolyline([(0, -197), (466, -197), (466, 197), (0, 197)], close=True)
    fixtures.add_ellipse(center=(233, 0), major_axis=(80, 0), ratio=0.6)

    host = doc.blocks.new("A$C_HOST")
    host.add_text("男卫", dxfattribs={"insert": (0, 0), "height": 200, "layer": "EL-DIM"})
    host.add_text("女卫", dxfattribs={"insert": (0, -4000), "height": 200, "layer": "EL-DIM"})
    # 4 squats in two groups (2+2)
    for i, y in enumerate((800, 1800, -3200, -2200)):
        host.add_blockref("做成ACAD TuKuai0155", insert=(2000, y), dxfattribs={"layer": "FF-FIXT", "rotation": 180})

    # two views share the same underlay
    msp.add_blockref("A$C_HOST", insert=(100000, 200000), dxfattribs={"layer": "TWT_DIM"})
    msp.add_blockref("A$C_HOST", insert=(130000, 200000), dxfattribs={"layer": "TWT_DIM"})
    msp.add_lwpolyline([(90000, 190000), (160000, 190000), (160000, 210000), (90000, 210000)], close=True, dxfattribs={"layer": "000PL"})

    # a modelspace floor drain
    drain = doc.blocks.new("地漏")
    drain.add_circle((0, 0), 130)
    msp.add_blockref("地漏", insert=(102000, 200100), dxfattribs={"layer": "0标注"})

    with tempfile.TemporaryDirectory() as tmp:
        from app import config, db, init_proj

        prev_data = config.CAD_DATA_DIR
        db.close()
        config.CAD_DATA_DIR = tmp
        db.close()
        try:
            path = Path(tmp) / "t.dxf"
            doc.saveas(str(path), encoding="utf-8")
            root = Path(tmp) / "proj"
            index = init_proj.build_first_index(path, root, "测试图", project_id="test-index")
            _ok("no-index-json-written", not (root / "index.json").is_file())
            _ok("no-index-md-written", not (root / "INDEX.md").is_file())
            _ok("no-changelog-md-written", not (root / "CHANGELOG.md").is_file())
            _ok("no-changelog-json-written", not (root / "changelog.json").is_file())
            stored = db.load_index("test-index")
            _ok("sqlite-has-full-index", stored == index)
            logs = db.list_changelogs("test-index")
            _ok("changelog-in-sqlite", len(logs) == 1 and "首次索引" in (logs[0].get("summary") or ""), str(logs))
        finally:
            db.close()
            config.CAD_DATA_DIR = prev_data

    _ok("schema", index.get("schema") == indexing.SCHEMA)
    squats = [e for e in index["equipment"] if e.get("display_name") == "蹲便器"]
    _ok("eight-nested-squats", len(squats) == 8, str(len(squats)))
    _ok("all-nested", all(e.get("nested") and e.get("host_block") == "A$C_HOST" for e in squats))
    _ok("shared-overlay", all(e.get("edit_hint") == "overlay" for e in squats))
    rooms = {r["name"] for r in index["rooms"]}
    _ok("rooms-from-block-text", {"男卫", "女卫"} <= rooms, str(rooms))
    # 4 per view should attach to 男卫/女卫
    by_room = {}
    for e in squats:
        by_room.setdefault(e.get("room"), 0)
        by_room[e.get("room")] += 1
    _ok("assigned-to-rooms", by_room.get("男卫", 0) == 4 and by_room.get("女卫", 0) == 4, str(by_room))

    drains = [e for e in index["equipment"] if e.get("display_name") == "地漏" or e.get("block") == "地漏"]
    _ok("floor-drain-indexed", len(drains) >= 1, str(len(drains)))

    proj = {"name": "测试图", "encoding": "utf-8", "dxf_version": "AC1021"}
    # excerpt_index calls load_index(proj) — stub it
    original = projects.load_index
    projects.load_index = lambda _p: index  # type: ignore
    try:
        excerpt = projects.excerpt_index(
            proj,
            {"kind": "sheet", "title": "测试图", "sheet_id": index["sheets"][0]["id"], "bbox": [90000, 190000, 160000, 210000]},
            "把这里的蹲便都改成坐便",
        )
    finally:
        projects.load_index = original
    _ok("excerpt-has-display", "蹲便器" in excerpt, excerpt[:400])
    _ok("excerpt-has-official-name", "做成ACAD TuKuai0155" in excerpt)
    count_xy = excerpt.count("坐标=")
    _ok("excerpt-lists-instances", count_xy >= 8, f"coord lines={count_xy}\n{excerpt}")
    _ok("excerpt-tells-overlay", "勿改底图块定义" in excerpt or "覆盖" in excerpt)


def test_library_search_seated() -> None:
    from app import library

    hits = library.search("坐便") or library.search("座便器") or library.search("马桶")
    names = [b.get("name") for b in hits]
    _ok("library-has-seated", any(n in names or "马桶" in (n or "") or "座便" in str(b.get("display_name")) or "马桶" in str(b.get("display_name")) for b, n in ((b, b.get("name")) for b in hits)) or bool(hits), str(names[:8]))
    # Prefer a real library block name, never an invented Chinese name as block.
    if hits:
        _ok("library-real-block-name", all(b.get("name") for b in hits))


def test_drawing_texts_in_index_and_excerpt() -> None:
    """TEXT/MTEXT/ATTRIB and anonymous-block labels must be stored and excerpted."""
    import ezdxf
    from app import indexing, projects

    _ok(
        "clean-color-mtext",
        indexing._clean_dxf_text(r"{\C50; 餐厅}") == "餐厅",
        indexing._clean_dxf_text(r"{\C50; 餐厅}"),
    )
    _ok(
        "clean-font-mtext",
        "双孔回收台带车" in indexing._clean_dxf_text(
            r"{\fSimHei|b0|i0|c134|p49;双孔回收台带车\P1800*700*800}"
        ),
        indexing._clean_dxf_text(r"{\fSimHei|b0|i0|c134|p49;双孔回收台带车\P1800*700*800}"),
    )
    _ok(
        "query-score-occupancy",
        indexing._text_query_score("一层使用人数：390人", "一层使用人数是多少", ["一层使用人数是多少"]) >= 6,
    )

    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (20000, 0), (20000, 15000), (0, 15000)],
        close=True,
        dxfattribs={"layer": "000PL"},
    )
    msp.add_text("备餐间", dxfattribs={"insert": (4000, 6000), "height": 300, "layer": "房间功能"})
    msp.add_text(
        "三孔回收台带车",
        dxfattribs={"insert": (4300, 5200), "height": 180, "layer": "0"},
    )
    msp.add_mtext(
        "一层使用人数：390人",
        dxfattribs={"insert": (2000, 12000), "char_height": 200, "layer": "0标注"},
    )
    # Formatted MTEXT like the cafeteria drawing: color / font groups must not eat wording.
    msp.add_mtext(
        r"{\C50; 餐厅}",
        dxfattribs={"insert": (3500, 6100), "char_height": 300, "layer": "WINDOW"},
    )
    msp.add_mtext(
        r"{\fSimHei|b0|i0|c134|p49;双孔回收台带车\P1800*700*800}",
        dxfattribs={"insert": (5000, 3000), "char_height": 120, "layer": "RAWEN设备名称"},
    )
    # ATTRIB on a named block insert
    blk = doc.blocks.new("EQ_TAG")
    blk.add_attdef(tag="NAME", text="设备", dxfattribs={"insert": (0, 200), "height": 100})
    ins = msp.add_blockref("EQ_TAG", insert=(8000, 6000), dxfattribs={"layer": "0"})
    ins.add_attrib("NAME", "压面机", dxfattribs={"insert": (8000, 6200), "height": 100})
    # Text inside an anonymous * block (equipment collection skips these)
    try:
        anon = doc.blocks.new("*U99")
        anon.add_text("防烟分区", dxfattribs={"insert": (0, 0), "height": 150, "layer": "0"})
        msp.add_blockref("*U99", insert=(9000, 4000), dxfattribs={"layer": "0"})
        have_anon = True
    except Exception as exc:
        have_anon = False
        print(f"note: could not create *U99 anonymous block ({exc})")

    with tempfile.TemporaryDirectory() as tmp:
        from app import config, db, init_proj

        prev_data = config.CAD_DATA_DIR
        db.close()
        config.CAD_DATA_DIR = tmp
        db.close()
        try:
            path = Path(tmp) / "ann.dxf"
            doc.saveas(str(path), encoding="utf-8")
            root = Path(tmp) / "proj"
            index = init_proj.build_first_index(path, root, "标注测试图", project_id="test-texts")
            stored = db.load_index("test-texts")
            _ok("sqlite-stores-texts", stored == index)
        finally:
            db.close()
            config.CAD_DATA_DIR = prev_data

    texts = index.get("texts") or []
    words = {str(t.get("text") or "") for t in texts}
    _ok("index-has-texts-field", isinstance(texts, list) and len(texts) >= 3, str(len(texts)))
    _ok("has-room-text", "备餐间" in words, str(sorted(words)))
    _ok("has-recycle-counter", "三孔回收台带车" in words, str(sorted(words)))
    _ok("has-occupancy-note", any("一层使用人数" in w and "390" in w for w in words), str(sorted(words)))
    _ok("has-formatted-dining-room", any(w.strip() == "餐厅" for w in words), str(sorted(words)))
    _ok("has-formatted-recycle-mtext", any("回收台带车" in w for w in words), str(sorted(words)))
    _ok("formatted-mtext-not-font-preamble", not any("SimHei" in w or w.startswith("fSim") for w in words), str(sorted(words)))
    _ok("has-attrib-press", any("压面机" in w for w in words), str(sorted(words)))
    if have_anon:
        _ok("has-anon-block-text", "防烟分区" in words, str(sorted(words)))

    rooms = {r["name"]: r for r in index.get("rooms") or []}
    _ok("room-beican", "备餐间" in rooms, str(list(rooms)))
    labels = rooms.get("备餐间", {}).get("labels_nearby") or []
    label_words = [it.get("text") if isinstance(it, dict) else str(it) for it in labels]
    _ok("room-labels-include-recycle", any("回收台" in w for w in label_words), str(label_words))

    # Enriching again must not drop rooms or already-attached labels.
    before_rooms = len(index.get("rooms") or [])
    before_eq = len(index.get("equipment") or [])
    indexing.apply_texts_to_index(index, list(texts))
    _ok("reapply-keeps-rooms", len(index.get("rooms") or []) == before_rooms)
    _ok("reapply-keeps-equipment", len(index.get("equipment") or []) == before_eq)
    _ok("reapply-keeps-recycle-label", any("回收台" in w for w in [
        it.get("text") if isinstance(it, dict) else str(it)
        for it in (rooms.get("备餐间") or index["rooms"][0]).get("labels_nearby") or []
    ]))

    proj = {"name": "标注测试图", "encoding": "utf-8", "dxf_version": "AC1021"}
    original = projects.load_index
    projects.load_index = lambda _p: index  # type: ignore
    try:
        region = {
            "kind": "sheet",
            "title": index["sheets"][0]["title"] if index.get("sheets") else "标注测试图",
            "sheet_id": index["sheets"][0]["id"] if index.get("sheets") else "",
            "bbox": [0, 0, 20000, 15000],
            "floor": "1F",
        }
        ex_recycle = projects.excerpt_index(proj, region, "回收台在哪")
        ex_people = projects.excerpt_index(proj, region, "一层使用人数是多少")
        ex_general = projects.excerpt_index(proj, region, "这张图的房间怎么布置")
    finally:
        projects.load_index = original

    _ok("excerpt-recycle", "三孔回收台带车" in ex_recycle or "双孔回收台带车" in ex_recycle, ex_recycle)
    _ok("excerpt-formatted-dining", "餐厅" in ex_recycle or "餐厅" in ex_general, ex_recycle)
    _ok("excerpt-occupancy", "一层使用人数：390人" in ex_people or "390人" in ex_people, ex_people)
    _ok("excerpt-sheet-sample-cjk", "备餐间" in ex_general or "三孔回收台带车" in ex_general, ex_general)
    _ok("excerpt-not-truncated-badly", "本图幅相关文字" in ex_recycle and len(ex_recycle) <= 12000, str(len(ex_recycle)))


if __name__ == "__main__":
    test_flatten_and_excerpt()
    test_library_search_seated()
    test_drawing_texts_in_index_and_excerpt()
    print("ALL PASS")
