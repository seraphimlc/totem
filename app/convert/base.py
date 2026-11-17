"""DWG → DXF 转换的公共部分：结果对象、错误、探测与安全的落盘。

这里刻意不 import ``app.config`` / ``app.cad_render``：配置由调用方经
``Options`` 传入，文字底纹修复经 ``sanitize`` 回调传入。这样 ``app.convert``
可以在任何目录、不装 matplotlib 的情况下单独测试。

设计要点（都是踩过的坑）：

* 子进程的「返回码」不可信：dwg2dxf 可能 exit 0 却打印 ``WRITE ERROR``，ODA
  在 Linux 上则总是以 core dump 结束。唯一可信的判据是**产物文件存在、非空、
  且 ezdxf 打得开并且有实体**。
* 绝不 ``doc.saveas(dest)``：saveas 失败会把正在写的文件写坏/截断。一律先写
  scratch，再 ``atomicio.replace_with_retry()`` 覆盖 dest。
* 超时按「总预算」封顶：一次请求里可能有 odafc、ODA CLI、dwg2dxf 三段子进程，
  各自设满超时会超过 Nginx ``/api/`` 的 300s，请求先被网关掐断。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("cad-studio.convert")

# 与现有 init_proj 行为保持一致：单次工具超时默认 180 秒。
DEFAULT_TOOL_TIMEOUT = 180.0
# 总预算：odafc / ODA CLI / dwg2dxf 合计不超过这个时间。
# Nginx location /api/ 是 proxy_read_timeout 300s，留 30 秒给上传与收尾。
DEFAULT_TOTAL_BUDGET = 270.0
MIN_ATTEMPT_SECONDS = 10.0

@dataclass(frozen=True)
class Options:
    """一次转换的输入参数，全部来自环境变量，由调用方解析后传入。"""

    mode: str = "auto"                  # auto | oda | dwg2dxf | none
    oda_path: str = ""                  # CAD_ODA_PATH
    oda_timeout: float = DEFAULT_TOOL_TIMEOUT
    dwg2dxf_path: str = ""              # CAD_DWG2DXF
    dwg2dxf_timeout: float = DEFAULT_TOOL_TIMEOUT
    tmpdir: str = ""                    # CAD_DWG_CONVERT_TMPDIR
    budget: float = DEFAULT_TOTAL_BUDGET

    @classmethod
    def from_env(cls, environ: Optional[dict] = None) -> "Options":
        env = os.environ if environ is None else environ

        def _num(name: str, default: float) -> float:
            raw = str(env.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = float(raw)
            except ValueError:
                log.warning("ignoring non-numeric %s=%r, using %s", name, raw, default)
                return default
            return value if value > 0 else default

        mode = str(env.get("CAD_DWG_CONVERTER") or "auto").strip().lower()
        if mode not in {"auto", "oda", "dwg2dxf", "none"}:
            log.warning("unknown CAD_DWG_CONVERTER=%r, falling back to auto", mode)
            mode = "auto"
        return cls(
            mode=mode,
            oda_path=str(env.get("CAD_ODA_PATH") or "").strip(),
            oda_timeout=_num("CAD_ODA_TIMEOUT", DEFAULT_TOOL_TIMEOUT),
            dwg2dxf_path=str(env.get("CAD_DWG2DXF") or "").strip(),
            dwg2dxf_timeout=_num("CAD_DWG2DXF_TIMEOUT", DEFAULT_TOOL_TIMEOUT),
            tmpdir=str(env.get("CAD_DWG_CONVERT_TMPDIR") or "").strip(),
            budget=_num("CAD_DWG_CONVERT_BUDGET", DEFAULT_TOTAL_BUDGET),
        )


@dataclass
class ConversionResult:
    """一次转换的全部事实，供日志与（以后的）健康检查使用。"""

    ok: bool
    tool: str = ""                      # oda | dwg2dxf
    tool_path: str = ""
    how: str = ""                       # odafc | cli | explicit | discovered
    output_path: Optional[Path] = None
    dxfversion: str = ""
    error_code: str = ""                # no_converter | no_oda | timeout | empty_output | ...
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0


class ConvertError(Exception):
    """转换失败。``code`` 供日志与测试判断原因，``detail`` 拼进用户文案。"""

    def __init__(self, code: str, detail: str = "", *, tool: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.tool = tool


class Budget:
    """子进程超时的总预算。每次取「剩余预算」与「本工具上限」的较小值。"""

    def __init__(self, total: float):
        self.total = max(float(total), 0.0)
        self.started = time.monotonic()

    def remaining(self) -> float:
        return max(self.total - (time.monotonic() - self.started), 0.0)

    def slice(self, per_tool: float) -> float:
        """本工具还能用多久；返回 0 表示预算耗尽，调用方应放弃。"""
        left = self.remaining()
        if left < MIN_ATTEMPT_SECONDS:
            return 0.0
        return min(float(per_tool) or self.total, left)

    @property
    def exhausted(self) -> bool:
        return self.remaining() < MIN_ATTEMPT_SECONDS


def run(argv: list[str], timeout: float, *, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """跑一个转换器子进程，统一编码与错误处理。超时抛 ConvertError('timeout')。"""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(float(timeout), 1.0),
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ConvertError("timeout", "超过 %.0f 秒" % float(timeout)) from exc
    except OSError as exc:
        raise ConvertError("exec_failed", str(exc)) from exc


def last_line(proc: Optional[subprocess.CompletedProcess]) -> str:
    """失败时给用户看的那一行：优先 stderr 的末行，其次 stdout。"""
    if proc is None:
        return ""
    for stream in (proc.stderr, proc.stdout):
        lines = [ln.strip() for ln in str(stream or "").splitlines() if ln.strip()]
        if lines:
            return lines[-1][:200]
    return ""


def is_executable(path) -> bool:
    try:
        p = Path(path)
        return p.is_file() and os.access(p, os.X_OK)
    except (OSError, TypeError, ValueError):
        return False


def dxf_entity_count(path: Path) -> Optional[int]:
    """能打开就返回模型空间实体数，打不开返回 None。"""
    try:
        import ezdxf
        doc = ezdxf.readfile(str(path))
        return len(doc.modelspace())
    except Exception:
        return None


def read_dxf(path: Path, encoding: str = "utf-8"):
    """读 DXF；指定编码读不出来时再按 ezdxf 默认方式试一次。"""
    import ezdxf
    try:
        return ezdxf.readfile(str(path), encoding=encoding)
    except Exception:
        return ezdxf.readfile(str(path))


def looks_like_dxf(path: Path) -> bool:
    """产物判据：存在、非空、ezdxf 能打开、模型空间有实体。"""
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size <= 0:
            return False
    except OSError:
        return False
    count = dxf_entity_count(p)
    return bool(count) and count > 0


def find_dxf_in(folder: Path, stem: str = "") -> Optional[Path]:
    """在输出目录里找可用的 DXF。扩展名大小写都要认（Windows 文件名不区分大小写）。"""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    hits = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".dxf"]
    if not hits:
        return None
    hits.sort(key=lambda p: (p.stem != stem, p.name.lower()))
    for candidate in hits:
        if looks_like_dxf(candidate):
            return candidate
    return None


def replace_file(src: Path, dest: Path) -> None:
    """用 src 覆盖 dest。Windows 上可能有读者短暂占用，走 atomicio 的重试。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".convert.tmp")
    try:
        shutil.copy2(src, tmp)
        from .. import atomicio
        atomicio.replace_with_retry(tmp, dest)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def normalize_utf8(
    dest: Path,
    *,
    sanitize: Optional[Callable[[object], int]] = None,
) -> Optional[str]:
    """尽可能把 dest 规范成 UTF-8，返回 DXF 版本号（读不出来返回 None）。

    绝不 ``saveas(dest)``：隔离到 scratch 文件，成功了才替换。规范化失败时
    保留原始转换稿——转换结果本身是可用的，只是编码/底纹没修。
    """
    dest = Path(dest)
    scratch = dest.with_name(dest.name + ".normalize.tmp")
    try:
        doc = read_dxf(dest, "utf-8")
        version = str(getattr(doc, "dxfversion", "") or "")
        if sanitize is not None:
            try:
                n = int(sanitize(doc) or 0)
                if n:
                    log.info("sanitized %s corrupt text backgrounds in %s", n, dest)
            except Exception:
                log.exception("could not sanitize text backgrounds in %s", dest)
        doc.saveas(str(scratch), encoding="utf-8")
        from .. import atomicio
        atomicio.replace_with_retry(scratch, dest)
        return version
    except Exception:
        log.exception("could not normalize converted dxf, keeping the raw conversion")
        return None
    finally:
        try:
            if scratch.exists():
                scratch.unlink()
        except OSError:
            pass


def workdir(dest: Path, opts: Options) -> Path:
    """转换中间目录的**父**目录；调用方只在这里面开自己的子目录。

    默认是目标文件旁边的 ``.convert-tmp``（旧的默认位置，会按需创建）。
    ``CAD_DWG_CONVERT_TMPDIR`` 是运维配置的路径：必须已经存在，打错时直接报错，
    而不是悄悄在别处建目录——更不能拿一个猜出来的路径去删。
    """
    if not opts.tmpdir:
        base_dir = Path(dest).parent / ".convert-tmp"
        base_dir.mkdir(parents=True, exist_ok=True)
        return base_dir
    root = Path(opts.tmpdir)
    if not root.is_dir():
        raise ConvertError("bad_tmpdir", "%s 不是已存在的目录" % root)
    return root
