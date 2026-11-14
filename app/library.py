from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from . import config

_STOP = {"一个", "一下", "我们", "这个", "那个", "以及", "或者", "还有", "什么", "哪些", "里面", "都有"}
_LEAD = {"加", "补", "放", "再", "请", "帮", "要", "用", "在", "把", "给", "向", "往", "里", "都", "有", "的", "是"}


def index_path() -> Optional[Path]:
    return _index_path()


def _index_path() -> Optional[Path]:
    env = getattr(config, "CAD_LIBRARY_INDEX", "") or ""
    candidates = []
    if env:
        candidates.append(Path(env))
    data = (config.CAD_DATA_DIR or "").strip()
    if data:
        candidates.append(Path(data) / "libraries" / "default" / "index.json")
    candidates.extend(
        [
            config.STUDIO_ROOT / "data" / "libraries" / "default" / "index.json",
            config.STUDIO_ROOT.parent / "libraries" / "default" / "index.json",
            Path("/workspace/cad-nl/libraries/default/index.json"),
        ]
    )
    for p in candidates:
        if p.is_file():
            return p
    return None


@lru_cache(maxsize=2)
def load_index(mtime: float = 0.0) -> dict[str, Any]:
    path = _index_path()
    if not path:
        return {"blocks": [], "libraries": [], "name": "默认组件库"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = str(path)
        return data
    except Exception:
        return {"blocks": [], "libraries": [], "name": "默认组件库"}


def get_index() -> dict[str, Any]:
    path = _index_path()
    mtime = path.stat().st_mtime if path and path.is_file() else 0.0
    return load_index(mtime)


def _tokens(text: str) -> list[str]:
    parts = re.findall(r"[\u4e00-\u9fff]{1,}|[A-Za-z0-9_$-]{2,}", text or "")
    tokens: list[str] = []
    for p in parts:
        if p in _STOP:
            continue
        if all("\u4e00" <= c <= "\u9fff" for c in p):
            rest = p
            for s in sorted(_STOP, key=len, reverse=True):
                rest = rest.replace(s, " ")
            for ch in re.split(r"\s+", rest):
                while ch and ch[0] in _LEAD:
                    ch = ch[1:]
                while ch and ch[-1] in _LEAD:
                    ch = ch[:-1]
                if len(ch) >= 2 and ch not in _STOP:
                    tokens.append(ch)
        elif len(p) >= 2:
            tokens.append(p)
    return list(dict.fromkeys(tokens))


def _is_auto_name(name: str) -> bool:
    n = name or ""
    return n.startswith("A$C") or "做成ACAD" in n


def search(query: str, *, limit: int = 16) -> list[dict[str, Any]]:
    tokens = _tokens(query)
    if not tokens:
        return []
    synonyms = {"坐便": "座便", "座便": "坐便", "坐便器": "座便器", "座便器": "坐便器", "马桶": "坐便器"}
    extra = []
    for tok in tokens:
        if tok in synonyms:
            extra.append(synonyms[tok])
        if tok.endswith("器") and len(tok) >= 3:
            extra.append(tok[:-1])
        elif tok in {"坐便", "座便", "蹲便"}:
            extra.append(tok + "器")
    tokens = list(dict.fromkeys(tokens + extra))
    if any(t in {"坐便", "座便", "坐便器", "座便器"} for t in tokens):
        tokens = list(dict.fromkeys(tokens + ["马桶"]))
    blocks = get_index().get("blocks") or []
    scored: list[tuple[int, dict[str, Any]]] = []
    for b in blocks:
        if b.get("anonymous"):
            continue
        name = b.get("name") or ""
        display = b.get("display_name") or name
        labels = " ".join(b.get("labels_nearby") or [])
        cat = b.get("category") or ""
        blob_name = f"{name} {display}"
        score = 0
        for tok in tokens:
            if tok == display or tok == name:
                score += 20
            elif name.startswith(tok) or display.startswith(tok):
                score += 14
            elif tok in name or (tok in display and len(display) <= 16):
                score += 8
            elif tok in labels and len(display) <= 16:
                score += 3
        if score:
            if len(display or "") > 20:
                score -= 8
            if str(name).startswith("A$C") or "做成ACAD" in str(name):
                score -= 4
            scored.append((score, b))
    scored.sort(key=lambda x: -x[0])
    seen = set()
    good, weak = [], []
    for _, b in scored:
        key = b.get("name")
        if not key or key in seen:
            continue
        seen.add(key)
        (weak if _is_auto_name(key) else good).append(b)
    # Prefer properly named blocks, but keep auto/hash names whose display_name
    # clearly matches the query (e.g. 做成ACAD… 显示为 座便器).
    named_hits, typo_hits = [], []
    for b in good + weak:
        disp = b.get("display_name") or ""
        name = b.get("name") or ""
        if _is_auto_name(name) and any(tok in disp for tok in tokens if len(tok) >= 2):
            typo_hits.append(b)
        elif not _is_auto_name(name):
            named_hits.append(b)
        elif not named_hits:
            typo_hits.append(b)
    out = named_hits + typo_hits
    # Dedup
    seen, uniq = set(), []
    for b in out:
        if b.get("name") in seen:
            continue
        seen.add(b.get("name"))
        uniq.append(b)
    return uniq[:limit]


def excerpt(query: str, *, max_chars: int = 2800) -> str:
    data = get_index()
    blocks = data.get("blocks") or []
    named = sum(1 for b in blocks if not b.get("anonymous"))
    libs = data.get("libraries") or []
    lines = [
        f"默认组件库：共 {named} 个命名块。library-1 偏装饰与注释符号，library-2 偏家具与洁具。",
        "新增图块时优先从本库选用，inserts 的 block 字段写库里的块名；本库没有、图里也没有时如实说明，不要编造块名。",
    ]
    hits = search(query)
    if not hits:
        lines.append("本次提问没有直接命中的库内块。厨房工艺设备（如 RAWEN、和面机、压面机）以当前图纸已有块为准。")
        return "\n".join(lines)
    lines.append("与本次相关的库内块：")
    for b in hits:
        size = ""
        if b.get("width_mm") is not None and b.get("height_mm") is not None:
            size = f"  尺寸={b['width_mm']:.0f}×{b['height_mm']:.0f}mm"
        disp = b.get("display_name") or b.get("name")
        extra = f"  显示名={disp}" if disp != b.get("name") else ""
        labs = "、".join((b.get("labels_nearby") or [])[:3])
        lab = f"  标签={labs}" if labs else ""
        lines.append(
            f"- `{b.get('name')}`  库={b.get('library')}  分类={b.get('category')}"
            f"{extra}  图层={b.get('typical_layer') or ''}{size}{lab}"
        )
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 12] + "\n…（已截断）"
    return text
