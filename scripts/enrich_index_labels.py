#!/usr/bin/env python3
"""Patch typologies and dining-furniture attachment into a stored drawing index.

Official block names are never changed. Safe to re-run (idempotent).

This does not talk to production. Point it at a local JSON or a local SQLite
copy when publishing; do not run it against the live server from this script.

Examples:
  .venv/bin/python scripts/enrich_index_labels.py --json known-indexes/xueshengshitang/index.json --dry-run
  .venv/bin/python scripts/enrich_index_labels.py --sqlite /path/to/studio.db --project-id <id>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import indexing  # noqa: E402


def _summarize(index: dict) -> str:
    eq = index.get("equipment") or []
    tables = [e for e in eq if e.get("display_name") == "餐桌" or e.get("block") == "A$C783832BF"]
    labeled = sum(1 for e in eq if e.get("display_name"))
    rooms = index.get("rooms") or []
    dining = [r for r in rooms if indexing.is_dining_room_name(r.get("name"))]
    nearby = sum(len(r.get("equipment_nearby") or []) for r in dining)
    unassigned = len(index.get("equipment_unassigned") or [])
    return (
        f"equipment={len(eq)} labeled={labeled} 餐桌/A$C783832BF={len(tables)} "
        f"dining_rooms={len(dining)} dining_nearby={nearby} unassigned={unassigned}"
    )


def _patch_json(path: Path, *, dry_run: bool) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    before = _summarize(data)
    indexing.relabel_index(data)
    after = _summarize(data)
    print(f"json {path}")
    print(f"  before: {before}")
    print(f"  after:  {after}")
    if dry_run:
        print("  dry-run, not written")
        return 0
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("  written")
    return 0


def _patch_sqlite(db_path: Path, project_id: str, *, dry_run: bool) -> int:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT index_json FROM drawing_indexes WHERE project_id = ?", (project_id,)
    ).fetchone()
    if not row:
        print(f"sqlite {db_path}: no row for project_id={project_id}", file=sys.stderr)
        return 1
    data = json.loads(row["index_json"])
    before = _summarize(data)
    indexing.relabel_index(data)
    after = _summarize(data)
    print(f"sqlite {db_path} project_id={project_id}")
    print(f"  before: {before}")
    print(f"  after:  {after}")
    if dry_run:
        print("  dry-run, not written")
        conn.close()
        return 0
    payload = json.dumps(data, ensure_ascii=False)
    conn.execute(
        "UPDATE drawing_indexes SET index_json = ?, schema = ? WHERE project_id = ?",
        (payload, data.get("schema") or indexing.SCHEMA, project_id),
    )
    conn.commit()
    conn.close()
    print("  written")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, help="index.json to patch in place")
    ap.add_argument("--sqlite", type=Path, help="SQLite database file with drawing_indexes")
    ap.add_argument("--project-id", help="project_id row to patch in --sqlite")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.json:
        return _patch_json(args.json, dry_run=args.dry_run)
    if args.sqlite:
        if not args.project_id:
            print("--project-id is required with --sqlite", file=sys.stderr)
            return 2
        return _patch_sqlite(args.sqlite, args.project_id, dry_run=args.dry_run)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
