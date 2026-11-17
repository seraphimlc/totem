#!/usr/bin/env python3
"""Phase 2：/api/health 转换器状态、转换结果写进 init.json、DWG 上传前预检。

全部用假转换器 / 临时 CAD_DATA_DIR，不依赖真实 DWG 或 ODA。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CAD_DATA_DIR", "")

from app import config, convert, db, init_proj  # noqa: E402
from app.convert import base, dwg2dxf, oda  # noqa: E402

DXF_BYTES = b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n" * 4
DWG_BYTES = b"\x00" * 512


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""), flush=True)


def _server(kind: str, how: str = "", path: str = "") -> convert.ServerConverter:
    """假转换器；path 故意写成绝对路径，用来检查它不会泄漏到公开接口。"""
    tools = {"oda": ("oda", "dwg2dxf"), "dwg2dxf": ("dwg2dxf",)}.get(kind, ())
    return convert.ServerConverter(kind, how, path or ("/opt/secret/ODAFileConverter" if kind != "none" else ""), tools)


# ---------------------------------------------------------------- a. /api/health


def test_health_dwg_field() -> None:
    from fastapi.testclient import TestClient

    from app import main

    client = TestClient(main.app)
    convert.invalidate_health_cache()
    with mock.patch.object(convert, "find_converter", return_value=_server("oda", "explicit")):
        resp = client.get("/api/health")
    _ok("health-ok", resp.status_code == 200, str(resp.status_code))
    data = resp.json()
    _ok("health-keeps-keys", all(k in data for k in ("ok", "name", "models", "auth_configured", "maas_configured", "decision_configured")), str(sorted(data)))
    dwg = data.get("dwg") or {}
    _ok("health-dwg-kind", dwg.get("converter") == "oda", str(dwg))
    _ok("health-dwg-how", dwg.get("how") == "explicit", str(dwg))
    blob = json.dumps(dwg, ensure_ascii=False)
    _ok("health-no-path", "/" not in blob and "\\" not in blob, blob)
    _ok("health-no-secret", "secret" not in blob.lower() and "ODAFileConverter" not in blob, blob)

    convert.invalidate_health_cache()
    with mock.patch.object(convert, "find_converter", return_value=_server("dwg2dxf", "discovered")):
        data2 = client.get("/api/health").json()
    _ok("health-dwg2dxf", (data2.get("dwg") or {}).get("converter") == "dwg2dxf" and (data2.get("dwg") or {}).get("how") == "discovered", str(data2.get("dwg")))

    convert.invalidate_health_cache()
    with mock.patch.object(convert, "find_converter", return_value=_server("none")):
        data3 = client.get("/api/health").json()
    _ok("health-none", (data3.get("dwg") or {}) == {"converter": "none", "how": ""}, str(data3.get("dwg")))

    # 公开端点：不带 cookie 也能读（另外：查找器结果要缓存，不能每次请求都扫描）
    calls = {"n": 0}
    real = convert.find_converter

    def counting(opts=None):
        calls["n"] += 1
        return real(opts)

    convert.invalidate_health_cache()
    with mock.patch.object(convert, "find_converter", side_effect=counting):
        first = client.get("/api/health").json()
        t0 = time.monotonic()
        second = client.get("/api/health").json()
        elapsed = time.monotonic() - t0
    _ok("health-public", resp.status_code == 200 and "dwg" in first, str(first.get("dwg")))
    _ok("health-cached", calls["n"] == 1 and (second.get("dwg") or {}) == (first.get("dwg") or {}), f"calls={calls['n']}")
    _ok("health-fast", elapsed < 0.5, f"{elapsed * 1000:.0f}ms")
    convert.invalidate_health_cache()

    # 只有种类与发现方式：status() 里的路径绝对不进 payload
    with mock.patch.object(convert, "converter_status", side_effect=AssertionError("health must not call converter_status")):
        with mock.patch.object(convert, "find_converter", return_value=_server("oda", "discovered")):
            convert.invalidate_health_cache()
            payload = convert.health_info()
    _ok("health-info-shape", set(payload) == {"converter", "how"}, str(payload))


# ---------------------------------------------------------------- b. 转换结果写进状态


def test_conversion_recorded_in_status() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sess"
        (root / "source").mkdir(parents=True)
        (root / "source" / "plan.dwg").write_bytes(DWG_BYTES)
        proj = {"id": "s-phase2", "root": str(root), "deliverable": "out/source.dxf"}
        st = {"state": "running", "steps": [{"id": s, "state": "pending"} for s in ("save", "index", "preview")]}
        written: list[dict] = []

        def fake_write(_proj, data):
            written.append(json.loads(json.dumps(data)))

        result = convert.ConversionResult(
            ok=True,
            tool="oda",
            tool_path="/opt/secret/ODAFileConverter",
            how="cli",
            dxfversion="AC1032",
            warnings=["odafc 失败（超时），改用 CLI"],
        )

        def fake_convert(src, dest):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(DXF_BYTES)
            return result

        with mock.patch.object(convert, "convert_dwg", side_effect=fake_convert), \
             mock.patch.object(init_proj, "report_progress"), \
             mock.patch.object(init_proj, "write_status", side_effect=fake_write):
            out = init_proj._ensure_dxf(proj, st)
        _ok("convert-dest", out == root / "out" / "source.dxf" and out.is_file(), str(out))
        rec = st.get("convert") or {}
        _ok("status-convert-recorded", rec.get("tool") == "oda" and rec.get("how") == "cli" and rec.get("dxfversion") == "AC1032", str(rec))
        _ok("status-convert-warnings", isinstance(rec.get("warnings"), list) and rec["warnings"], str(rec.get("warnings")))
        blob = json.dumps(rec, ensure_ascii=False)
        _ok("status-convert-no-path", "/" not in blob and "\\" not in blob, blob)
        _ok("status-convert-was-written", bool(written) and any((w.get("convert") or {}).get("tool") == "oda" for w in written), str(len(written)))
        # 兼容：转换结果不改变既有字段
        _ok("status-keeps-state", st.get("state") == "running" and isinstance(st.get("steps"), list))

        # 公开状态原样透出 convert（前端只读字段，不依赖它存在）
        with mock.patch.object(init_proj, "read_status", return_value=dict(st)):
            pub = init_proj.public_status(proj)
        _ok("public-status-convert", (pub.get("convert") or {}).get("tool") == "oda", str(pub.get("convert")))

        # 兼容：convert_dwg 的老签名/返回值不变，结果通过 out 参数带出
        got: dict = {}
        with mock.patch.object(convert, "convert_dwg", side_effect=fake_convert):
            ret = init_proj.convert_dwg(Path(root) / "source" / "plan.dwg", Path(root) / "out" / "x.dxf", out=got)
        _ok("convert-dwg-returns-path", isinstance(ret, Path) and ret == root / "out" / "x.dxf", str(ret))
        _ok("convert-dwg-out-param", got.get("tool") == "oda" and "tool_path" not in got, str(got))

        # 失败时老文案不变
        with mock.patch.object(convert, "find_converter", return_value=_server("none")):
            try:
                init_proj.convert_dwg(Path(root) / "source" / "plan.dwg", Path(root) / "out" / "y.dxf")
            except ValueError as exc:
                _ok("convert-dwg-no-tool-message", str(exc) == convert.MSG_NO_TOOL, str(exc))
            else:
                raise AssertionError("FAIL convert-dwg-no-tool-message: no exception")


# ---------------------------------------------------------------- c. DWG 上传预检


def _reset_data_dir(tmp: str) -> str:
    prev = config.CAD_DATA_DIR
    db.close()
    config.CAD_DATA_DIR = tmp
    db.close()
    return prev


def test_upload_dwg_without_converter() -> None:
    from fastapi.testclient import TestClient

    from app import auth, main

    with tempfile.TemporaryDirectory() as tmp:
        prev = _reset_data_dir(tmp)
        try:
            base_dir = Path(tmp)
            with mock.patch.object(convert, "find_converter", return_value=_server("none")):
                try:
                    init_proj.create_session_from_upload("plan.dwg", DWG_BYTES, name="无转换器")
                except ValueError as exc:
                    _ok("upload-dwg-no-tool-message", str(exc) == convert.MSG_NO_TOOL, str(exc))
                else:
                    raise AssertionError("FAIL upload-dwg-no-tool-message: no exception")
            sessions = base_dir / "sessions"
            _ok("upload-dwg-no-session-dir", not sessions.exists() or not any(sessions.iterdir()), str(list(sessions.glob('*')) if sessions.exists() else []))
            _ok("upload-dwg-no-sessions-json", not (base_dir / "sessions.json").exists())

            # 走 HTTP：400 + 同一句中文
            prev_user, prev_pw, prev_secret = config.ACCESS_USERNAME, config.ACCESS_PASSWORD, config.SESSION_SECRET
            config.ACCESS_USERNAME, config.ACCESS_PASSWORD, config.SESSION_SECRET = "u", "p", "phase2-secret"
            try:
                client = TestClient(main.app)
                token = auth.issue_session("u")
                client.cookies.set(config.SESSION_COOKIE, token)
                with mock.patch.object(convert, "find_converter", return_value=_server("none")):
                    resp = client.post("/api/projects/upload", files={"file": ("plan.dwg", DWG_BYTES, "application/octet-stream")})
                _ok("upload-http-400", resp.status_code == 400, str(resp.status_code))
                _ok("upload-http-message", (resp.json() or {}).get("detail") == convert.MSG_NO_TOOL, str(resp.json()))
                _ok("upload-http-no-dir", not any(sessions.iterdir()) if sessions.exists() else True)
            finally:
                config.ACCESS_USERNAME, config.ACCESS_PASSWORD, config.SESSION_SECRET = prev_user, prev_pw, prev_secret
        finally:
            db.close()
            config.CAD_DATA_DIR = prev
            db.close()


def test_upload_dxf_unaffected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        prev = _reset_data_dir(tmp)
        try:
            started: list[str] = []
            with mock.patch.object(init_proj, "start", side_effect=lambda proj, **_k: started.append(proj["id"])), \
                 mock.patch.object(convert, "find_converter", return_value=_server("none")):
                proj = init_proj.create_session_from_upload("plan.dxf", DXF_BYTES, name="只有 DXF")
            _ok("upload-dxf-session", bool(proj.get("id")) and Path(proj["root"]).is_dir(), str(proj.get("id")))
            src = list((Path(proj["root"]) / "source").iterdir())
            _ok("upload-dxf-source-written", len(src) == 1 and src[0].read_bytes() == DXF_BYTES, str(src))
            _ok("upload-dxf-start-called", started == [proj["id"]], str(started))
            st = json.loads((Path(proj["root"]) / ".studio" / "init.json").read_text(encoding="utf-8"))
            _ok("upload-dxf-no-convert", "convert" not in st, str(st.get("convert")))
        finally:
            db.close()
            config.CAD_DATA_DIR = prev
            db.close()


def test_ensure_dxf_calls_convert_once() -> None:
    """convert_dwg 只调一次：不能靠捕获 TypeError 来判断签名（真转换里的 TypeError 会被吞）。"""
    counter = {"n": 0}

    def _sess(tmp: str) -> tuple[dict, str]:
        counter["n"] += 1
        root = Path(tmp) / f"sess{counter['n']}"
        (root / "source").mkdir(parents=True)
        (root / "source" / "plan.dwg").write_bytes(DWG_BYTES)
        return {"id": "s-once", "root": str(root), "deliverable": "out/source.dxf"}, str(root)

    with tempfile.TemporaryDirectory() as tmp:
        proj, root = _sess(tmp)
        calls: list[tuple] = []

        def boom(src, dest, *, out=None):
            calls.append((src, dest, out))
            raise TypeError("inside conversion")

        with mock.patch.object(init_proj, "convert_dwg", side_effect=boom), \
             mock.patch.object(init_proj, "report_progress"), \
             mock.patch.object(init_proj, "write_status"), \
             mock.patch.object(init_proj, "_existing_dxf", return_value=None):
            try:
                init_proj._ensure_dxf(proj, {"state": "running"})
            except TypeError as exc:
                _ok("ensure-typeerror-propagates", "inside conversion" in str(exc), str(exc))
            else:
                raise AssertionError("FAIL ensure-typeerror-propagates: TypeError swallowed")
        _ok("ensure-typeerror-once", len(calls) == 1, f"calls={len(calls)}")

        # 新签名：out 出参被用到
        proj2, _ = _sess(tmp)
        got = {}

        def new_sig(src, dest, *, out=None):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(DXF_BYTES)
            if out is not None:
                out.update({"tool": "dwg2dxf", "how": "discovered", "dxfversion": "AC1032", "warnings": []})
            return Path(dest)

        with mock.patch.object(init_proj, "convert_dwg", side_effect=new_sig), \
             mock.patch.object(init_proj, "report_progress"), \
             mock.patch.object(init_proj, "write_status"), \
             mock.patch.object(init_proj, "_existing_dxf", return_value=None):
            st2 = {"state": "running"}
            init_proj._ensure_dxf(proj2, st2)
        _ok("ensure-new-signature", (st2.get("convert") or {}).get("tool") == "dwg2dxf", str(st2.get("convert")))

        # 旧签名 (src, dest)：照常工作，不打 out，状态里不写 convert
        proj3, _ = _sess(tmp)
        old_calls: list[tuple] = []

        def old_sig(src, dest):
            old_calls.append((src, dest))
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(DXF_BYTES)
            return Path(dest)

        with mock.patch.object(init_proj, "convert_dwg", side_effect=old_sig), \
             mock.patch.object(init_proj, "report_progress"), \
             mock.patch.object(init_proj, "write_status"), \
             mock.patch.object(init_proj, "_existing_dxf", return_value=None):
            st3 = {"state": "running"}
            out3 = init_proj._ensure_dxf(proj3, st3)
        _ok("ensure-old-signature-works", len(old_calls) == 1 and out3.is_file(), str(old_calls))
        _ok("ensure-old-signature-no-convert", "convert" not in st3, str(st3.get("convert")))


def test_convert_summary_redacts_paths() -> None:
    """提示里可能带着 ODA/dwg2dxf 的原始报错（含绝对路径）：写进状态前要抹掉。"""
    def _summary(warnings):
        return init_proj.convert_summary(
            convert.ConversionResult(ok=True, tool="oda", how="cli", dxfversion="AC1032", warnings=warnings)
        )

    samples = [
        "/opt/oda/ODAFileConverter failed: rc=1",
        r"C:\Program Files\ODA\x.exe 失败",
        r"\\fileserver\share\draw.dwg 打不开",
        "odafc 失败（/tmp/tmp9k/oda-out/x.dwg: 超时），改用 CLI",
        "写入 /home/cad/out/source.dxf 失败；原目录 C:\\temp\\work 只读",
    ]
    rec = _summary(samples)
    blob = json.dumps(rec, ensure_ascii=False)
    _ok("summary-no-posix-path", not re.search(r"(?<![\w])/[A-Za-z0-9._-]", blob), blob)
    _ok("summary-no-drive-path", ":\\" not in blob.replace("：\\", "：归"), blob)
    _ok("summary-no-unc", not re.search(r"\\\\\w", blob), blob)
    _ok("summary-redacted-marker", sum(w.count("<路径>") for w in rec["warnings"]) >= 5, str(rec["warnings"]))
    joined = " ".join(rec["warnings"])
    for keep in ("odafc 失败", "改用 CLI", "原目录", "写入"):
        _ok(f"summary-keeps-text:{keep}", keep in joined, joined)
    # 评审给的两个例子：路径抹掉、中文保留
    one = _summary(["/opt/oda/ODAFileConverter failed: rc=1"])["warnings"][0]
    _ok("summary-example-posix", one.startswith("<路径>") and "/" not in one, one)
    two = _summary([r"C:\Program Files\ODA\x.exe 失败"])["warnings"][0]
    _ok("summary-example-win", two == "<路径>", two)
    three = _summary(["ODA 失败（C:\\Program Files\\ODA\\x.exe: rc=1），已改用 dwg2dxf"])["warnings"][0]
    _ok(
        "summary-example-mixed",
        "C:\\" not in three and "<路径>" in three and "已改用 dwg2dxf" in three and "rc=1" in three,
        three,
    )
    # 段号失败之类的文字不受影响
    plain = _summary(["ODA 失败，已改用 dwg2dxf", "规范化失败，保留原始转换稿"])
    _ok("summary-plain-untouched", plain["warnings"] == ["ODA 失败，已改用 dwg2dxf", "规范化失败，保留原始转换稿"], str(plain["warnings"]))


def test_converter_kind_ignores_env_noise() -> None:
    """kind 只看 find_converter 的返回，不把路径当键，也不受环境变量拼写影响。"""
    with mock.patch.dict(os.environ, {"CAD_DWG_CONVERTER": "none"}, clear=False):
        _ok("kind-none-from-env", convert.find_converter().kind == "none")
    with mock.patch.object(oda, "find", return_value="/fake/ODAFileConverter"), \
         mock.patch.object(oda, "has_display", return_value=True), \
         mock.patch.object(dwg2dxf, "find", return_value=""):
        conv = convert.find_converter(base.Options(mode="auto"))
        _ok("kind-oda-shape", conv.kind == "oda" and conv.how == "discovered", str(conv.kind))


def main() -> int:
    test_health_dwg_field()
    test_conversion_recorded_in_status()
    test_upload_dwg_without_converter()
    test_upload_dxf_unaffected()
    test_ensure_dxf_calls_convert_once()
    test_convert_summary_redacts_paths()
    test_converter_kind_ignores_env_noise()
    print("ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AssertionError as exc:
        print(str(exc), flush=True)
        raise SystemExit(1)
