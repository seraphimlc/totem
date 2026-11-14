#!/usr/bin/env python3
"""SQLite is the live source of truth for drawing indexes and changelogs.

Saving must keep the full current-state index JSON verbatim. Leftover
index.json / changelog files may be imported once; they are not written again.
"""

from __future__ import annotations

import json
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


def _rich_index() -> dict:
    return {
        "schema": "cad-nl-drawing-index/v1.1",
        "project": "学生食堂",
        "source": {
            "dxf": "source.dxf",
            "indexed_at": "2026-11-14T13:00:00+08:00",
            "note": "自动索引。房间名和官方块名取自图纸原文。",
        },
        "sheets": [
            {
                "id": "1F-装饰平面",
                "title": "一层装饰平面",
                "floor": "1F",
                "kind": "装饰平面",
                "frame": {"bbox": [0.0, 0.0, 20000.0, 15000.0]},
            }
        ],
        "layers": [
            {"name": "EL-DIM", "color": 7, "on": True, "frozen": False},
            {"name": "FF-FIXT", "color": 3, "on": True, "frozen": False},
        ],
        "blocks": [
            {"name": "做成ACAD TuKuai0155", "display_name": "蹲便器", "insert_count": 2},
            {"name": "地漏", "display_name": "地漏", "insert_count": 1},
        ],
        "rooms": [
            {
                "name": "男卫",
                "sheet": "1F-装饰平面",
                "world_xy": [1200.5, 3400.25],
                "equipment_nearby": [
                    {
                        "block": "做成ACAD TuKuai0155",
                        "display_name": "蹲便器",
                        "xy": [1500.0, 3600.0],
                        "nested": True,
                        "host_block": "A$C_HOST",
                    }
                ],
            }
        ],
        "equipment": [
            {
                "block": "做成ACAD TuKuai0155",
                "display_name": "蹲便器",
                "xy": [1500.0, 3600.0],
                "sheet": "1F-装饰平面",
                "room": "男卫",
                "nested": True,
                "host_block": "A$C_HOST",
            },
            {
                "block": "地漏",
                "display_name": "地漏",
                "xy": [1800.0, 3500.0],
                "sheet": "1F-装饰平面",
                "nested": False,
            },
        ],
        "equipment_unassigned": [],
        "custom_extra": {"keep": True, "counts": [1, 2, 3]},
        "changelog": "changelog.json",
    }


def _isolate(tmp: str):
    from app import config, db

    prev = config.CAD_DATA_DIR
    db.close()
    config.CAD_DATA_DIR = tmp
    db.close()
    return prev


def _restore(prev: str) -> None:
    from app import config, db

    db.close()
    config.CAD_DATA_DIR = prev


def test_save_and_load_verbatim() -> None:
    from app import db, projects

    with tempfile.TemporaryDirectory() as tmp:
        prev = _isolate(tmp)
        try:
            root = Path(tmp) / "sess"
            root.mkdir()
            index = _rich_index()
            db.save_index("p-full", index, "# 索引\\n")
            for name in ("index.json", "INDEX.md", "CHANGELOG.md", "changelog.json"):
                _ok(f"save-does-not-write-{name}", not (root / name).is_file())
            loaded = db.load_index("p-full")
            _ok("sqlite-roundtrip-verbatim", loaded == index)
            _ok("keeps-official-block-name", loaded["blocks"][0]["name"] == "做成ACAD TuKuai0155")
            _ok("keeps-nested-equipment", loaded["equipment"][0]["nested"] is True and loaded["equipment"][0]["host_block"] == "A$C_HOST")
            _ok("keeps-coordinates", loaded["rooms"][0]["world_xy"] == [1200.5, 3400.25])
            _ok("keeps-extra-field", loaded["custom_extra"] == {"keep": True, "counts": [1, 2, 3]})
            _ok("keeps-schema", loaded["schema"] == "cad-nl-drawing-index/v1.1")

            proj = {"id": "p-full", "root": str(root), "index_json": str(root / "index.json")}
            via_projects = projects.load_index(proj)
            _ok("load-index-sqlite-only", via_projects == index)
        finally:
            _restore(prev)


def test_import_leftover_index_once() -> None:
    from app import db, projects

    with tempfile.TemporaryDirectory() as tmp:
        prev = _isolate(tmp)
        try:
            root = Path(tmp) / "sess"
            root.mkdir()
            index = _rich_index()
            (root / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (root / "INDEX.md").write_text("# leftover\n", encoding="utf-8")
            proj = {"id": "p-import", "root": str(root), "index_json": str(root / "index.json")}
            loaded = projects.load_index(proj)
            _ok("import-full-index", loaded == index)
            _ok("import-stored-in-sqlite", db.load_index("p-import") == index)

            # Changing the leftover file must not overwrite the already-imported row.
            thinner = {"schema": "cad-nl-drawing-index/v1.1", "sheets": [], "rooms": [], "note": "should not replace"}
            (root / "index.json").write_text(json.dumps(thinner, ensure_ascii=False), encoding="utf-8")
            again = projects.load_index(proj)
            _ok("import-only-once", again == index, str(again.get("note")))
        finally:
            _restore(prev)


def test_changelog_appends_and_file_import() -> None:
    from app import db, projects

    with tempfile.TemporaryDirectory() as tmp:
        prev = _isolate(tmp)
        try:
            root = Path(tmp) / "sess"
            root.mkdir()
            db.append_changelog("p-append", "上传图纸并建立首次索引。", {"kind": "init"})
            for name in ("CHANGELOG.md", "changelog.json"):
                _ok(f"append-does-not-write-{name}", not (root / name).is_file())
            rows = db.list_changelogs("p-append")
            _ok("append-to-sqlite", len(rows) == 1 and rows[0]["summary"] == "上传图纸并建立首次索引。")
            _ok("append-keeps-payload", rows[0]["payload"] == {"kind": "init"})

            # Structured leftover changelog.json is imported in full when no rows exist.
            entries = {
                "schema": "cad-nl-changelog/v1",
                "drawing": "学生食堂",
                "entries": [
                    {
                        "id": "20261114-index-v1.1-rebuild",
                        "date": "2026-11-14",
                        "title": "补全索引（嵌套符号与可读名称）",
                        "updated_at": "2026-11-14T01:22:52+08:00",
                        "note": "只更新索引，未改图纸。",
                        "edits": [{"block": "做成ACAD TuKuai0155"}],
                    },
                    {
                        "ts": "2026-11-14T02:00:00+08:00",
                        "summary": "上传图纸并建立首次索引。",
                    },
                ],
            }
            other = Path(tmp) / "legacy"
            other.mkdir()
            (other / "changelog.json").write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
            n = db.import_changelog_file("p-legacy", other)
            _ok("import-changelog-json-count", n == 2, str(n))
            imported = db.list_changelogs("p-legacy")
            _ok("import-preserves-ts", imported[0]["ts"] == "2026-11-14T01:22:52+08:00")
            _ok("import-preserves-title", imported[0]["summary"] == "补全索引（嵌套符号与可读名称）")
            _ok("import-payload-verbatim", imported[0]["payload"] == entries["entries"][0])
            _ok("import-second-entry", imported[1]["summary"] == "上传图纸并建立首次索引。" and imported[1]["ts"] == "2026-11-14T02:00:00+08:00")
            _ok("import-changelog-once", db.import_changelog_file("p-legacy", other) == 0)

            # Markdown fallback when changelog.json is absent.
            md_dir = Path(tmp) / "mdonly"
            md_dir.mkdir()
            (md_dir / "CHANGELOG.md").write_text(
                "# 变更日志\n\n"
                "## 2026-11-14 套用已校对索引\n\n"
                "- 时间：2026-11-14T11:00:00+08:00（Asia/Shanghai）\n"
                "- 上传图纸，并套用同一份文件已校对的索引。\n",
                encoding="utf-8",
            )
            n = db.import_changelog_file("p-md", md_dir)
            _ok("import-changelog-md-count", n == 1, str(n))
            md_rows = db.list_changelogs("p-md")
            _ok("import-md-summary", md_rows[0]["summary"] == "套用已校对索引")
            _ok("import-md-ts", md_rows[0]["ts"] == "2026-11-14T11:00:00+08:00")

            # load_index also imports leftover changelogs for a project with an index file.
            idx = {"schema": "cad-nl-drawing-index/v1.1", "sheets": [{"id": "A"}], "rooms": [], "layers": []}
            (other / "index.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
            # p-legacy already has changelogs; a fresh id with both files:
            fresh = Path(tmp) / "both"
            fresh.mkdir()
            (fresh / "index.json").write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
            (fresh / "changelog.json").write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            got = projects.load_index({"id": "p-both", "root": str(fresh), "index_json": str(fresh / "index.json")})
            _ok("load-imports-index", got == idx)
            _ok("load-imports-changelog", len(db.list_changelogs("p-both")) == 2)
        finally:
            _restore(prev)


def main() -> int:
    test_save_and_load_verbatim()
    test_import_leftover_index_once()
    test_changelog_appends_and_file_import()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
