"""SQLite store for sessions, drawing indexes, and changelogs."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import config

SHANGHAI = timezone(timedelta(hours=8))
_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'session',
    source_filename TEXT,
    data_subdir TEXT,
    deliverable TEXT,
    encoding TEXT DEFAULT 'utf-8',
    status TEXT DEFAULT 'pending',
    oss_source_key TEXT,
    oss_dxf_key TEXT,
    extra_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drawing_indexes (
    project_id TEXT PRIMARY KEY,
    schema TEXT,
    index_json TEXT NOT NULL,
    index_md TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changelogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_changelogs_project ON changelogs(project_id, ts);
"""


def _now() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def connect() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is not None:
            return _CONN
        path = config.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        _CONN = conn
        return conn


def close() -> None:
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None


def upsert_session(spec: dict[str, Any]) -> None:
    conn = connect()
    now = _now()
    extra = {k: v for k, v in spec.items() if k not in {
        "id", "name", "kind", "source_filename", "data_subdir", "deliverable",
        "encoding", "status", "oss_source_key", "oss_dxf_key", "created_at", "updated_at",
        "root", "dxf_path", "available", "index_json", "index_md", "chat_path",
        "default_root", "root_env", "default_sheet", "default_subview", "note",
    }}
    # keep useful extras
    keep = {}
    for key in ("default_sheet", "default_subview", "note", "dxf_version", "oss_source_url", "oss_dxf_url"):
        if spec.get(key) is not None:
            keep[key] = spec[key]
    keep.update({k: v for k, v in extra.items() if not str(k).startswith("_")})
    with _LOCK:
        conn.execute(
            """
            INSERT INTO sessions (
                id, name, kind, source_filename, data_subdir, deliverable, encoding,
                status, oss_source_key, oss_dxf_key, extra_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                kind=excluded.kind,
                source_filename=COALESCE(excluded.source_filename, sessions.source_filename),
                data_subdir=COALESCE(excluded.data_subdir, sessions.data_subdir),
                deliverable=COALESCE(excluded.deliverable, sessions.deliverable),
                encoding=COALESCE(excluded.encoding, sessions.encoding),
                status=COALESCE(excluded.status, sessions.status),
                oss_source_key=COALESCE(excluded.oss_source_key, sessions.oss_source_key),
                oss_dxf_key=COALESCE(excluded.oss_dxf_key, sessions.oss_dxf_key),
                extra_json=excluded.extra_json,
                updated_at=excluded.updated_at
            """,
            (
                spec["id"],
                spec.get("name") or spec["id"],
                spec.get("kind") or "session",
                spec.get("source_filename"),
                spec.get("data_subdir"),
                spec.get("deliverable"),
                spec.get("encoding") or "utf-8",
                spec.get("status") or "pending",
                spec.get("oss_source_key"),
                spec.get("oss_dxf_key"),
                json.dumps(keep, ensure_ascii=False),
                spec.get("created_at") or now,
                now,
            ),
        )
        conn.commit()


def update_session(project_id: str, fields: dict[str, Any]) -> None:
    row = get_session(project_id) or {"id": project_id}
    row.update(fields)
    upsert_session(row)


def get_session(project_id: str) -> Optional[dict[str, Any]]:
    conn = connect()
    with _LOCK:
        cur = conn.execute("SELECT * FROM sessions WHERE id = ?", (project_id,))
        row = cur.fetchone()
    return _row_to_session(row) if row else None


def list_sessions() -> list[dict[str, Any]]:
    conn = connect()
    with _LOCK:
        cur = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC, created_at DESC")
        rows = cur.fetchall()
    return [_row_to_session(r) for r in rows]


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    extra = {}
    raw = data.pop("extra_json", None)
    if raw:
        try:
            extra = json.loads(raw)
        except Exception:
            extra = {}
    data.update(extra)
    return data


def save_index(project_id: str, index: dict[str, Any], index_md: Optional[str] = None) -> None:
    """Store the full current-state index JSON verbatim. Do not slim or drop fields."""
    if not isinstance(index, dict):
        raise TypeError("index must be a dict")
    conn = connect()
    with _LOCK:
        conn.execute(
            """
            INSERT INTO drawing_indexes (project_id, schema, index_json, index_md, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                schema=excluded.schema,
                index_json=excluded.index_json,
                index_md=COALESCE(excluded.index_md, drawing_indexes.index_md),
                updated_at=excluded.updated_at
            """,
            (
                project_id,
                index.get("schema") or "cad-nl-drawing-index/v1",
                json.dumps(index, ensure_ascii=False),
                index_md,
                _now(),
            ),
        )
        conn.commit()


def load_index(project_id: str) -> Optional[dict[str, Any]]:
    conn = connect()
    with _LOCK:
        cur = conn.execute("SELECT index_json FROM drawing_indexes WHERE project_id = ?", (project_id,))
        row = cur.fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["index_json"])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def append_changelog(project_id: str, summary: str, payload: Optional[dict[str, Any]] = None) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "INSERT INTO changelogs (project_id, ts, summary, payload_json) VALUES (?, ?, ?, ?)",
            (project_id, _now(), summary, json.dumps(payload, ensure_ascii=False) if payload else None),
        )
        conn.commit()


def import_index_file(project_id: str, path: Path) -> Optional[dict[str, Any]]:
    """One-time fallback: import a leftover index.json in full if this project has no row yet."""
    if not path or not Path(path).is_file():
        return None
    existing = load_index(project_id)
    if existing:
        return existing
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    md = None
    md_path = Path(path).with_name("INDEX.md")
    if md_path.is_file():
        try:
            md = md_path.read_text(encoding="utf-8")
        except Exception:
            md = None
    save_index(project_id, data, md)
    return data


def has_index(project_id: str) -> bool:
    conn = connect()
    with _LOCK:
        cur = conn.execute("SELECT 1 FROM drawing_indexes WHERE project_id = ? LIMIT 1", (project_id,))
        return cur.fetchone() is not None


def has_changelogs(project_id: str) -> bool:
    conn = connect()
    with _LOCK:
        cur = conn.execute("SELECT 1 FROM changelogs WHERE project_id = ? LIMIT 1", (project_id,))
        return cur.fetchone() is not None


def list_changelogs(project_id: str) -> list[dict[str, Any]]:
    conn = connect()
    with _LOCK:
        cur = conn.execute(
            "SELECT id, project_id, ts, summary, payload_json FROM changelogs WHERE project_id = ? ORDER BY id ASC",
            (project_id,),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = {
            "id": row["id"],
            "project_id": row["project_id"],
            "ts": row["ts"],
            "summary": row["summary"],
        }
        raw = row["payload_json"]
        if raw:
            try:
                item["payload"] = json.loads(raw)
            except Exception:
                item["payload"] = raw
        else:
            item["payload"] = None
        out.append(item)
    return out


def _insert_changelog(project_id: str, ts: str, summary: str, payload: Optional[dict[str, Any]] = None) -> None:
    conn = connect()
    with _LOCK:
        conn.execute(
            "INSERT INTO changelogs (project_id, ts, summary, payload_json) VALUES (?, ?, ?, ?)",
            (project_id, ts, summary, json.dumps(payload, ensure_ascii=False) if payload is not None else None),
        )
        conn.commit()


def _changelog_paths(path: Path) -> tuple[Path, Path]:
    path = Path(path)
    if path.is_dir():
        return path / "changelog.json", path / "CHANGELOG.md"
    name = path.name.lower()
    parent = path.parent
    if name == "changelog.json":
        return path, parent / "CHANGELOG.md"
    if name == "changelog.md":
        return parent / "changelog.json", path
    return parent / "changelog.json", parent / "CHANGELOG.md"


def _parse_changelog_json(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        entries = data.get("entries")
        if entries is None and any(k in data for k in ("summary", "ts", "title", "note")):
            entries = [data]
        elif not isinstance(entries, list):
            return []
    elif isinstance(data, list):
        entries = data
    else:
        return []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts") or entry.get("updated_at") or entry.get("date") or _now()
        summary = entry.get("summary") or entry.get("title") or entry.get("note") or "变更记录"
        out.append({"ts": str(ts), "summary": str(summary), "payload": entry})
    return out


def _parse_changelog_md(path: Path) -> list[dict[str, Any]]:
    import re

    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []
    heading = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})(?:\s+(.*))?$")
    time_re = re.compile(r"(\d{4}-\d{2}-\d{2}T[^\s（(]+)")
    entries: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    body: list[str] = []

    def _flush() -> None:
        nonlocal current, body
        if not current:
            return
        note = "\n".join(body).strip()
        payload = {
            "date": current.get("date"),
            "title": current.get("title") or current["summary"],
            "updated_at": current["ts"],
            "note": note,
            "source": "CHANGELOG.md",
        }
        entries.append({"ts": current["ts"], "summary": current["summary"], "payload": payload})
        current = None
        body = []

    for raw in text.splitlines():
        line = raw.strip()
        matched = heading.match(line)
        if matched:
            _flush()
            date, title = matched.group(1), (matched.group(2) or "").strip()
            current = {
                "date": date,
                "title": title,
                "ts": date,
                "summary": title or "变更记录",
            }
            body = []
            continue
        if current is None or not line:
            continue
        item = line[2:].strip() if line.startswith("- ") else line
        if item.startswith("#"):
            continue
        tm = time_re.search(item)
        if tm:
            current["ts"] = tm.group(1)
        elif current["summary"] == "变更记录" and item:
            current["summary"] = item
            current["title"] = item
        body.append(item)
    _flush()
    return entries


def import_changelog_file(project_id: str, path: Path) -> int:
    """One-time fallback: import leftover changelog.json or CHANGELOG.md if no rows exist.

    Prefers changelog.json (stores each entry payload in full). Returns the number of rows inserted.
    """
    if not project_id or not path:
        return 0
    if has_changelogs(project_id):
        return 0
    json_path, md_path = _changelog_paths(Path(path))
    entries = _parse_changelog_json(json_path) if json_path.is_file() else []
    if not entries and md_path.is_file():
        entries = _parse_changelog_md(md_path)
    if not entries:
        return 0
    for entry in entries:
        _insert_changelog(project_id, entry["ts"], entry["summary"], entry.get("payload"))
    return len(entries)
