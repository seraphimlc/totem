#!/usr/bin/env python3
"""Patch drawing texts / annotations into a stored index from its DXF.

Does not invent official block names, does not drop existing rooms, equipment,
or curated labels_nearby. Safe to re-run (idempotent).

This script does not talk to production. Point it at a local JSON or a local
SQLite copy when publishing; do not run it against the live server from here.

Examples:
  .venv/bin/python scripts/enrich_index_texts.py \\
      --json known-indexes/xueshengshitang/index.json \\
      --dxf /path/to/source-fixed.dxf --encoding utf-8 --dry-run
  .venv/bin/python scripts/enrich_index_texts.py \\
      --sqlite /path/to/tutan.sqlite --project-id xueshengshitang \\
      --dxf /path/to/source-fixed.dxf --encoding utf-8 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402

from app import indexing, known  # noqa: E402


def _cjk_count(texts: list[dict]) -> int:
    import re

    n = 0
    for rec in texts:
        if re.search(r"[\u4e00-\u9fff]", str(rec.get("text") or "")):
            n += 1
    return n


def _summarize(index: dict) -> str:
    texts = index.get("texts") or []
    rooms = index.get("rooms") or []
    labeled = sum(1 for r in rooms if r.get("labels_nearby"))
    labels = sum(len(r.get("labels_nearby") or []) for r in rooms)
    eq = index.get("equipment") or []
    return (
        f"texts={len(texts)} cjk={_cjk_count(texts)} "
        f"rooms={len(rooms)} rooms_with_labels={labeled} labels={labels} "
        f"equipment={len(eq)}"
    )


def _collect(dxf: Path, encoding: str) -> list[dict]:
    try:
        doc = ezdxf.readfile(str(dxf), encoding=encoding)
    except Exception:
        doc = ezdxf.readfile(str(dxf))
    return indexing.collect_texts(doc, doc.modelspace())


def _apply(index: dict, texts: list[dict]) -> dict:
    # Keep typologies / other fields intact; only add texts and fill labels.
    return indexing.apply_texts_to_index(index, texts)


def _patch_json(path: Path, dxf: Path, encoding: str, *, dry_run: bool) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    before = _summarize(data)
    texts = _collect(dxf, encoding)
    _apply(data, texts)
    after = _summarize(data)
    print(f"json {path}")
    print(f"  dxf {dxf} encoding={encoding}")
    print(f"  before: {before}")
    print(f"  after:  {after}")
    if dry_run:
        print("  dry-run, not written")
        return 0
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("  written")
    return 0


def _patch_sqlite(db_path: Path, project_id: str, dxf: Path, encoding: str, *, dry_run: bool) -> int:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT index_json FROM drawing_indexes WHERE project_id = ?", (project_id,)
    ).fetchone()
    if not row:
        print(f"sqlite {db_path}: no row for project_id={project_id}", file=sys.stderr)
        conn.close()
        return 1
    data = json.loads(row["index_json"])
    before = _summarize(data)
    texts = _collect(dxf, encoding)
    _apply(data, texts)
    after = _summarize(data)
    print(f"sqlite {db_path} project_id={project_id}")
    print(f"  dxf {dxf} encoding={encoding}")
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
    ap.add_argument("--dxf", type=Path, required=True, help="source DXF used to collect texts")
    ap.add_argument("--encoding", help="ezdxf encoding (default: detect from DXF body)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.dxf.is_file():
        print(f"DXF not found: {args.dxf}", file=sys.stderr)
        return 2
    encoding = args.encoding or known.detect_dxf_encoding(args.dxf)
    if args.json:
        return _patch_json(args.json, args.dxf, encoding, dry_run=args.dry_run)
    if args.sqlite:
        if not args.project_id:
            print("--project-id is required with --sqlite", file=sys.stderr)
            return 2
        return _patch_sqlite(args.sqlite, args.project_id, args.dxf, encoding, dry_run=args.dry_run)
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
