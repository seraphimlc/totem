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

    # The component library index lives next to the drawings on the server; a
    # machine without it can still run everything else.
    index = library.index_path()
    if not index:
        print("SKIP  component library index not found", flush=True)
        print("SKIP  set CAD_LIBRARY_INDEX to libraries/default/index.json to run this test", flush=True)
        return

    hits = library.search("坐便") or library.search("座便器") or library.search("马桶")
    names = [b.get("name") for b in hits]
    _ok("library-has-seated", any(n in names or "马桶" in (n or "") or "座便" in str(b.get("display_name")) or "马桶" in str(b.get("display_name")) for b, n in ((b, b.get("name")) for b in hits)) or bool(hits), str(names[:8]))
    # Prefer a real library block name, never an invented Chinese name as block.
    if hits:
        _ok("library-real-block-name", all(b.get("name") for b in hits))


if __name__ == "__main__":
    test_flatten_and_excerpt()
    test_library_search_seated()
    print("ALL PASS")
