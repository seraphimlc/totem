#!/usr/bin/env python3
"""Dining furniture labels, excerpt, and agreement-to-plan nudges."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CAD_DATA_DIR", "")

KNOWN = ROOT / "known-indexes" / "xueshengshitang" / "index.json"
REGION = {
    "kind": "sheet",
    "title": "1F装饰平面布置图",
    "sheet_id": "1F-装饰平面布置图",
    "floor": "1F",
    "bbox": [1541089.9323, -37617.6922, 1630189.9323, 25382.3078],
}


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""))


def _cafeteria_index() -> dict:
    data = json.loads(KNOWN.read_text(encoding="utf-8"))
    from app import indexing

    return indexing.relabel_index(data)


def test_query_tokens_and_labels() -> None:
    from app import indexing

    for text in ("餐桌密度", "餐桌的密度太大了", "把餐桌疏一疏"):
        tokens = indexing._query_tokens(text)
        _ok(f"tokens-has-餐桌:{text}", "餐桌" in tokens, str(tokens))

    index = _cafeteria_index()
    tables = [e for e in index["equipment"] if e.get("block") == "A$C783832BF"]
    _ok("typology-labels-all-tables", len(tables) == 220 and all(e.get("display_name") == "餐桌" for e in tables), str(len(tables)))
    _ok("official-name-unchanged", all(e.get("block") == "A$C783832BF" for e in tables))
    _ok("aliases-include-桌椅", all("桌子" in (e.get("aliases") or []) for e in tables))

    f1 = [e for e in tables if e.get("floor") == "1F" or "1F-装饰平面" in (e.get("sheet") or "")]
    _ok("1F-tables-stay-on-1F", len(f1) == 77, str(len(f1)))
    rooms = {e.get("room") for e in f1}
    _ok("1F-tables-attached-to-餐厅", rooms == {"餐厅"}, str(rooms))

    dining = next(r for r in index["rooms"] if r.get("name") == "餐厅" and r.get("floor") == "1F")
    nearby = dining.get("equipment_nearby") or []
    furn = [e for e in nearby if e.get("block") == "A$C783832BF"]
    _ok("餐厅-nearby-has-1F-tables", len(furn) == 77, str(len(furn)))

    toilet = next(r for r in index["rooms"] if r.get("name") == "卫生间" and r.get("floor") == "1F")
    toilet_tables = [e for e in (toilet.get("equipment_nearby") or []) if e.get("block") == "A$C783832BF"]
    _ok("卫生间-not-flooded-with-tables", toilet_tables == [], str(len(toilet_tables)))

    other = [e for e in index["equipment"] if e.get("block") == "A$C89f64b4f"]
    _ok("unknown-furn-not-forced-into-餐厅", all(e.get("room") != "餐厅" for e in other), str([e.get("room") for e in other]))


def test_excerpt_table_density() -> None:
    from app import indexing, projects

    index = _cafeteria_index()
    hits = indexing.search_equipment(index, "餐桌密度", region=REGION, limit=40)
    _ok("search-finds-tables", len(hits) >= 1 and all(h.get("block") == "A$C783832BF" for h in hits), str(len(hits)))
    _ok("search-stays-on-1F", all("1F" in (h.get("sheet") or "") or h.get("floor") == "1F" for h in hits))
    _ok("search-has-coords", all(isinstance(h.get("xy"), list) and len(h.get("xy") or []) >= 2 for h in hits))

    proj = {"name": "学生食堂", "encoding": "utf-8", "dxf_version": "AC1021"}
    original = projects.load_index
    projects.load_index = lambda _p: json.loads(KNOWN.read_text(encoding="utf-8"))  # unlabeled; excerpt relabels
    try:
        excerpt = projects.excerpt_index(proj, REGION, "餐桌密度")
        excerpt2 = projects.excerpt_index(proj, REGION, "餐桌的密度太大了")
    finally:
        projects.load_index = original

    for name, text in (("密度", excerpt), ("密度太大了", excerpt2)):
        _ok(f"excerpt-has-official-block:{name}", "A$C783832BF" in text, text[:400])
        _ok(f"excerpt-has-display-餐桌:{name}", "餐桌" in text)
        _ok(f"excerpt-has-coords:{name}", "x=" in text and "158" in text)
        furn = text.split("房间：")[0] if "房间：" in text else text
        _ok(f"excerpt-not-all-floors:{name}", "2F-装饰平面布置图" not in furn)
        # Must not invent a Chinese official block name.
        _ok(f"excerpt-no-invented-official-name:{name}", "块名 `餐桌`" not in text and "块名 `9910桌`" not in text)
        _ok(f"excerpt-not-truncated:{name}", "…（摘录已截断）" not in text, f"len={len(text)}")
        # Compact row groups, not 77 verbose lines that bloated the MaaS POST.
        _ok(f"excerpt-grouped-rows:{name}", "套  x=" in text)
        tables = [e for e in index["equipment"] if e.get("block") == "A$C783832BF" and e.get("floor") == "1F"]
        have = sum(1 for e in tables if f"{e['xy'][0]:.1f}" in text)
        _ok(f"excerpt-lists-all-1F-tables:{name}", have == 77, f"have={have} len={len(text)}")
        _ok(f"excerpt-compact:{name}", len(text) < 9000, f"len={len(text)}")
        _ok(f"excerpt-under-cap-or-keeps-furniture:{name}", "本图幅相关家具" in text)


def test_relabel_idempotent() -> None:
    from app import indexing

    index = _cafeteria_index()
    again = indexing.relabel_index(index)
    tables = [e for e in again["equipment"] if e.get("block") == "A$C783832BF" and e.get("floor") == "1F"]
    dining = next(r for r in again["rooms"] if r.get("name") == "餐厅" and r.get("floor") == "1F")
    _ok("relabel-idempotent-count", len(tables) == 77)
    nearby = [e for e in (dining.get("equipment_nearby") or []) if e.get("block") == "A$C783832BF"]
    _ok("relabel-idempotent-nearby", len(nearby) == 77, str(len(nearby)))


def test_maas_write_timeout() -> None:
    import httpx
    from app import maas

    _ok("write-timeout-120", maas.REQUEST_TIMEOUT.write == 120.0, str(maas.REQUEST_TIMEOUT.write))
    _ok("read-timeout-kept", maas.REQUEST_TIMEOUT.read == 180.0)
    _ok("connect-timeout-kept", maas.REQUEST_TIMEOUT.connect == 15.0)

    # complete() must turn a write timeout with an image into a retryable MaasError
    # so the chat path can resend text-only instead of showing 回复时出了问题.
    async def _fake_stream(_client, _payload):
        raise httpx.WriteTimeout("write timed out")
        yield {}  # pragma: no cover — make this an async generator

    async def _run():
        orig = maas._stream
        maas._stream = _fake_stream  # type: ignore
        try:
            agen = maas.complete(
                model="Tifa-6.1-sol",
                system="sys",
                user_text="餐桌的密度太大了，你有什么想法",
                image_data_url="data:image/png;base64,AAAA",
            )
            try:
                async for _ in agen:
                    pass
            except maas.MaasError as exc:
                _ok("write-timeout-retryable", exc.retryable_without_image is True, str(exc))
                return
            raise AssertionError("FAIL write-timeout-retryable: no MaasError")
        finally:
            maas._stream = orig

    import asyncio
    # complete() checks MAAS_KEY only when sending; stub _headers too if needed.
    # _stream is entered after payload build, headers are built inside _stream.
    # Patch _headers and _url so we never touch env secrets / network.
    orig_headers, orig_url = maas._headers, maas._url
    maas._headers = lambda: {"Authorization": "Bearer test", "Content-Type": "application/json"}  # type: ignore
    maas._url = lambda: "http://example.invalid/chat/completions"  # type: ignore
    try:
        asyncio.run(_run())
    finally:
        maas._headers, maas._url = orig_headers, orig_url


if __name__ == "__main__":
    test_query_tokens_and_labels()
    test_excerpt_table_density()
    test_relabel_idempotent()
    test_maas_write_timeout()
    print("ALL PASS")
