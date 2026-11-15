#!/usr/bin/env python3
"""材质表空引用自愈：DWG 转 DXF 丢 MATERIAL 对象时，写入仍要能成功。

复现路径：转换产物只留 ACAD_MATERIAL 字典里的 handle，OBJECTS 段没有 MATERIAL。
ezdxf 保存时 _update_header_vars 取 materials.get("ByLayer").dxf → AttributeError，
apply 直接 500，图没变。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf

from app import apply as appapply


def _drop_material_objects(text: str) -> str:
    """把 DXF 里所有 0/MATERIAL 对象整段删掉，模拟转换器的丢失。"""
    lines = text.splitlines(keepends=True)
    # DXF 是 (group code, value) 成对的行；必须成对删，
    # 否则值恰好是 "0" 的行会让后面的结构错位。
    assert len(lines) % 2 == 0, f"行数不成对：{len(lines)}"
    pairs = [(lines[i], lines[i + 1]) for i in range(0, len(lines), 2)]
    out: list[str] = []
    i = 0
    dropped = 0
    while i < len(pairs):
        code, value = pairs[i]
        if code.strip() == "0" and value.strip() == "MATERIAL":
            i += 1
            while i < len(pairs) and pairs[i][0].strip() != "0":
                i += 1
            dropped += 1
            continue
        out.append(code)
        out.append(value)
        i += 1
    assert dropped, "没有找到 MATERIAL 对象，测试前提不成立"
    return "".join(out)


def _make_clean(path: Path) -> Path:
    """造一张最小但完整的图纸。"""
    doc = ezdxf.new("R2007", setup=True)
    doc.layers.add("图框-测试", color=7)
    blk = doc.blocks.new(name="TOILET")
    blk.add_circle((0, 0), radius=150)
    blk.add_line((-150, 0), (150, 0))
    msp = doc.modelspace()
    msp.add_line((0, 0), (10000, 0), dxfattribs={"layer": "图框-测试"})
    msp.add_line((0, 0), (0, 8000), dxfattribs={"layer": "图框-测试"})
    msp.add_text("卫生间", height=200).set_placement((500, 500))
    msp.add_blockref("TOILET", (2000, 2000))
    doc.saveas(str(path))
    return path


def _strip_materials(path: Path) -> Path:
    text = path.read_text(encoding="utf-8")
    path.write_text(_drop_material_objects(text), encoding="utf-8")
    return path


def case_reproduces_bug(broken: Path) -> None:
    doc = ezdxf.readfile(str(broken))
    got = doc.materials.get("ByLayer")
    assert isinstance(got, str), f"应该复现悬空引用，实际是 {type(got).__name__}"
    try:
        out = broken.with_name("plain.dxf")
        doc.saveas(str(out))
    except AttributeError as exc:
        print(f"PASS reproduce-bug → {exc}")
        return
    raise AssertionError("原样保存竟然成功了，测试前提不成立")


def case_repair_read(broken: Path) -> None:
    doc = appapply._read_doc(broken, "utf-8")
    for name in ("ByBlock", "ByLayer", "Global"):
        got = doc.materials.get(name)
        assert hasattr(got, "dxf"), f"{name} 应该是 Material 实体，实际 {type(got).__name__}"
        assert got.dxf.handle, f"{name} 缺 handle"
    print("PASS repair-on-read → ByBlock/ByLayer/Global 都是 Material")


def case_repair_is_idempotent(clean: Path) -> None:
    doc = appapply._read_doc(clean, "utf-8")
    before = {n: doc.materials.get(n).dxf.handle for n in ("ByBlock", "ByLayer", "Global")}
    fixed = appapply._repair_material_table(doc)
    after = {n: doc.materials.get(n).dxf.handle for n in ("ByBlock", "ByLayer", "Global")}
    assert fixed == 0, f"干净文件不该被改动，实际修了 {fixed} 处"
    assert before == after, "干净文件的材质对象被换掉了"
    print("PASS repair-idempotent → 干净文件 0 处改动")


def case_save_roundtrip(broken: Path) -> None:
    doc = appapply._read_doc(broken, "utf-8")
    msp = doc.modelspace()
    msp.add_blockref("TOILET", (6000, 3000))
    out = broken.with_name("saved.dxf")
    appapply._save_doc(doc, out)
    assert out.is_file(), "没有写出文件"

    again = ezdxf.readfile(str(out))
    mats = {k: type(v).__name__ for k, v in again.materials}
    assert all(t == "Material" for t in mats.values()), mats
    xy = [
        tuple(round(v, 3) for v in (e.dxf.insert.x, e.dxf.insert.y))
        for e in again.modelspace().query("INSERT")
    ]
    assert (6000.0, 3000.0) in xy, xy
    print(f"PASS save-roundtrip → {out.stat().st_size} bytes, insert={xy}")


def case_save_retry_path(broken: Path) -> None:
    """绕过 _read_doc 自愈（模拟内存文档中途又坏了），_save_doc 要自己救回来。"""
    doc = ezdxf.readfile(str(broken))
    assert isinstance(doc.materials.get("ByLayer"), str)
    out = broken.with_name("retry.dxf")
    appapply._save_doc(doc, out)
    assert out.is_file(), "重试路径没写出文件"
    again = ezdxf.readfile(str(out))
    assert hasattr(again.materials.get("ByLayer"), "dxf")
    print("PASS save-retry-repairs → _save_doc 自己修回来了")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tuteng-matfix-"))
    try:
        clean = _make_clean(tmp / "clean.dxf")
        case_repair_is_idempotent(clean)
        broken = _strip_materials(shutil.copy2(clean, tmp / "broken.dxf"))
        case_reproduces_bug(broken)
        case_repair_read(broken)
        case_save_roundtrip(broken)
        case_save_retry_path(broken)
        print("ALL PASSED")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
