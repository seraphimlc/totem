#!/usr/bin/env python3
"""Two sessions can chat at once; GETs stay fast while one chat waits on the model."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"ok {name}" + (f" — {detail}" if detail else ""), flush=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _tiny_png(path: Path) -> Path:
    # 1x1 transparent PNG
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return path


def _region() -> dict:
    return {
        "id": "sheet:1F",
        "kind": "sheet",
        "sheet_id": "1F",
        "title": "1F建筑平面图",
        "bbox": [0.0, 0.0, 1000.0, 800.0],
    }


async def _parse_sse_done(resp: httpx.Response, *, timeout: float) -> dict:
    done = None
    errors = []
    deadline = time.monotonic() + timeout
    async for line in resp.aiter_lines():
        if time.monotonic() > deadline:
            raise TimeoutError("SSE timed out before done")
        if not line.startswith("data: "):
            continue
        ev = json.loads(line[6:])
        if ev.get("type") == "error":
            errors.append(ev.get("message"))
        if ev.get("type") == "done":
            done = ev
            break
    if errors and not done:
        raise AssertionError(f"chat error: {errors}")
    if not done:
        raise AssertionError("chat stream ended without a done event")
    return done


async def _run() -> None:
    from app import apply, auth, cad_render, config, db, decision, init_proj, maas, projects
    from app import main as app_main

    tmpdir = Path(tempfile.mkdtemp(prefix="tutan-concurrency-"))
    preview = _tiny_png(tmpdir / "preview.png")
    prev_data = config.CAD_DATA_DIR
    config.CAD_DATA_DIR = str(tmpdir)
    db.close()

    fixtures: dict[str, dict] = {}
    for pid, name in (("conc-a", "会话A"), ("conc-b", "会话B")):
        root = tmpdir / pid
        root.mkdir()
        dxf = root / "work.dxf"
        dxf.write_text("0\nSECTION\n0\nENDSEC\n0\nEOF\n", encoding="utf-8")
        fixtures[pid] = {
            "id": pid,
            "name": name,
            "kind": "session",
            "root": str(root),
            "dxf_path": str(dxf),
            "available": True,
            "encoding": "utf-8",
            "chat_path": str(root / "chat.json"),
        }

    excerpt_started = threading.Event()
    complete_started: dict[str, float] = {}
    complete_finished: dict[str, float] = {}
    orig_resolve = projects.resolve_project
    orig_get_region = projects.get_region
    orig_excerpt = projects.excerpt_index
    orig_is_ready = init_proj.is_ready
    orig_public = init_proj.public_status
    orig_resume = init_proj.resume_unfinished
    orig_user = auth.current_user
    orig_complete = maas.complete
    orig_classify = decision.classify_off_topic
    orig_preview = cad_render.base_preview
    orig_current = apply.current_dxf
    orig_status = apply.status

    def fake_resolve(project_id: str):
        if project_id in fixtures:
            return dict(fixtures[project_id])
        return orig_resolve(project_id)

    def fake_region(proj, region_id=None):
        return _region()

    def fake_excerpt(proj, region, user_text, *, max_chars=12000):
        if proj.get("id") == "conc-a":
            excerpt_started.set()
            time.sleep(1.5)
        return "【测试摘录】房间=大厅"

    def fake_ready(proj):
        if proj.get("id") in fixtures:
            return True
        return orig_is_ready(proj)

    def fake_public(proj):
        if proj.get("id") in fixtures:
            return {"state": "ready", "ready": True, "message": "准备完成", "steps": [], "done": 4, "total": 4}
        return orig_public(proj)

    async def fake_classify(text, **kwargs):
        return {"off_topic": False, "reason": "test"}

    async def fake_complete(**kwargs):
        user_text = str(kwargs.get("user_text") or "")
        pid = "conc-a" if "MARKER-A" in user_text else "conc-b" if "MARKER-B" in user_text else "unknown"
        complete_started[pid] = time.monotonic()
        delay = 2.0 if pid == "conc-a" else 0.3
        await asyncio.sleep(delay)
        complete_finished[pid] = time.monotonic()
        yield {"type": "delta", "text": f'{{"mode":"answer","reply":"回复{pid}。"}}'}
        yield {"type": "end"}

    def fake_preview(*args, **kwargs):
        return preview

    def fake_current(proj):
        return Path(proj["dxf_path"])

    def fake_apply_status(proj):
        return {"dirty": False, "can_undo": False, "pending_plan": None, "official_untouched": True}

    projects.resolve_project = fake_resolve
    projects.get_region = fake_region
    projects.excerpt_index = fake_excerpt
    init_proj.is_ready = fake_ready
    init_proj.public_status = fake_public
    init_proj.resume_unfinished = lambda: 0
    auth.current_user = lambda request: "tester"
    maas.complete = fake_complete
    decision.classify_off_topic = fake_classify
    cad_render.base_preview = fake_preview
    apply.current_dxf = fake_current
    apply.status = fake_apply_status

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app_main.app, host="127.0.0.1", port=port, log_level="warning", workers=1)
    )
    task = asyncio.create_task(server.serve())
    try:
        deadline = time.monotonic() + 8
        while not server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            await asyncio.sleep(0.05)

        base = f"http://127.0.0.1:{port}"
        timeout = httpx.Timeout(30.0, read=30.0, connect=5.0)
        async with httpx.AsyncClient(base_url=base, timeout=timeout) as client:
            # Start both chats together. A sleeps 1.5s in excerpt (thread) then 2s
            # in complete; B skips the excerpt sleep and waits 0.3s in complete,
            # so B can finish while A is still preparing or both model calls overlap.
            t0 = time.monotonic()
            req_a = client.stream(
                "POST",
                "/api/projects/conc-a/chat",
                json={"text": "MARKER-A 这张图有哪些房间？", "model": "Tifa-6.1-sol"},
            )
            req_b = client.stream(
                "POST",
                "/api/projects/conc-b/chat",
                json={"text": "MARKER-B 这张图的走廊在哪？", "model": "Tifa-6.1-sol"},
            )
            async with req_a as resp_a, req_b as resp_b:
                _ok("chat-a-accepted", resp_a.status_code == 200, str(resp_a.status_code))
                _ok("chat-b-accepted", resp_b.status_code == 200, str(resp_b.status_code))

                # Wait until A's sync excerpt has started, so a blocked loop would stall GETs.
                start_wait = time.monotonic()
                while not excerpt_started.is_set() and time.monotonic() - start_wait < 3:
                    await asyncio.sleep(0.02)
                _ok("excerpt-a-started", excerpt_started.is_set())

                async def timed_get(path: str):
                    t = time.monotonic()
                    res = await client.get(path)
                    return path, res.status_code, time.monotonic() - t, res

                gets = await asyncio.gather(
                    timed_get("/api/projects/conc-b"),
                    timed_get("/api/projects/conc-b/chat"),
                    timed_get("/api/projects/conc-b/preview"),
                    timed_get("/api/projects/conc-a"),
                    timed_get("/api/projects/conc-a/chat"),
                )
                for path, status, elapsed, res in gets:
                    _ok(f"get-ok:{path}", status == 200, f"status={status}")
                    _ok(f"get-fast:{path}", elapsed < 0.8, f"{elapsed:.3f}s")

                done_b, done_a = await asyncio.gather(
                    _parse_sse_done(resp_b, timeout=15),
                    _parse_sse_done(resp_a, timeout=15),
                )

            elapsed_a = time.monotonic() - t0
            _ok("reply-a", "conc-a" in ((done_a.get("message") or {}).get("text") or ""), str(done_a))
            _ok("reply-b", "conc-b" in ((done_b.get("message") or {}).get("text") or ""), str(done_b))
            # B must be able to finish while A is still waiting on the fake model.
            _ok("both-models-ran", "conc-a" in complete_started and "conc-b" in complete_started, str(complete_started))
            # B must finish its model call before A does — typically during A's excerpt.
            _ok(
                "b-finished-before-a",
                complete_finished["conc-b"] < complete_finished["conc-a"],
                f"A={complete_started['conc-a']:.3f}->{complete_finished['conc-a']:.3f} B={complete_started['conc-b']:.3f}->{complete_finished['conc-b']:.3f}",
            )
            _ok("a-took-expected-time", elapsed_a >= 2.0, f"{elapsed_a:.3f}s")

            # User turns persisted independently.
            ids_a = [m["role"] + ":" + (m.get("text") or "")[:20] for m in projects.read_chat(fixtures["conc-a"])["messages"]]
            ids_b = [m["role"] + ":" + (m.get("text") or "")[:20] for m in projects.read_chat(fixtures["conc-b"])["messages"]]
            _ok("chat-a-user-saved", any(x.startswith("user:") and "MARKER-A" in x for x in ids_a), str(ids_a))
            _ok("chat-b-user-saved", any(x.startswith("user:") and "MARKER-B" in x for x in ids_b), str(ids_b))
            _ok("chats-not-mixed", all("MARKER-B" not in x for x in ids_a) and all("MARKER-A" not in x for x in ids_b), f"A={ids_a} B={ids_b}")
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5)
        except Exception:
            task.cancel()
        projects.resolve_project = orig_resolve
        projects.get_region = orig_get_region
        projects.excerpt_index = orig_excerpt
        init_proj.is_ready = orig_is_ready
        init_proj.public_status = orig_public
        init_proj.resume_unfinished = orig_resume
        auth.current_user = orig_user
        maas.complete = orig_complete
        decision.classify_off_topic = orig_classify
        cad_render.base_preview = orig_preview
        apply.current_dxf = orig_current
        apply.status = orig_status
        config.CAD_DATA_DIR = prev_data
        db.close()


def main() -> int:
    asyncio.run(_run())
    print("ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
