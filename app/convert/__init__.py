"""DWG → DXF：ODA File Converter 优先，dwg2dxf（LibreDWG）兜底。

``app/init_proj.py`` 里的 ``convert_dwg()`` / ``find_dwg2dxf()`` 保留为薄包装，
上传初始化（``_ensure_dxf()``）的调用方式不变。

环境变量（全部可选）：

- ``CAD_DWG_CONVERTER``：auto / oda / dwg2dxf / none，默认 auto
- ``CAD_ODA_PATH``：ODA 可执行文件路径，默认自动发现
- ``CAD_ODA_TIMEOUT``：单次 ODA 超时（秒），默认 180
- ``CAD_DWG2DXF``：dwg2dxf 可执行文件路径，默认自动发现
- ``CAD_DWG2DXF_TIMEOUT``：单次 dwg2dxf 超时（秒），默认 180
- ``CAD_DWG_CONVERT_TMPDIR``：转换中间目录，默认目标文件旁的 .convert-tmp
- ``CAD_DWG_CONVERT_BUDGET``：一次请求的总预算（秒），默认 270

失败抛 :class:`ConvertError`（``ValueError`` 的子类），``detail`` 是给用户看的
那一行；``init_proj.convert_dwg()`` 用它拼回原来的中文文案。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import dwg2dxf, oda
from .base import Budget, ConvertError, ConversionResult, Options

log = logging.getLogger("cad-studio.convert")

__all__ = [
    "ConvertError",
    "ConversionResult",
    "MSG_FAILED",
    "MSG_NO_TOOL",
    "Options",
    "ServerConverter",
    "convert_dwg",
    "converter_status",
    "find_converter",
    "find_dwg2dxf",
    "health_info",
    "invalidate_health_cache",
    "dwg2dxf",
    "oda",
]

# 用户可见的固定文案，与改动前逐字一致。
MSG_NO_TOOL = "服务器目前不能直接读取 DWG。请先在 AutoCAD 里另存为 DXF（建议 AC1021）后再上传。"
MSG_FAILED = "DWG 转换没有成功（%s）。请改传 DXF，或检查原文件是否完好。"


@dataclass(frozen=True)
class ServerConverter:
    """这台机器上可用的转换器。``kind == "none"`` 表示没有任何可用的转换器。"""

    kind: str                       # oda | dwg2dxf | none
    how: str                        # explicit | discovered
    path: str                       # 只进日志，不要直接回给 HTTP
    tools: tuple[str, ...] = ()     # 按顺序尝试的工具链


def find_converter(opts: Optional[Options] = None) -> ServerConverter:
    """按 ``CAD_DWG_CONVERTER`` 决定工具链。

    auto：ODA 优先，其次 dwg2dxf；oda / dwg2dxf：只用指定工具，缺了就是 none；
    none：关闭转换（上传 DWG 时直接给出提示）。
    """
    opts = opts or Options.from_env()
    if opts.mode == "none":
        return ServerConverter("none", "", "")

    oda_exe = oda.find(opts) if opts.mode in {"auto", "oda"} else None
    if oda_exe and oda.has_display():
        how = "explicit" if oda_exe == opts.oda_path else "discovered"
        tools = ("oda", "dwg2dxf") if opts.mode == "auto" else ("oda",)
        return ServerConverter("oda", how, oda_exe, tools)

    if opts.mode == "oda":
        # 明确只用 ODA，但 ODA 不可用：不偷偷换成 dwg2dxf。
        return ServerConverter("none", "", "")

    if oda_exe:
        log.warning("ODAFileConverter found at %s but no X display; using dwg2dxf", oda_exe)

    dwg2dxf_exe = dwg2dxf.find(opts)
    if dwg2dxf_exe:
        how = "explicit" if dwg2dxf_exe == opts.dwg2dxf_path else "discovered"
        return ServerConverter("dwg2dxf", how, dwg2dxf_exe, ("dwg2dxf",))
    return ServerConverter("none", "", "")


HEALTH_TTL_SECONDS = 30.0
_HEALTH_CACHE: dict[str, Any] = {"at": 0.0, "value": None}
_HEALTH_LOCK = threading.Lock()


def invalidate_health_cache() -> None:
    """清掉 /api/health 的转换器缓存（测试与运维排查用；平时 30 秒自动过期）。"""
    with _HEALTH_LOCK:
        _HEALTH_CACHE["at"] = 0.0
        _HEALTH_CACHE["value"] = None


def health_info(opts: Optional[Options] = None) -> dict[str, str]:
    """公开健康检查用的转换器状态：只有种类与发现方式，**绝不含路径/环境变量**。

    ``/api/health`` 未登录可读，所以这里只吐 {"converter","how"} 两个枚举字段；
    路径、工具清单、display 之类的排查信息只留在 :func:`converter_status`（日志用）。
    发现只看文件是否可执行（shutil.which / isfile），不起子进程；结果缓存 30 秒，
    免得高频轮询时反复扫描。
    """
    if opts is not None:
        conv = find_converter(opts)
        return {"converter": conv.kind, "how": conv.how if conv.kind != "none" else ""}
    now = time.monotonic()
    with _HEALTH_LOCK:
        cached = _HEALTH_CACHE["value"]
        if cached is not None and (now - float(_HEALTH_CACHE["at"])) < HEALTH_TTL_SECONDS:
            return dict(cached)
    conv = find_converter()
    value = {"converter": conv.kind, "how": conv.how if conv.kind != "none" else ""}
    with _HEALTH_LOCK:
        _HEALTH_CACHE["at"] = now
        _HEALTH_CACHE["value"] = dict(value)
    return value


def converter_status(opts: Optional[Options] = None) -> dict:
    """给日志与运维排查用。含路径，**不要**直接回给未登录的 HTTP（健康检查用 health_info）。"""
    opts = opts or Options.from_env()
    conv = find_converter(opts)
    return {
        "kind": conv.kind,
        "how": conv.how,
        "path": conv.path,
        "tools": list(conv.tools),
        "mode": opts.mode,
        "display": oda.has_display(),
    }


def sanitize_text_backgrounds():
    """文字底纹修复在 cad_render 里；它拖 matplotlib，所以延迟导入。

    真跑转换时用它，测试里传 ``None`` 即可避开整条 matplotlib 依赖链。
    """
    try:
        from .. import cad_render
        return cad_render.sanitize_doc_text_backgrounds
    except Exception:
        log.debug("cad_render unavailable, skip text background sanitize", exc_info=True)
        return None


def _convert_with(
    tool: str,
    src: Path,
    dest: Path,
    opts: Options,
    budget: Budget,
    sanitize: Optional[Callable[[object], int]],
) -> ConversionResult:
    if tool == "oda":
        return oda.convert(src, dest, opts, budget, sanitize=sanitize)
    return dwg2dxf.convert(src, dest, opts, budget, sanitize=sanitize)


def convert_dwg(
    src,
    dest,
    *,
    opts: Optional[Options] = None,
    sanitize: Optional[Callable[[object], int]] = None,
) -> ConversionResult:
    """把 DWG 转成 DXF。成功返回 :class:`ConversionResult`，失败抛 :class:`ConvertError`。

    工具链由 :func:`find_converter` 决定。auto 模式下 ODA 失败会继续试 dwg2dxf；
    ``CAD_DWG_CONVERTER=oda`` 时只用 ODA，不偷偷降级。
    """
    started = time.monotonic()
    src = Path(src)
    dest = Path(dest)
    opts = opts or Options.from_env()
    if sanitize is None:
        sanitize = sanitize_text_backgrounds()

    conv = find_converter(opts)
    if conv.kind == "none":
        log.warning(
            "no DWG converter available (mode=%s, oda_path=%r, dwg2dxf_path=%r, display=%s)",
            opts.mode,
            opts.oda_path,
            opts.dwg2dxf_path,
            oda.has_display(),
        )
        raise ConvertError("no_converter")

    budget = Budget(opts.budget)
    failures: list[tuple[str, str]] = []
    for tool in conv.tools:
        if budget.exhausted:
            failures.append((tool, "超过总预算"))
            break
        try:
            result = _convert_with(tool, src, dest, opts, budget, sanitize)
        except ConvertError as exc:
            log.warning("DWG conversion via %s failed: code=%s detail=%s", tool, exc.code, exc.detail)
            # 链里「没找到」不算尝试失败的原因：它的 detail 是给日志看的，
            # 不能顶替真正跑过并失败的工具输出。
            failures.append((tool, exc.detail if exc.code != "no_dwg2dxf" else ""))
            continue
        result.duration_ms = int((time.monotonic() - started) * 1000)
        if failures:
            tried = "、".join(name for name, _detail in failures)
            result.warnings = ["%s 失败，已改用 %s" % (tried, result.tool), *result.warnings]
        log.info(
            "DWG converted with %s (%s, path=%s) in %dms%s",
            result.tool,
            result.how,
            result.tool_path,
            result.duration_ms,
            (" warnings=" + "; ".join(result.warnings)) if result.warnings else "",
        )
        return result

    detail = next((text for _tool, text in reversed(failures) if text), "")
    raise ConvertError("all_failed", detail or "没有输出")


def find_dwg2dxf() -> Optional[str]:
    """兼容旧名：只找 dwg2dxf，不涉及 ODA。"""
    return dwg2dxf.find(Options.from_env())
