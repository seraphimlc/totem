#!/usr/bin/env python3
"""Local checks for the precise init bar and the background OSS archive queue."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("CAD_DATA_DIR", "")


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"PASS {name}" + (f" — {detail}" if detail else ""))


def test_steps_and_progress() -> None:
    from app import init_proj

    _ok("steps-three", [s[0] for s in init_proj.STEPS] == ["save", "index", "preview"])
    _ok("no-upload-step", "upload" not in dict(init_proj.STEPS))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "sess"
        (root / ".studio").mkdir(parents=True)
        proj = {"id": "s-progress", "root": str(root)}
        st = init_proj.default_status("开始")
        init_proj.report_progress(proj, st, step="save", current=0, total=1, flush=True)
        _ok("save-start", st["progress"]["percent"] == 0)
        init_proj.report_progress(proj, st, step="save", current=1, total=1, flush=True)
        _ok("save-done", st["progress"]["percent"] == 10)
        init_proj.report_progress(proj, st, step="index", current=5, total=10, flush=True)
        _ok("index-half", st["progress"]["percent"] == 32.5, str(st["progress"]))
        init_proj.report_progress(proj, st, step="preview", current=3, total=10, flush=True)
        _ok("preview-30", st["progress"]["percent"] == 68.5, str(st["progress"]))
        st["ready"] = True
        init_proj.report_progress(proj, st, step="preview", current=10, total=10, flush=True)
        _ok("ready-100", st["progress"]["percent"] == 100)

        public = init_proj.public_status(proj)
        _ok("public-has-progress", isinstance(public.get("progress"), dict) and "percent" in public["progress"])

        stale = init_proj.default_status()
        stale["steps"].append({"id": "upload", "label": "上传到云端", "state": "running"})
        stale["phase"] = "upload"
        init_proj._normalize_steps(stale)
        _ok("normalize-drops-upload", [s["id"] for s in stale["steps"]] == ["save", "index", "preview"])
        _ok("normalize-phase", stale["phase"] != "upload", stale["phase"])


def test_archive_queue() -> None:
    from app import archive_queue, config, projects

    with tempfile.TemporaryDirectory() as tmp:
        config.CAD_DATA_DIR = tmp
        archive_queue._UPLOADER = None
        updates: list[tuple[str, dict]] = []
        original_update = projects.update_session

        def fake_update(pid, fields):
            updates.append((pid, dict(fields)))

        projects.update_session = fake_update  # type: ignore[method-assign]
        archive_queue.BACKOFF_SECONDS = (0, 0, 0, 0, 0)
        try:
            src = Path(tmp) / "source.dwg"
            dxf = Path(tmp) / "out.dxf"
            src.write_bytes(b"dwg-bytes")
            dxf.write_bytes(b"dxf-bytes")

            calls = {"n": 0}

            def flaky(path: Path, key: str, **_kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TimeoutError("simulated timeout")
                return {"key": key, "url": f"https://example.test/{key}"}

            started = archive_queue.start_worker(uploader=flaky)
            _ok("worker-started", started is True)
            job_id = archive_queue.enqueue(
                "s-queue",
                [
                    {"path": str(src), "kind": "source", "name": src.name},
                    {"path": str(dxf), "kind": "dxf", "name": dxf.name},
                ],
                kind="session",
                name="测试会话",
            )
            _ok("enqueued", bool(job_id))
            # Dedup: second enqueue for the same project stays a single pending/running job.
            archive_queue.enqueue(
                "s-queue",
                [{"path": str(dxf), "kind": "dxf", "name": dxf.name}],
                kind="session",
                name="测试会话",
            )
            _ok("idle-after-retry", archive_queue.wait_idle(timeout=8), json.dumps(archive_queue.counts()))
            stats = archive_queue.counts()
            _ok("queue-done", stats["done"] == 1 and stats["failed"] == 0 and stats["pending"] == 0, json.dumps(stats))
            _ok("retried-once", calls["n"] >= 2, f"calls={calls['n']}")
            _ok("session-keys", any("oss_dxf_key" in fields for _, fields in updates), str(updates))

            done = json.loads((archive_queue.queue_root() / "done" / "s-queue.json").read_text(encoding="utf-8"))
            _ok("done-status", done.get("status") == "done")
        finally:
            archive_queue.stop_worker()
            projects.update_session = original_update  # type: ignore[method-assign]


def test_resume_checkpoints() -> None:
    from app import config, db, init_proj

    with tempfile.TemporaryDirectory() as tmp:
        prev_data = config.CAD_DATA_DIR
        db.close()
        config.CAD_DATA_DIR = tmp
        db.close()
        try:
            _run_resume_checkpoints(tmp, init_proj, db)
        finally:
            db.close()
            config.CAD_DATA_DIR = prev_data


def _run_resume_checkpoints(tmp: str, init_proj, db) -> None:
        root = Path(tmp) / "sess"
        out = root / "out"
        out.mkdir(parents=True)
        dxf = out / "source.dxf"
        dxf.write_bytes((b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n") * 4)
        proj = {
            "id": "s-resume",
            "root": str(root),
            "dxf_path": str(dxf),
            "deliverable": "out/source.dxf",
            "index_json": str(root / "index.json"),
        }
        _ok("dxf-ready", init_proj._dxf_ready(proj))
        _ok("need-index", init_proj._first_incomplete_step(proj) == "index")

        leftover = {
            "schema": "cad-nl-drawing-index/v1.1",
            "sheets": [{"id": "1F", "title": "one"}],
            "rooms": [{"name": "男卫", "sheet": "1F", "world_xy": [1.0, 2.0]}],
            "layers": [{"name": "EL-DIM"}],
            "blocks": [{"name": "做成ACAD TuKuai0155", "insert_count": 2}],
        }
        (root / "index.json").write_text(json.dumps(leftover, ensure_ascii=False), encoding="utf-8")
        _ok("index-ready-from-leftover-file", init_proj._index_ready(proj))
        loaded = db.load_index("s-resume")
        _ok("leftover-imported-to-sqlite", loaded == leftover, str((loaded or {}).keys()))
        nxt = init_proj._first_incomplete_step(proj)
        _ok("need-preview", nxt == "preview", str(nxt))

        # After import, SQLite is the live source even if the leftover file is gone.
        (root / "index.json").unlink()
        _ok("index-ready-sqlite-only-after-import", init_proj._index_ready(proj))

        # A project with only a SQLite row (never had index files) is also ready.
        db.save_index("s-resume-dbonly", {
            "schema": "cad-nl-drawing-index/v1.1",
            "sheets": [{"id": "2F", "title": "two"}],
            "rooms": [],
            "layers": [{"name": "0"}],
        })
        proj_db = {
            "id": "s-resume-dbonly",
            "root": str(root),
            "dxf_path": str(dxf),
            "deliverable": "out/source.dxf",
            "index_json": str(root / "missing-index.json"),
        }
        _ok("index-ready-sqlite-only", init_proj._index_ready(proj_db))
        _ok("no-index-file-created", not (root / "index.json").is_file() and not (root / "INDEX.md").is_file())

        st = init_proj.default_status()
        init_proj._set_step(st, "save", "done")
        init_proj._set_step(st, "index", "done")
        init_proj._set_step(st, "preview", "running", "warming")
        st["state"] = "running"
        init_proj.write_status(proj, st)
        loaded = init_proj.read_status(proj)
        loaded["state"] = "interrupted"
        loaded["message"] = "interrupted, continue from preview"
        init_proj.write_status(proj, loaded)
        again = init_proj.read_status(proj)
        _ok("interrupted-persisted", again.get("state") == "interrupted")
        pub = init_proj.public_status(proj)
        _ok("resumable-flag", pub.get("resumable") is True)


def test_registry_merge() -> None:
    from app import projects

    builtins = [
        {
            "id": "wanshangcheng",
            "name": "万商城3号楼给排水",
            "kind": "builtin",
            "data_subdir": "万商城3号楼给排水",
            "deliverable": "out/demo.dxf",
            "encoding": "utf-8",
        }
    ]
    sessions = [
        {
            "id": "wanshangcheng",
            "name": "万商城3号楼给排水",
            "kind": "builtin",
            "data_subdir": None,
            "deliverable": None,
            "oss_dxf_key": "oss/demo.dxf",
        },
        {
            "id": "s-user",
            "name": "用户会话",
            "kind": "session",
            "data_subdir": "sessions/s-user",
            "deliverable": "out/source.dxf",
        },
    ]
    merged = projects.merge_registry(sessions, builtins)
    ids = [item["id"] for item in merged]
    _ok("registry-order", ids == ["s-user", "wanshangcheng"], str(ids))
    wsc = next(item for item in merged if item["id"] == "wanshangcheng")
    _ok("builtin-subdir-kept", wsc.get("data_subdir") == "万商城3号楼给排水")
    _ok("builtin-deliverable-kept", wsc.get("deliverable") == "out/demo.dxf")
    _ok("oss-key-overlaid", wsc.get("oss_dxf_key") == "oss/demo.dxf")
    _ok("no-duplicate-builtin", ids.count("wanshangcheng") == 1)



def main() -> int:
    test_steps_and_progress()
    test_archive_queue()
    test_resume_checkpoints()
    test_registry_merge()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
