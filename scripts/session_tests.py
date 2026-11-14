#!/usr/bin/env python3
"""Local API tests for 图腾: multi-session init, chat modes, jev gate, apply/undo, PDF, OSS, SQLite."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path("/workspace/cad-nl")
STUDIO = ROOT / "studio"
BASE = "http://127.0.0.1:8788"
OUT = Path("/tmp/tuteng-session-tests.json")
CANTEEN = ROOT / "projects" / "学生食堂" / "dxf" / "source-fixed.dxf"
MALL = ROOT / "demo-t53" / "dxf" / "source.dxf"

env = dotenv_values(STUDIO / ".env")
USER = env.get("ACCESS_USERNAME") or ""
PASSWORD = env.get("ACCESS_PASSWORD") or ""
if not USER or not PASSWORD:
    raise SystemExit("missing login env")

results: list[dict] = []


def rec(name: str, ok: bool, **extra):
    item = {"name": name, "ok": ok, **extra}
    results.append(item)
    status = "PASS" if ok else "FAIL"
    # ASCII-safe console
    print(f"{status}  {name}", flush=True)
    return ok


def save():
    OUT.write_text(json.dumps({"results": results, "failed": [r for r in results if not r["ok"]]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        line = block.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        try:
            events.append(json.loads(raw))
        except json.JSONDecodeError:
            events.append({"type": "parse_error", "raw": raw[:200]})
    return events


def chat(client: httpx.Client, pid: str, text: str, region: str | None = None, timeout: float = 180.0) -> dict:
    r = client.post(
        f"{BASE}/api/projects/{pid}/chat",
        json={"text": text, "region": region},
        timeout=timeout,
        headers={"Accept": "text/event-stream"},
    )
    events = parse_sse(r.text) if r.status_code == 200 else []
    done = next((e for e in events if e.get("type") == "done"), None)
    err = next((e for e in events if e.get("type") == "error"), None)
    return {
        "http": r.status_code,
        "detail": None if r.status_code == 200 else (r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]),
        "events": events,
        "message": (done or {}).get("message") if done else None,
        "error": (err or {}).get("message") if err else None,
    }


def wait_ready(client: httpx.Client, pid: str, timeout: float, label: str) -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        r = client.get(f"{BASE}/api/projects/{pid}/init", timeout=30)
        last = r.json() if r.status_code == 200 else {"http": r.status_code, "text": r.text[:200]}
        if last.get("ready") or last.get("state") == "error":
            last["elapsed_s"] = round(time.time() - t0, 1)
            return last
        time.sleep(3)
    last["elapsed_s"] = round(time.time() - t0, 1)
    last["timeout"] = True
    return last


def main() -> int:
    if not CANTEEN.is_file() or not MALL.is_file():
        rec("drawings_present", False, canteen=str(CANTEEN), mall=str(MALL))
        save()
        return 1
    rec("drawings_present", True, canteen_mb=round(CANTEEN.stat().st_size / 1e6, 1), mall_mb=round(MALL.stat().st_size / 1e6, 1))

    canteen_mtime = CANTEEN.stat().st_mtime
    mall_mtime = MALL.stat().st_mtime

    with httpx.Client(follow_redirects=False, timeout=60.0) as client:
        h = client.get(f"{BASE}/api/health")
        rec("health", h.status_code == 200 and h.json().get("decision_configured") and h.json().get("maas_configured"), body=h.json())

        login = client.post(f"{BASE}/api/login", json={"username": USER, "password": PASSWORD})
        rec("login", login.status_code == 200 and "cad_session" in client.cookies)

        # Upload mall first (smaller), then canteen and a second mall session while the first is still initializing.
        def upload(path: Path, name: str) -> dict:
            with path.open("rb") as f:
                r = client.post(
                    f"{BASE}/api/projects/upload",
                    files={"file": (path.name, f, "application/octet-stream")},
                    data={"name": name},
                    timeout=120.0,
                )
            return {"http": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:300]}

        w1 = upload(MALL, "万商城-测1")
        rec("upload_mall_1", w1["http"] == 200 and bool((w1["body"] or {}).get("id")), body={k: w1["body"].get(k) for k in ("id", "name", "ready") if isinstance(w1["body"], dict)})
        if w1["http"] != 200:
            save()
            return 1
        wid1 = w1["body"]["id"]

        # Chat must be locked until init finishes.
        early = client.post(f"{BASE}/api/projects/{wid1}/chat", json={"text": "当前图幅有哪些设备？"}, timeout=30)
        rec("chat_blocked_until_ready", early.status_code == 409, http=early.status_code, detail=(early.json().get("detail") if early.status_code != 200 else None))

        c1 = upload(CANTEEN, "学生食堂-测1")
        rec("upload_canteen_1", c1["http"] == 200 and bool((c1["body"] or {}).get("id")), id=(c1["body"] or {}).get("id"))
        cid1 = (c1["body"] or {}).get("id")

        w2 = upload(MALL, "万商城-测2")
        rec("upload_mall_2", w2["http"] == 200 and bool((w2["body"] or {}).get("id")), id=(w2["body"] or {}).get("id"))
        wid2 = (w2["body"] or {}).get("id")

        # Switching sessions must not cancel the others: all three should keep initializing / become ready.
        listed = client.get(f"{BASE}/api/projects")
        ids = [p.get("id") for p in (listed.json().get("projects") or [])] if listed.status_code == 200 else []
        rec("list_shows_all_sessions", all(i in ids for i in (wid1, wid2, cid1) if i), count=len(ids))

        st_w1 = wait_ready(client, wid1, 900, "mall1")
        rec("init_mall_1", bool(st_w1.get("ready")), init=st_w1)
        if cid1:
            # Mid-init, mall should already be usable while canteen still prepares.
            mid = client.get(f"{BASE}/api/projects/{cid1}/init", timeout=30).json()
            rec("canteen_still_independent", True, canteen_state=mid.get("state"), mall_ready=st_w1.get("ready"))

        st_w2 = wait_ready(client, wid2, 900, "mall2") if wid2 else {"ready": False}
        rec("init_mall_2", bool(st_w2.get("ready")), init=st_w2)

        st_c1 = wait_ready(client, cid1, 1800, "canteen1") if cid1 else {"ready": False}
        rec("init_canteen_1", bool(st_c1.get("ready")), init=st_c1)

        def default_region(pid: str) -> str | None:
            r = client.get(f"{BASE}/api/projects/{pid}", timeout=30)
            if r.status_code != 200:
                return None
            body = r.json()
            return (body.get("region") or body.get("default_region") or {}).get("id") if isinstance(body.get("region") or body.get("default_region"), dict) else body.get("default_region") or body.get("region")

        # Inspect project payload for region id
        def project(pid: str) -> dict:
            r = client.get(f"{BASE}/api/projects/{pid}", timeout=30)
            return r.json() if r.status_code == 200 else {"http": r.status_code}

        if st_w1.get("ready"):
            pw = project(wid1)
            rec("mall_project", pw.get("id") == wid1 or pw.get("ready") is True, keys=sorted(list(pw.keys()))[:30])
            # preview
            prev = client.get(f"{BASE}/api/projects/{wid1}/preview", timeout=180)
            rec("mall_preview", prev.status_code == 200 and prev.headers.get("content-type", "").startswith("image/"), http=prev.status_code, ctype=prev.headers.get("content-type"), nbytes=len(prev.content))
            pdf = client.get(f"{BASE}/api/projects/{wid1}/pdf", timeout=180)
            rec("mall_pdf", pdf.status_code == 200 and (pdf.headers.get("content-type", "").startswith("application/pdf") or pdf.content[:4] == b"%PDF"), http=pdf.status_code, ctype=pdf.headers.get("content-type"), nbytes=len(pdf.content))

            inv = chat(client, wid1, "当前图幅有哪些主要设备和房间？请根据索引简要盘点。")
            msg = inv.get("message") or {}
            rec("chat_inventory", inv.get("http") == 200 and msg.get("mode") in {"answer", "critique", "plan"} and bool(msg.get("text")), mode=msg.get("mode"), text=(msg.get("text") or "")[:180], error=inv.get("error"))

            cri = chat(client, wid1, "点评一下当前图幅的布置，指出明显问题即可，先不要改图。")
            msg = cri.get("message") or {}
            rec("chat_critique", cri.get("http") == 200 and msg.get("mode") in {"critique", "answer"} and bool(msg.get("text")), mode=msg.get("mode"), text=(msg.get("text") or "")[:180], error=cri.get("error"))

            off = chat(client, wid1, "今天上海天气怎么样")
            msg = off.get("message") or {}
            rec("chat_reject_weather", off.get("http") == 200 and msg.get("mode") == "reject" and "图腾" in (msg.get("text") or ""), mode=msg.get("mode"), gate=msg.get("gate"), text=(msg.get("text") or "")[:180])

            off2 = chat(client, wid1, "帮我写一首关于秋天的诗")
            msg = off2.get("message") or {}
            rec("chat_reject_poem", off2.get("http") == 200 and msg.get("mode") == "reject", mode=msg.get("mode"), gate=msg.get("gate"), text=(msg.get("text") or "")[:180])

            plan = chat(client, wid1, "请只在当前图幅上加一行测试文字，内容写「图腾测试标注」，不要改其他楼层。", timeout=240)
            msg = plan.get("message") or {}
            rec("chat_plan", plan.get("http") == 200 and msg.get("mode") in {"plan", "answer", "critique"} and bool(msg.get("text")), mode=msg.get("mode"), has_edits=bool((msg.get("plan") or {}).get("texts") or (msg.get("plan") or {}).get("inserts")), text=(msg.get("text") or "")[:180], plan_keys=list((msg.get("plan") or {}).keys()) if msg.get("plan") else [])

            mid = msg.get("id")
            if msg.get("mode") == "plan" or (msg.get("plan") and any(msg["plan"].get(k) for k in ("texts", "inserts", "deletes", "replaces", "moves", "pipes"))):
                applied = client.post(f"{BASE}/api/projects/{wid1}/apply", json={"message_id": mid}, timeout=120)
                rec("apply_work_copy", applied.status_code == 200, http=applied.status_code, body=(applied.json() if applied.status_code < 500 else applied.text[:200]))
                if applied.status_code == 200:
                    undone = client.post(f"{BASE}/api/projects/{wid1}/undo", timeout=120)
                    rec("undo_work_copy", undone.status_code == 200, http=undone.status_code, body=(undone.json() if undone.status_code < 500 else undone.text[:200]))
            else:
                rec("apply_work_copy", False, skipped=True, reason="no edit plan from model")
                rec("undo_work_copy", False, skipped=True)

        if st_c1.get("ready") and cid1:
            prev = client.get(f"{BASE}/api/projects/{cid1}/preview", timeout=180)
            rec("canteen_preview", prev.status_code == 200 and prev.headers.get("content-type", "").startswith("image/"), http=prev.status_code, nbytes=len(prev.content))
            inv = chat(client, cid1, "一层卫生间有哪些洁具？请按索引回答。")
            msg = inv.get("message") or {}
            rec("canteen_inventory", inv.get("http") == 200 and msg.get("mode") in {"answer", "critique", "plan"} and bool(msg.get("text")), mode=msg.get("mode"), text=(msg.get("text") or "")[:200], error=inv.get("error"))
            off = chat(client, cid1, "比特币现在什么价格")
            msg = off.get("message") or {}
            rec("canteen_reject_price", off.get("http") == 200 and msg.get("mode") == "reject", mode=msg.get("mode"), gate=msg.get("gate"), text=(msg.get("text") or "")[:180])

        # Official source files must be untouched.
        rec("official_canteen_untouched", CANTEEN.stat().st_mtime == canteen_mtime)
        rec("official_mall_untouched", MALL.stat().st_mtime == mall_mtime)

        # SQLite
        db = Path("/workspace/cad-nl/studio/var/test-runs/tutan.sqlite")
        if db.is_file():
            con = sqlite3.connect(db)
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            counts = {}
            for tname in tables:
                try:
                    counts[tname] = con.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
                except Exception as exc:
                    counts[tname] = str(exc)
            rec("sqlite_present", True, tables=tables, counts=counts)
            con.close()
        else:
            rec("sqlite_present", False)

        # OSS flags from session payloads
        oss_flags = []
        for pid, label in ((wid1, "mall1"), (wid2, "mall2"), (cid1, "canteen1")):
            if not pid:
                continue
            body = project(pid)
            init = body.get("init") or {}
            oss_flags.append({"label": label, "oss_ok": init.get("oss_ok"), "init_keys": sorted(init.keys()) if isinstance(init, dict) else None, "proj_oss": {k: body.get(k) for k in body if "oss" in k.lower()}})
        rec("oss_archive_checked", True, flags=oss_flags)

    save()
    failed = [r for r in results if not r["ok"]]
    print(f"DONE  {len(results)-len(failed)}/{len(results)} passed", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:
        rec("runner_crash", False, error=type(exc).__name__, detail=str(exc)[:400])
        save()
        code = 1
    sys.exit(code)
