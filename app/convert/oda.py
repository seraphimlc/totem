"""ODA File Converter（免费版）：优先使用的 DWG → DXF 转换器。

两段式尝试：

1. ``ezdxf.addons.odafc``。ezdxf 只从自己的 ``ezdxf.options`` 或 ``PATH`` 找
   可执行文件，**不会**读 ``CAD_ODA_PATH``，所以先把路径写进
   ``ezdxf.options["odafc-addon"][win_exec_path|unix_exec_path]`` 再用它。
2. 自己按 ODA 的 CLI 约定调用（输入目录 → 输出目录）。odafc 封装在无
   ``DISPLAY`` 的服务器上会抛异常，Linux 上又总以 core dump 收尾，这时
   直接 CLI 更可控。

两种方式都不看返回码：只看输出目录里有没有能打开的非空 DXF。

ODA 的许可由其发行方定义；本项目只调用用户自行安装的程序，不打包、不 vendoring。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

from . import base
from .base import ConvertError, Options

# ODA CLI 的输出 DXF 版本。ACAD2018 是 ODA 官方支持的稳定档位，
# 也是 ezdxf 的 VALID_VERSIONS 之一；读入后由 normalize_utf8() 决定最终编码。
OUTPUT_VERSION = "ACAD2018"


def virtual_display() -> str:
    """Linux 上能拿到的虚拟显示手段："xvfb-run" / "Xvfb" / "DISPLAY"；没有返回 ""。

    三种都要认：
    * ``xvfb-run``：可以直接给 CLI 加前缀；
    * ``Xvfb``：ezdxf 的 ``odafc._linux_dummy_display()`` 是**直接起 Xvfb 二进制**
      的（不走 xvfb-run），所以只装了 ``apt install xvfb`` 的机器也能跑 ODA；
      我们自己的 CLI 调用也可以在这个环境下交给 odafc 之外的方式；
    * ``DISPLAY``：已经有 X 服务器（桌面机 / X 转发）。
    """
    if not sys.platform.startswith("linux"):
        return ""
    if shutil.which("xvfb-run"):
        return "xvfb-run"
    if shutil.which("Xvfb"):
        return "Xvfb"
    if os.environ.get("DISPLAY"):
        return "DISPLAY"
    return ""


def has_display() -> bool:
    """Linux 上的 ODA 需要 X11；其它平台不需要。"""
    if not sys.platform.startswith("linux"):
        return True
    return bool(virtual_display())


def _configure_ezdxf(path: Optional[str]) -> None:
    """把 CAD_ODA_PATH 交给 ezdxf，否则 odafc 永远只看 PATH。"""
    if not path:
        return
    try:
        import ezdxf
        key = "win_exec_path" if sys.platform == "win32" else "unix_exec_path"
        ezdxf.options.set("odafc-addon", key, str(path))
    except Exception:
        base.log.debug("could not set ezdxf odafc path", exc_info=True)


def discover(opts: Options) -> list[str]:
    """按优先级列出可能的 ODA 可执行文件：显式路径 → 平台默认位置 → PATH。"""
    found: list[str] = []
    if opts.oda_path:
        found.append(opts.oda_path)
    if sys.platform == "win32":
        for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
            root = (os.getenv(env_key) or "").strip()
            if not root:
                continue
            try:
                for folder in sorted((Path(root) / "ODA").glob("ODAFileConverter*")):
                    found.append(str(folder / "ODAFileConverter.exe"))
            except OSError:
                pass
    else:
        found.extend(
            [
                "/usr/bin/ODAFileConverter",
                "/usr/local/bin/ODAFileConverter",
                "/opt/ODAFileConverter/ODAFileConverter",
                "/opt/cad-tools/ODAFileConverter/bin/ODAFileConverter",
            ]
        )
        # macOS：ezdxf 在 Darwin 上只查 which，所以 .app 里的可执行文件
        # 只能靠 CAD_ODA_PATH 或这里的补位。
        try:
            found.extend(str(p) for p in Path("/Applications").glob("ODAFileConverter*.app/Contents/MacOS/ODAFileConverter"))
        except OSError:
            pass
        try:
            found.extend(str(p) for p in Path("/opt").glob("ODAFileConverter*"))
        except OSError:
            pass
    which = shutil.which("ODAFileConverter")
    if which:
        found.append(which)
    hits: list[str] = []
    for path in found:
        if path and path not in hits and base.is_executable(path):
            hits.append(path)
    return hits


def find(opts: Optional[Options] = None) -> Optional[str]:
    opts = opts or Options.from_env()
    hits = discover(opts)
    return hits[0] if hits else None


def _via_odafc(src: Path, out_dir: Path, timeout: float) -> Path:
    """用 ezdxf 的 odafc 封装转换。

    ``odafc.readfile()`` 自己不带超时，这里放进 daemon 线程里等：超时就不等它，
    交给 CLI 兜底（线程迟早会结束，进程退出也不受影响）。
    """
    try:
        from ezdxf.addons import odafc
    except Exception as exc:
        raise ConvertError("odafc_unavailable", str(exc)[:200], tool="oda") from exc
    if not odafc.is_installed():
        raise ConvertError("odafc_unavailable", "ezdxf 没有发现 ODA", tool="oda")

    holder: dict = {}

    def _work() -> None:
        try:
            doc = odafc.readfile(str(src))
            target = out_dir / (src.stem + ".dxf")
            doc.saveas(str(target))
            holder["path"] = target
        except BaseException as exc:  # noqa: BLE001 - 原样交给调用方判断
            holder["error"] = exc

    thread = threading.Thread(target=_work, name="odafc", daemon=True)
    thread.start()
    thread.join(max(float(timeout), 1.0))
    if thread.is_alive():
        raise ConvertError("timeout", "odafc 超过 %.0f 秒" % float(timeout), tool="oda")
    if "error" in holder:
        err = holder["error"]
        # ezdxf 的异常里带着 ODA 的 stderr，末行通常就是真正原因，优先取它。
        lines = [ln.strip() for ln in str(err).splitlines() if ln.strip()]
        detail = lines[-1] if lines else str(err)
        raise ConvertError("odafc_failed", detail[:200], tool="oda")
    target = holder.get("path")
    if not target or not base.looks_like_dxf(Path(target)):
        raise ConvertError("empty_output", "odafc 没有产出可读 DXF", tool="oda")
    return Path(target)


def _oda_input_name(src: Path) -> str:
    """ODA 输入目录里的文件名。

    官方过滤器 ``*.DWG`` 是**扩展名大小写敏感**的（Linux 上尤其明显），而用户
    上传的文件几乎都是小写 ``.dwg``，原样拷进去 ODA 会一个都不认。所以统一
    成大写 ``.DWG``；主文件名只做最小清洗：去掉会破坏 glob 的 ``* ? [ ]``，
    以及以 ``.`` 开头的隐藏名。
    """
    stem = re.sub(r"[*?\[\]\x00-\x1f]", "_", Path(src).stem).strip()
    if not stem or stem.startswith("."):
        stem = "source"
    return f"{stem}.DWG"


def _via_cli(exe: str, src: Path, work: Path, timeout: float) -> Path:
    """ODAFileConverter 输入目录 输出目录 版本 类型 递归 审计 [过滤]。"""
    in_dir = work / "oda-in"
    out_dir = work / "oda-out"
    for folder in (in_dir, out_dir):
        folder.mkdir(parents=True, exist_ok=True)
    in_name = _oda_input_name(src)
    # ODA 按目录批量处理，输出文件名 = 输入文件名换后缀，所以留主文件名、只换后缀。
    shutil.copy2(src, in_dir / in_name)
    argv = [exe, str(in_dir), str(out_dir), OUTPUT_VERSION, "DXF", "0", "0", "*.DWG"]
    if virtual_display() == "xvfb-run":
        argv = ["xvfb-run", "-a", *argv]
    proc = base.run(argv, timeout)
    hit = base.find_dxf_in(out_dir, stem=Path(in_name).stem)
    if hit is None:
        raise ConvertError("empty_output", base.last_line(proc), tool="oda")
    return hit


def convert(src: Path, dest: Path, opts: Options, budget: base.Budget, *, sanitize=None) -> base.ConversionResult:
    """ODA 优先：odafc 失败再 CLI。两条都失败才抛 ``ConvertError``。

    中间文件放在 ``CAD_DWG_CONVERT_TMPDIR``（或目标旁）下的**唯一子目录**里，
    结束时只删这个子目录，绝不删用户指定的目录本身。
    """
    exe = find(opts)
    if not exe:
        raise ConvertError("no_oda", "找不到 ODAFileConverter", tool="oda")
    if not has_display():
        raise ConvertError("no_display", "没有 xvfb-run 也没有 DISPLAY", tool="oda")

    _configure_ezdxf(exe)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    root = base.workdir(dest, opts)
    work = Path(tempfile.mkdtemp(prefix="oda-", dir=str(root)))
    warnings: list[str] = []
    produced: Optional[Path] = None
    how = ""
    try:
        timeout = budget.slice(opts.oda_timeout)
        if timeout <= 0:
            raise ConvertError("timeout", "超过总预算", tool="oda")
        scr = work / "odafc-out"
        scr.mkdir(parents=True, exist_ok=True)
        try:
            produced = _via_odafc(src, scr, timeout)
            how = "odafc"
        except ConvertError as exc:
            warnings.append("odafc 失败（%s），改用 CLI" % (exc.detail or exc.code))
            timeout = budget.slice(opts.oda_timeout)
            if timeout <= 0:
                raise ConvertError("timeout", "超过总预算", tool="oda") from exc
            produced = _via_cli(exe, src, work, timeout)
            how = "cli"
        base.replace_file(produced, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    version = base.normalize_utf8(dest, sanitize=sanitize)
    if not version:
        warnings.append("规范化失败，保留原始转换稿")
    if not base.looks_like_dxf(dest):
        warnings.append("产物里没有模型空间实体")
    return base.ConversionResult(
        ok=True,
        tool="oda",
        tool_path=exe,
        how=how,
        output_path=dest,
        dxfversion=version or "",
        warnings=warnings,
    )
