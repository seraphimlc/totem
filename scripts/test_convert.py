#!/usr/bin/env python3
"""DWG → DXF 转换层自测：ODA 优先、dwg2dxf 兜底、环境变量与兼容包装。

不需要真实图纸，也不需要装 ODA / dwg2dxf：子进程与 odafc 全部 mock。
真机上有转换器时额外跑一条端到端用例；没有就 SKIP，并以 0 退出
（与 scripts/test_apply_preview.py 等脚本的约定一致）。

沿用其它脚本的写法：直接 assert，失败抛 AssertionError。
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402

from app import convert, init_proj  # noqa: E402
from app.convert import base, dwg2dxf, oda  # noqa: E402


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""), flush=True)


def _skip(name: str, why: str) -> None:
    print(f"SKIP {name} — {why}", flush=True)


def _fake_proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    """subprocess.CompletedProcess 的最小替身。"""
    return mock.Mock(stdout=stdout, stderr=stderr, returncode=returncode)


def _dxf(path: Path, *, lines: int = 1) -> Path:
    """造一张能打开、模型空间有实体的最小 DXF。"""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for i in range(lines):
        msp.add_line((0, i * 100), (1000, i * 100))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(path))
    return path


def _fake_tool_run(*, oda_fails: bool = True, write_with_flag: str = "-o",
                   stderr: str = "", stdout: str = "", returncode: int = 0):
    """按 argv 模拟 dwg2dxf / ODA 的因果，返回真的 CompletedProcess。

    比 MagicMock 可靠：调用方会读 stdout/stderr/returncode，用真类型才不会被
    未设置的属性静默糊过去（之前正是这里把一个没写过的用例糊成了 PASS）。
    """
    import subprocess

    def _run(argv, timeout, env=None):
        argv = [str(a) for a in argv]
        exe = Path(argv[0]).name.lower()
        if "odafileconverter" in exe:
            if not oda_fails:
                out_dir = Path(argv[2])
                out_dir.mkdir(parents=True, exist_ok=True)
                _dxf(out_dir / "a.dxf")
            return subprocess.CompletedProcess(argv, returncode if oda_fails else 0,
                                               stdout, stderr or "ODA exploded")
        # dwg2dxf：-y -o <文件> <源>  或  -y <源>
        write = write_with_flag and (write_with_flag != "-o" or ("-o" in argv))
        if write:
            target = Path(argv[argv.index("-o") + 1]) if "-o" in argv else Path(argv[-1]).with_suffix(".dxf")
            _dxf(target)
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    return _run


def _touch_exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _no_env(**overrides) -> dict:
    """干净的环境：不带任何 CAD_* 变量。"""
    return {**overrides}


def _opts(**kw) -> base.Options:
    return base.Options(**kw)


# ---------------------------------------------------------------- 1. 纯逻辑


def test_result_and_suffix() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        upper = _dxf(td / "UPPER.DXF", lines=2)
        _ok("looks-like-dxf-upper", base.looks_like_dxf(upper), upper.name)

        empty = td / "empty.dxf"
        empty.write_bytes(b"")
        _ok("looks-like-dxf-empty", not base.looks_like_dxf(empty))

        junk = td / "junk.dxf"
        junk.write_text("this is not a dxf\n" * 20, encoding="utf-8")
        _ok("looks-like-dxf-junk", not base.looks_like_dxf(junk))

        nodata = ezdxf.new("R2010")
        nodata.saveas(str(td / "noents.dxf"))
        _ok("looks-like-dxf-no-entities", not base.looks_like_dxf(td / "noents.dxf"))

        res = base.ConversionResult(ok=True, tool="oda", how="odafc")
        for name in ("ok", "tool", "tool_path", "how", "output_path", "dxfversion",
                     "error_code", "error", "warnings", "duration_ms"):
            _ok(f"result-field-{name}", hasattr(res, name))


def test_options_from_env() -> None:
    opts = base.Options.from_env(_no_env(CAD_DWG_CONVERTER="ODA", CAD_ODA_PATH="/x/oda",
                                         CAD_ODA_TIMEOUT="30", CAD_DWG2DXF="/x/d2d",
                                         CAD_DWG2DXF_TIMEOUT="45", CAD_DWG_CONVERT_TMPDIR="/x/tmp",
                                         CAD_DWG_CONVERT_BUDGET="99"))
    _ok("env-mode-lowercase", opts.mode == "oda", opts.mode)
    _ok("env-paths", (opts.oda_path, opts.dwg2dxf_path) == ("/x/oda", "/x/d2d"))
    _ok("env-timeouts", (opts.oda_timeout, opts.dwg2dxf_timeout) == (30.0, 45.0))
    _ok("env-tmpdir-budget", (opts.tmpdir, opts.budget) == ("/x/tmp", 99.0))

    dflt = base.Options.from_env(_no_env())
    _ok("env-defaults", (dflt.mode, dflt.oda_timeout, dflt.dwg2dxf_timeout, dflt.budget)
        == ("auto", 180.0, 180.0, 270.0))

    bad = base.Options.from_env(_no_env(CAD_DWG_CONVERTER="tinycad", CAD_ODA_TIMEOUT="abc",
                                        CAD_DWG_CONVERT_BUDGET="-3"))
    _ok("env-bad-mode-to-auto", bad.mode == "auto")
    _ok("env-bad-numbers-default", (bad.oda_timeout, bad.budget) == (180.0, 270.0))


def test_budget() -> None:
    b = base.Budget(270)
    _ok("budget-slice-caps-by-tool", b.slice(180) == 180.0)
    small = base.Budget(60)
    _ok("budget-slice-caps-by-total", 0 < small.slice(180) <= 60.0, str(small.slice(180)))
    spent = base.Budget(0)
    _ok("budget-exhausted", spent.slice(180) == 0.0 and spent.exhausted)


# ---------------------------------------------------------------- 2. 发现顺序


def test_discovery_order() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        exe = _touch_exe(td / "ODAFileConverter")

        with mock.patch.object(oda, "find", return_value=str(exe)), \
             mock.patch.object(oda, "has_display", return_value=True), \
             mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"):
            conv = convert.find_converter(_opts(mode="auto"))
            _ok("discover-auto-oda-first", conv.kind == "oda", str(conv))
            _ok("discover-auto-chain", conv.tools == ("oda", "dwg2dxf"), str(conv.tools))

            conv = convert.find_converter(_opts(mode="oda", oda_path=str(exe)))
            _ok("discover-oda-only", conv.kind == "oda" and conv.tools == ("oda",), str(conv))
            _ok("discover-oda-explicit", conv.how == "explicit", conv.how)

            conv = convert.find_converter(_opts(mode="dwg2dxf"))
            _ok("discover-dwg2dxf-only", conv.kind == "dwg2dxf" and conv.tools == ("dwg2dxf",), str(conv))

            conv = convert.find_converter(_opts(mode="none"))
            _ok("discover-none", conv.kind == "none" and conv.tools == (), str(conv))

        # 强制 oda 但机器上没有 ODA：不该偷偷换成 dwg2dxf。
        with mock.patch.object(oda, "find", return_value=None), \
             mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"):
            conv = convert.find_converter(_opts(mode="oda"))
            _ok("discover-oda-missing-is-none", conv.kind == "none", str(conv))

        # auto 模式下 ODA 没有显示（headless 无 xvfb）：退到 dwg2dxf。
        with mock.patch.object(oda, "find", return_value=str(exe)), \
             mock.patch.object(oda, "has_display", return_value=False), \
             mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"):
            conv = convert.find_converter(_opts(mode="auto"))
            _ok("discover-no-display-falls-back", conv.kind == "dwg2dxf", str(conv))

        # 什么都没有。
        with mock.patch.object(oda, "find", return_value=None), \
             mock.patch.object(dwg2dxf, "find", return_value=None):
            conv = convert.find_converter(_opts(mode="auto"))
            _ok("discover-nothing-is-none", conv.kind == "none", str(conv))
            status = convert.converter_status(_opts(mode="auto"))
            _ok("status-none-kind", status["kind"] == "none" and status["path"] == "", str(status))


# ---------------------------------------------------------------- 3/4. ODA


def test_oda_odafc_success() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "a.dwg"
        src.write_bytes(b"\x00" * 256)
        dest = td / "out" / "source.dxf"

        def fake_readfile(path):
            doc = ezdxf.new("R2010")
            doc.modelspace().add_line((0, 0), (10, 0))
            return doc

        fake_odafc = mock.Mock(is_installed=lambda: True, readfile=fake_readfile)
        with mock.patch.dict(sys.modules, {"ezdxf.addons.odafc": fake_odafc}), \
             mock.patch.object(oda, "find", return_value=str(_touch_exe(td / "ODAFileConverter"))), \
             mock.patch.object(oda, "has_display", return_value=True):
            result = oda.convert(src, dest, _opts(mode="oda"), base.Budget(60), sanitize=None)
        _ok("oda-odafc-ok", result.ok and result.how == "odafc", str(result))
        _ok("oda-odafc-dest-written", dest.is_file() and base.looks_like_dxf(dest))
        _ok("oda-odafc-version", result.dxfversion.startswith("AC"), result.dxfversion)


def test_oda_odafc_failure_falls_to_cli() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "a.dwg"
        src.write_bytes(b"\x00" * 256)
        dest = td / "out" / "source.dxf"
        exe = _touch_exe(td / "ODAFileConverter")

        calls: list[list[str]] = []

        def fake_run(argv, timeout, env=None):
            calls.append(list(argv))
            # CLI 形态：[exe, in, out, ver, type, recurse, audit, filter]
            out_dir = Path(argv[2])
            # ODA 在 Linux 上总以 core dump 收尾：返回码非 0 也要看产物。
            _dxf(out_dir / "a.dxf", lines=3)
            return _fake_proc(stderr="Quit (core dumped)", returncode=139)

        failing = mock.Mock(is_installed=lambda: True,
                             readfile=mock.Mock(side_effect=RuntimeError("ODAFileConverter failed: rc=1")))
        with mock.patch.dict(sys.modules, {"ezdxf.addons.odafc": failing}), \
             mock.patch.object(oda, "find", return_value=str(exe)), \
             mock.patch.object(oda, "has_display", return_value=True), \
             mock.patch.object(base, "run", side_effect=fake_run), \
             mock.patch("shutil.which", return_value=None):
            result = oda.convert(src, dest, _opts(mode="oda"), base.Budget(60), sanitize=None)

        _ok("oda-cli-used-after-odafc-fail", result.how == "cli", str(result))
        _ok("oda-cli-argv", calls and calls[0][3:7] == ["ACAD2018", "DXF", "0", "0"], str(calls[:1]))
        _ok("oda-cli-verdict-from-artefact", dest.is_file() and base.looks_like_dxf(dest))
        _ok("oda-cli-warns-odafc", any("odafc" in w for w in result.warnings), str(result.warnings))


def test_oda_cli_copies_uppercase_dwg() -> None:
    """ODA 的官方过滤器是 *.DWG（Linux 上大小写敏感），拷进输入目录的必须是大写后缀。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "plan.dwg"          # 用户上传几乎都是小写
        src.write_bytes(b"\x00" * 256)
        dest = td / "out" / "source.dxf"
        exe = _touch_exe(td / "ODAFileConverter")
        seen: dict = {}

        def fake_run(argv, timeout, env=None):
            in_dir = Path(argv[1])
            seen["argv"] = list(argv)
            seen["inputs"] = [p.name for p in in_dir.iterdir()]
            _dxf(Path(argv[2]) / (Path(seen["inputs"][0]).stem + ".dxf"))
            return _fake_proc(returncode=139, stderr="Quit (core dumped)")

        with mock.patch.object(oda, "find", return_value=str(exe)), \
             mock.patch.object(oda, "has_display", return_value=True), \
             mock.patch.object(oda, "_via_odafc", side_effect=base.ConvertError("odafc_unavailable", "x")), \
             mock.patch.object(base, "run", side_effect=fake_run), \
             mock.patch("shutil.which", return_value=None):
            result = oda.convert(src, dest, _opts(mode="oda"), base.Budget(60), sanitize=None)

        _ok("oda-cli-upper-input", seen.get("inputs") == ["plan.DWG"], str(seen.get("inputs")))
        _ok("oda-cli-filter", seen.get("argv", [None] * 8)[-1] == "*.DWG", str(seen.get("argv")))
        _ok("oda-cli-upper-converted", result.how == "cli" and base.looks_like_dxf(dest), str(result))

        names = {name: oda._oda_input_name(Path(name)) for name in
                 ("PLAN.DWG", "楼层平面-1F.dwg", ".hidden.dwg", "a*b?.dwg", "very.dotted.name.dwg")}
        _ok("oda-input-name-already-upper", names["PLAN.DWG"] == "PLAN.DWG")
        _ok("oda-input-name-cjk", names["楼层平面-1F.dwg"] == "楼层平面-1F.DWG")
        _ok("oda-input-name-hidden", names[".hidden.dwg"] == "source.DWG", names[".hidden.dwg"])
        _ok("oda-input-name-glob", names["a*b?.dwg"] == "a_b_.DWG", names["a*b?.dwg"])
        _ok("oda-input-name-dots", names["very.dotted.name.dwg"] == "very.dotted.name.DWG")


def test_has_display_variants() -> None:
    """Xvfb 二进制本身也算有显示：ezdxf 的 odafc 是直接起 Xvfb 的，不走 xvfb-run。

    只 ``apt install xvfb`` 的服务器（有 /usr/bin/Xvfb、没有 DISPLAY）不应被跳过。
    """
    def check(which_names, display, expected, label, platform="linux"):
        # clear=True：默认没有任何 DISPLAY；需要时再显式放进去。
        env = {"DISPLAY": display} if display is not None else {}
        found = lambda cmd: ("/usr/bin/" + cmd) if cmd in set(which_names) else None
        with mock.patch.object(oda, "shutil") as fake_shutil, \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(oda.sys, "platform", platform):
            fake_shutil.which.side_effect = found
            got = oda.has_display()
            mode = oda.virtual_display()
        _ok(label, got is expected, f"has_display={got} virtual_display={mode!r}")

    check({"Xvfb"}, None, True, "display-xvfb-only")
    check({"xvfb-run"}, None, True, "display-xvfb-run")
    check(set(), ":0", True, "display-env")
    check({"Xvfb", "xvfb-run"}, None, True, "display-both")
    check(set(), None, False, "display-none")
    check(set(), None, True, "display-windows-always", platform="win32")
    check(set(), None, True, "display-darwin-always", platform="darwin")

    def mode_for(which_names, display):
        env = {"DISPLAY": display} if display is not None else {}
        found = lambda cmd: ("/usr/bin/" + cmd) if cmd in set(which_names) else None
        with mock.patch.object(oda, "shutil") as fake_shutil, \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(oda.sys, "platform", "linux"):
            fake_shutil.which.side_effect = found
            return oda.virtual_display()

    # CLI 前缀只给 xvfb-run 加；光有 Xvfb 时不加前缀（odafc 自己会起 Xvfb）。
    _ok("display-mode-xvfb-run", mode_for({"xvfb-run", "Xvfb"}, None) == "xvfb-run")
    _ok("display-mode-xvfb-only", mode_for({"Xvfb"}, None) == "Xvfb")
    _ok("display-mode-env", mode_for(set(), ":0") == "DISPLAY")
    _ok("display-mode-none", mode_for(set(), None) == "")


def test_oda_cli_empty_output_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "a.dwg"
        src.write_bytes(b"\x00" * 256)
        dest = td / "out" / "source.dxf"
        exe = _touch_exe(td / "ODAFileConverter")

        with mock.patch.object(oda, "find", return_value=str(exe)), \
             mock.patch.object(oda, "has_display", return_value=True), \
             mock.patch.object(oda, "_via_odafc", side_effect=base.ConvertError("odafc_unavailable", "no oda")), \
             mock.patch.object(base, "run", return_value=_fake_proc(returncode=0)):
            try:
                oda.convert(src, dest, _opts(mode="oda"), base.Budget(60), sanitize=None)
            except base.ConvertError as exc:
                _ok("oda-empty-output-raises", exc.code == "empty_output", str(exc))
            else:
                raise AssertionError("FAIL oda-empty-output-raises: no exception")

        leftover = list((td / "out").glob("oda-*")) if (td / "out").is_dir() else []
        _ok("oda-cleanup", not leftover, str(leftover))


def test_oda_headless_no_display() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "a.dwg"
        src.write_bytes(b"\x00" * 64)
        exe = _touch_exe(td / "ODAFileConverter")
        with mock.patch.object(oda, "find", return_value=str(exe)), \
             mock.patch.object(oda, "has_display", return_value=False):
            try:
                oda.convert(src, td / "o.dxf", _opts(mode="oda"), base.Budget(60), sanitize=None)
            except base.ConvertError as exc:
                _ok("oda-headless-code", exc.code == "no_display", str(exc))
            else:
                raise AssertionError("FAIL oda-headless-code")


# ---------------------------------------------------------------- 5. dwg2dxf


def test_dwg2dxf_two_attempts() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "plan.dwg"
        src.write_bytes(b"\x00" * 256)
        dest = td / "out" / "source.dxf"
        seen: list[list[str]] = []

        def fake_run(argv, timeout, env=None):
            seen.append(list(argv))
            # 第一次 -y -o 不产出；第二次只认输入路径，写在源文件旁边。
            if "-o" not in argv:
                _dxf(Path(argv[-1]).with_suffix(".dxf"), lines=2)
            return _fake_proc(returncode=0)

        with mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(base, "run", side_effect=fake_run):
            result = dwg2dxf.convert(src, dest, _opts(mode="dwg2dxf"), base.Budget(60), sanitize=None)

        _ok("dwg2dxf-tries-both", len(seen) == 2 and "-o" in seen[0] and "-o" not in seen[1], str(seen))
        _ok("dwg2dxf-o-takes-file", seen[0][-1].endswith("plan.dwg") and seen[0][-2].endswith("source.dxf"),
            str(seen[0]))
        _ok("dwg2dxf-second-attempt-ok", result.ok and result.tool == "dwg2dxf", str(result))
        _ok("dwg2dxf-dest-ok", dest.is_file() and base.looks_like_dxf(dest))
        _ok("dwg2dxf-cleanup", not (td / "out" / ".convert-tmp").exists())


def test_dwg2dxf_exit0_without_artefact_is_failure() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "bad.dwg"
        src.write_bytes(b"\x00" * 64)
        dest = td / "out" / "source.dxf"
        with mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(base, "run", return_value=_fake_proc(stdout="WRITE ERROR: broken\n", returncode=0)):
            try:
                dwg2dxf.convert(src, dest, _opts(mode="dwg2dxf"), base.Budget(60), sanitize=None)
            except base.ConvertError as exc:
                _ok("dwg2dxf-exit0-no-artefact", exc.code == "empty_output", str(exc))
                _ok("dwg2dxf-hint-from-stderr", "WRITE ERROR" in exc.detail, exc.detail)
            else:
                raise AssertionError("FAIL dwg2dxf-exit0-no-artefact")
        _ok("dwg2dxf-no-dest-on-failure", not dest.exists())


def _plant_user_tmpdir(td: Path) -> Path:
    """模拟被 CAD_DWG_CONVERT_TMPDIR 指向的目录：里面有别人的文件和子目录。"""
    user_tmp = td / "user-tmp"
    user_tmp.mkdir()
    (user_tmp / "precious.txt").write_text("do not delete", encoding="utf-8")
    sibling = user_tmp / "other-project"
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")
    return user_tmp


def _assert_user_tmpdir_intact(user_tmp: Path, label: str) -> None:
    _ok(f"{label}-precious-survives", (user_tmp / "precious.txt").is_file()
        and (user_tmp / "precious.txt").read_text(encoding="utf-8") == "do not delete")
    sibling = user_tmp / "other-project"
    _ok(f"{label}-sibling-dir-survives", sibling.is_dir() and (sibling / "keep.txt").is_file())
    leftovers = [p.name for p in user_tmp.iterdir() if p.name not in
                 {"precious.txt", "other-project"}]
    _ok(f"{label}-no-leftovers", not leftovers, str(leftovers))


def test_user_tmpdir_survives_failed_conversion() -> None:
    """CAD_DWG_CONVERT_TMPDIR 是用户的目录：转换失败时也不能删/清空它。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        user_tmp = _plant_user_tmpdir(td)
        src = td / "plan.dwg"
        src.write_bytes(b"\x00" * 64)
        opts = _opts(mode="dwg2dxf", tmpdir=str(user_tmp))
        with mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(base, "run", side_effect=_fake_tool_run(write_with_flag="",
                                                                       stdout="WRITE ERROR: bad\n")):
            try:
                dwg2dxf.convert(src, td / "out" / "source.dxf", opts, base.Budget(60), sanitize=None)
            except base.ConvertError as exc:
                _ok("user-tmpdir-fail-conversion-fails", exc.code == "empty_output", str(exc))
            else:
                raise AssertionError("FAIL user-tmpdir-fail-conversion-fails: no exception")
        _assert_user_tmpdir_intact(user_tmp, "user-tmpdir-fail")
        _ok("user-tmpdir-fail-dir-kept", user_tmp.is_dir())


def test_user_tmpdir_survives_successful_conversion() -> None:
    """成功时同样：只在用户目录下开唯一子目录，用完只删子目录。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        user_tmp = _plant_user_tmpdir(td)
        src = td / "plan.dwg"
        src.write_bytes(b"\x00" * 64)
        dest = td / "out" / "source.dxf"
        opts = _opts(mode="dwg2dxf", tmpdir=str(user_tmp))
        with mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(base, "run", side_effect=_fake_tool_run()):
            result = dwg2dxf.convert(src, dest, opts, base.Budget(60), sanitize=None)
        _ok("user-tmpdir-ok-converted", result.ok and base.looks_like_dxf(dest), str(result))
        _assert_user_tmpdir_intact(user_tmp, "user-tmpdir-ok")


def test_user_tmpdir_must_exist() -> None:
    """路径打错时直接报错，而不是悄悄在别处建目录或清掉它。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        missing = td / "typo-dir"
        src = td / "plan.dwg"
        src.write_bytes(b"\x00" * 64)
        opts = _opts(mode="dwg2dxf", tmpdir=str(missing))
        with mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(base, "run", side_effect=_fake_tool_run()):
            try:
                dwg2dxf.convert(src, td / "out" / "source.dxf", opts, base.Budget(60), sanitize=None)
            except base.ConvertError as exc:
                _ok("user-tmpdir-missing-rejected", exc.code == "bad_tmpdir", str(exc))
            else:
                raise AssertionError("FAIL user-tmpdir-missing-rejected: no exception")
        _ok("user-tmpdir-missing-not-created", not missing.exists())


def test_default_convert_tmp_is_cleared() -> None:
    """默认的 .convert-tmp 是我们的目录，旧行为保留：用它之前先清空。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out = td / "out"
        stale = out / ".convert-tmp" / "stale-from-last-run.dxf"
        _dxf(stale)
        src = td / "plan.dwg"
        src.write_bytes(b"\x00" * 64)
        with mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(base, "run", side_effect=_fake_tool_run()):
            dwg2dxf.convert(src, out / "source.dxf", _opts(mode="dwg2dxf"), base.Budget(60), sanitize=None)
        _ok("default-tmp-stale-cleared", not stale.exists(), str(stale))
        _ok("default-tmp-removed", not (out / ".convert-tmp").exists())


def test_chain_falls_back_to_dwg2dxf() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "a.dwg"
        src.write_bytes(b"\x00" * 256)
        dest = td / "out" / "source.dxf"
        exe = _touch_exe(td / "ODAFileConverter")

        def fake_run(argv, timeout, env=None):
            if "ODAFileConverter" in str(argv[0]):
                return _fake_proc(returncode=1, stderr="ODA exploded")
            out = Path(argv[argv.index("-o") + 1])
            _dxf(out, lines=1)
            return _fake_proc(returncode=0)

        with mock.patch.object(oda, "find", return_value=str(exe)), \
             mock.patch.object(oda, "has_display", return_value=True), \
             mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(oda, "_via_odafc", side_effect=base.ConvertError("odafc_unavailable", "x")), \
             mock.patch.object(base, "run", side_effect=fake_run), \
             mock.patch("shutil.which", return_value=None):
            result = convert.convert_dwg(src, dest, opts=_opts(mode="auto"), sanitize=None)

        _ok("chain-used-dwg2dxf", result.tool == "dwg2dxf", str(result))
        _ok("chain-warns-fallback", any("dwg2dxf" in w for w in result.warnings), str(result.warnings))
        _ok("chain-dest-ok", dest.is_file() and base.looks_like_dxf(dest))


def test_forced_oda_does_not_downgrade() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "a.dwg"
        src.write_bytes(b"\x00" * 256)
        exe = _touch_exe(td / "ODAFileConverter")
        used = {"dwg2dxf": 0}

        def boom(*a, **k):
            used["dwg2dxf"] += 1
            raise AssertionError("dwg2dxf must not run when CAD_DWG_CONVERTER=oda")

        with mock.patch.object(oda, "find", return_value=str(exe)), \
             mock.patch.object(oda, "has_display", return_value=True), \
             mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(dwg2dxf, "convert", side_effect=boom), \
             mock.patch.object(oda, "_via_odafc", side_effect=base.ConvertError("odafc_unavailable", "x")), \
             mock.patch.object(base, "run", return_value=_fake_proc(returncode=1)), \
             mock.patch("shutil.which", return_value=None):
            try:
                convert.convert_dwg(src, td / "o.dxf", opts=_opts(mode="oda"), sanitize=None)
            except base.ConvertError as exc:
                _ok("forced-oda-all-failed", exc.code == "all_failed", str(exc))
            else:
                raise AssertionError("FAIL forced-oda-all-failed")
        _ok("forced-oda-no-dwg2dxf", used["dwg2dxf"] == 0)


def test_normalize_never_saveas_onto_dest() -> None:
    """规范化必须先写 scratch：saveas 失败时 dest 保持原样，不被截断/写坏。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        dest = _dxf(td / "source.dxf", lines=2)
        before = dest.read_bytes()

        def boom(*a, **k):
            raise RuntimeError("saveas exploded")

        with mock.patch.object(base, "read_dxf", side_effect=boom):
            version = base.normalize_utf8(dest, sanitize=None)
        _ok("normalize-failure-keeps-dest", dest.read_bytes() == before and base.looks_like_dxf(dest))
        _ok("normalize-failure-returns-none", version is None, str(version))
        _ok("normalize-no-scratch-left", not list(td.glob("*.normalize.tmp")))

        calls = {"n": 0}

        def counting(doc):
            calls["n"] += 1
            return 1

        version = base.normalize_utf8(dest, sanitize=counting)
        _ok("normalize-ok-version", bool(version) and version.startswith("AC"), str(version))
        _ok("normalize-sanitize-called", calls["n"] == 1)
        _ok("normalize-ok-dest-readable", base.looks_like_dxf(dest))


# ---------------------------------------------------------------- 6. 真机端到端


def test_real_tool_smoke() -> None:
    conv = convert.find_converter(_opts(mode="auto"))
    if conv.kind == "none":
        _skip("real-tool-fake-dwg", "本机没有 ODA / dwg2dxf")
        return
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "fake.dwg"
        src.write_bytes(b"not a real dwg" * 8)
        dest = td / "out" / "source.dxf"
        try:
            convert.convert_dwg(src, dest, opts=_opts(mode="auto"), sanitize=None)
        except convert.ConvertError as exc:
            _ok("real-tool-fake-dwg-fails", exc.code in {"all_failed", "empty_output", "timeout"}, str(exc))
            _ok("real-tool-no-dest", not dest.exists() or not base.looks_like_dxf(dest))
        else:
            _skip("real-tool-fake-dwg", "转换器接受了假 DWG，跳过")


# ---------------------------------------------------------------- 7. 兼容包装


def test_compat_wrappers() -> None:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "a.dwg"
        src.write_bytes(b"\x00" * 256)
        dest = td / "out" / "source.dxf"

        # 没有转换器：原样文案。
        with mock.patch.object(convert, "find_converter",
                               return_value=convert.ServerConverter("none", "", "")):
            try:
                init_proj.convert_dwg(src, dest)
            except ValueError as exc:
                _ok("compat-no-tool-message", str(exc) == convert.MSG_NO_TOOL, str(exc))
            else:
                raise AssertionError("FAIL compat-no-tool-message: no exception")

        # 转换失败：文案带上原因。
        with mock.patch.object(convert, "find_converter",
                               return_value=convert.ServerConverter("dwg2dxf", "discovered",
                                                                   "/fake/dwg2dxf", ("dwg2dxf",))), \
             mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(base, "run", side_effect=_fake_tool_run(
                 write_with_flag="", stdout="WRITE ERROR: bad input\n")):
            try:
                init_proj.convert_dwg(src, dest)
            except ValueError as exc:
                _ok("compat-failed-message", str(exc) == convert.MSG_FAILED % "WRITE ERROR: bad input", str(exc))
            else:
                raise AssertionError("FAIL compat-failed-message: no exception")

        # 成功：返回 dest。
        with mock.patch.object(convert, "find_converter",
                               return_value=convert.ServerConverter("dwg2dxf", "discovered",
                                                                   "/fake/dwg2dxf", ("dwg2dxf",))), \
             mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"), \
             mock.patch.object(base, "run", side_effect=_fake_tool_run()):
            out = init_proj.convert_dwg(src, dest)
        _ok("compat-returns-path", isinstance(out, Path) and out == dest and dest.is_file(), str(out))

        # 旧名仍然可用。
        with mock.patch.object(dwg2dxf, "find", return_value="/fake/dwg2dxf"):
            _ok("compat-find-dwg2dxf", init_proj.find_dwg2dxf() == "/fake/dwg2dxf")
            _ok("compat-windows-candidates", isinstance(init_proj._windows_dwg2dxf_candidates(), list))
            _ok("compat-which-where-posix", init_proj._which_where("definitely-not-a-tool-xyz") == "")


def test_ensure_dxf_still_calls_convert_dwg() -> None:
    """_ensure_dxf() 的调用方式不变：上传初始化不需要改。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "sess"
        (root / "source").mkdir(parents=True)
        (root / "source" / "plan.dwg").write_bytes(b"\x00" * 64)
        proj = {"id": "s-test", "root": str(root), "deliverable": "out/source.dxf"}
        seen = {}

        def fake_convert(src, dest):
            seen["src"], seen["dest"] = Path(src), Path(dest)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_text("0\nEOF\n", encoding="utf-8")
            return Path(dest)

        with mock.patch.object(init_proj, "convert_dwg", side_effect=fake_convert), \
             mock.patch.object(init_proj, "report_progress"), \
             mock.patch.object(init_proj, "_existing_dxf", return_value=None):
            out = init_proj._ensure_dxf(proj, {"state": "running"})
        _ok("ensure-dxf-src", seen.get("src") == root / "source" / "plan.dwg", str(seen))
        _ok("ensure-dxf-dest", seen.get("dest") == root / "out" / "source.dxf", str(seen))
        _ok("ensure-dxf-returns-dest", out == root / "out" / "source.dxf", str(out))


def main() -> int:
    test_result_and_suffix()
    test_options_from_env()
    test_budget()
    test_discovery_order()
    test_oda_odafc_success()
    test_oda_odafc_failure_falls_to_cli()
    test_oda_cli_copies_uppercase_dwg()
    test_has_display_variants()
    test_oda_cli_empty_output_fails()
    test_oda_headless_no_display()
    test_dwg2dxf_two_attempts()
    test_dwg2dxf_exit0_without_artefact_is_failure()
    test_user_tmpdir_survives_failed_conversion()
    test_user_tmpdir_survives_successful_conversion()
    test_user_tmpdir_must_exist()
    test_default_convert_tmp_is_cleared()
    test_chain_falls_back_to_dwg2dxf()
    test_forced_oda_does_not_downgrade()
    test_normalize_never_saveas_onto_dest()
    test_real_tool_smoke()
    test_compat_wrappers()
    test_ensure_dxf_still_calls_convert_dwg()
    print("ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("CAD_DATA_DIR", "")
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(str(exc), flush=True)
        raise SystemExit(1)
