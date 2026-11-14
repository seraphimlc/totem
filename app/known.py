"""Curated indexes for drawings we already checked, matched by file hash."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

KNOWN_INDEX_DIR = Path(__file__).resolve().parent.parent / "known-indexes"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_known_drawings() -> list[dict[str, Any]]:
    manifest = KNOWN_INDEX_DIR / "manifest.json"
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list(data.get("drawings") or [])


def match_known_drawing(dxf_path: Path) -> Optional[dict[str, Any]]:
    digest = file_sha256(dxf_path)
    for item in load_known_drawings():
        hashes = [item.get("sha256"), *(item.get("sha256_list") or [])]
        if digest in hashes:
            return item
    return None


def known_index_dir(item: dict[str, Any]) -> Path:
    return KNOWN_INDEX_DIR / (item.get("index_dir") or item["id"])


def detect_dxf_encoding(path: Path, hint: Optional[str] = None) -> str:
    if hint in {"gbk", "gb18030"}:
        return "gbk"
    if hint in {"utf-8", "utf8"}:
        return "utf-8"
    # $DWGCODEPAGE is often left as ANSI_936 even when the body is UTF-8,
    # so prefer common CAD Chinese words over the header.
    data = path.read_bytes()[:800_000]
    markers = (
        "平面", "布置", "卫生间", "宋体", "图框", "说明", "装饰", "给排水",
        "请下载", "天正", "图层", "比例", "详图", "喷淋", "拆除",
    )

    def score(enc: str) -> int:
        text = data.decode(enc, errors="ignore")
        return sum(text.count(w) for w in markers)

    s_utf, s_gbk = score("utf-8"), score("gbk")
    if s_gbk > s_utf + 2:
        return "gbk"
    if s_utf > s_gbk + 2:
        return "utf-8"
    header = re.search(rb"\$DWGCODEPAGE[\s\S]{0,80}?(ANSI_\d+|UTF[-_]?8)", data, re.I)
    if header:
        val = header.group(1).decode("ascii", "ignore").upper()
        if "936" in val or "GB" in val:
            return "gbk"
        if "UTF" in val:
            return "utf-8"
    return "utf-8"
