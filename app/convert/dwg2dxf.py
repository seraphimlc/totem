"""LibreDWG 的 dwg2dxf：保底转换器。

从 ``app/init_proj.py`` 原样迁出 ``find_dwg2dxf()`` / ``_windows_dwg2dxf_candidates()``
/ ``_which_where()`` 与 ``convert_dwg()`` 的 dwg2dxf 分支：调用参数、判成功的
方式、Windows 的探测路径都不变。差别只有：错误改成带 code 的
:class:`ConvertError`，超时受总预算约束，落盘走 ``base.replace_file()``。

LibreDWG 是 GPLv3 软件。这里只按路径调用子进程，不链接、不打包、不 vendoring。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from . import base
from .base import ConvertError, Options

# 与改动前一致的 Linux 查找顺序。
_LINUX_CANDIDATES = (
    "/tmp/cad-tools/prefix/bin/dwg2dxf",
    "/opt/cad-tools/prefix/bin/dwg2dxf",
    "/opt/cad-studio/tools/bin/dwg2dxf",
    "/usr/local/bin/dwg2dxf",
)


def _windows_dwg2dxf_candidates() -> list[str]:
    """Extra lookup paths for Windows installs (LibreDWG / ODA builds)."""
    found: list[str] = []
    local = (os.getenv("LOCALAPPDATA") or "").strip()
    if local:
        programs = Path(local) / "Programs"
        found.extend(str(p) for p in programs.glob("**/dwg2dxf.exe"))
        found.extend(str(p) for p in programs.glob("**/bin/dwg2dxf.exe"))
    for base_dir in (r"C:\Program Files\DWG", r"C:\Program Files\LibreDWG", r"C:\Program Files (x86)\DWG"):
        try:
            if Path(base_dir).is_dir():
                found.extend(str(p) for p in Path(base_dir).glob("**/dwg2dxf.exe"))
        except OSError:
            continue
    return found


def _which_where(name: str) -> str:
    """shutil.which, then the Windows `where` command for shims it misses."""
    hit = shutil.which(name)
    if hit:
        return hit
    if os.name != "nt":
        return ""
    try:
        proc = subprocess.run(
            ["where", name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line and Path(line).is_file():
            return line
    return ""


def find(opts: Optional[Options] = None) -> Optional[str]:
    """按原顺序找：CAD_DWG2DXF → Linux 固定路径 → PATH →（Windows）常见安装目录。"""
    opts = opts or Options.from_env()
    candidates = [opts.dwg2dxf_path, *_LINUX_CANDIDATES, shutil.which("dwg2dxf") or ""]
    if sys.platform == "win32":
        candidates.extend(_windows_dwg2dxf_candidates())
        candidates.append(_which_where("dwg2dxf"))
        candidates.append(_which_where("dwg2dxf.exe"))
    for path in candidates:
        if path and base.is_executable(path):
            return path
    return None


def _hint(procs: list[subprocess.CompletedProcess]) -> str:
    """用户文案里的括号内容：优先最后一次尝试的 stderr/stdout 末行。"""
    for proc in reversed(procs):
        line = base.last_line(proc)
        if line:
            return line
    return "没有输出"


def convert(src: Path, dest: Path, opts: Options, budget: base.Budget, *, sanitize=None) -> base.ConversionResult:
    """把 ``src``（.dwg）转成 ``dest``（.dxf），语义与改动前一致：

    1. 先把源文件拷进中间目录（dwg2dxf 默认写在源文件旁边）；
    2. 先试 ``-y -o outfile src``（``-o`` 要的是**文件**，不是目录）；
    3. 没有产物再试 ``-y src``，在中间目录里找它写出的 DXF；
    4. 有非空产物才算成功；拷到 dest 后规范化成 UTF-8。
    """
    tool = find(opts)
    if not tool:
        raise ConvertError("no_dwg2dxf", "找不到 dwg2dxf", tool="dwg2dxf")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 中间目录：
    #  * 默认（目标旁的 .convert-tmp）是我们自己的目录，沿用旧行为每次清空，
    #    避免旧产物被当成本次结果；
    #  * CAD_DWG_CONVERT_TMPDIR 是**用户**的目录，里面可能有别人的文件，
    #    绝不能整目录删或清空。和 oda.py 一样在它下面开一个唯一子目录，
    #    结束时只删这个子目录。
    root = base.workdir(dest, opts)
    if opts.tmpdir:
        tmp_dir = Path(tempfile.mkdtemp(prefix="dwg2dxf-", dir=str(root)))
    else:
        tmp_dir = root
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        tmp_src = tmp_dir / Path(src).name
        shutil.copy2(src, tmp_src)
        return _run_conversion(tool, tmp_dir, tmp_src, dest, opts, budget, sanitize)
    finally:
        # 两种情形都只删本次使用的 tmp_dir：默认时它就是 .convert-tmp 本身，
        # 用户指定时它是 .convert-tmp 等价物下面的一个唯一子目录。
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_conversion(
    tool: str,
    tmp_dir: Path,
    tmp_src: Path,
    dest: Path,
    opts: Options,
    budget: base.Budget,
    sanitize,
) -> base.ConversionResult:
    procs: list[subprocess.CompletedProcess] = []

    def _timeout() -> float:
        left = budget.slice(opts.dwg2dxf_timeout)
        if left <= 0:
            raise ConvertError("timeout", "超过总预算", tool="dwg2dxf")
        return left

    # dwg2dxf 可以 exit 0 却打印 "WRITE ERROR"，所以产物文件是唯一证据。
    out_dxf = tmp_dir / "source.dxf"
    out_dxf.unlink(missing_ok=True)
    procs.append(base.run([tool, "-y", "-o", str(out_dxf), str(tmp_src)], _timeout()))
    produced: Optional[Path] = None
    if out_dxf.is_file() and out_dxf.stat().st_size > 0:
        produced = out_dxf
    else:
        # 有些构建只认输入路径，把结果写在源文件旁边。
        procs.append(base.run([tool, "-y", str(tmp_src)], _timeout()))
        hits = sorted(tmp_dir.glob("*.dxf")) + sorted(tmp_dir.glob("*.DXF"))
        hits = [h for h in hits if h.stat().st_size > 0]
        if not hits:
            raise ConvertError("empty_output", _hint(procs), tool="dwg2dxf")
        produced = hits[0]
    base.replace_file(produced, dest)

    version = base.normalize_utf8(dest, sanitize=sanitize)
    warnings = [] if version else ["规范化失败，保留原始转换稿"]
    if not base.looks_like_dxf(dest):
        warnings.append("产物里没有模型空间实体")
    return base.ConversionResult(
        ok=True,
        tool="dwg2dxf",
        tool_path=tool,
        how="explicit" if opts.dwg2dxf_path and tool == opts.dwg2dxf_path else "discovered",
        output_path=dest,
        dxfversion=version or "",
        warnings=warnings,
    )
