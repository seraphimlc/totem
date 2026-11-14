"""User turns must land in chat.json immediately, even if the reply is cancelled."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import main as app_main  # noqa: E402
from app import projects  # noqa: E402


def _ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"FAIL {name}: {detail}")
    print(f"ok {name}", flush=True)


def _proj(tmpdir: Path) -> dict:
    chat = tmpdir / "chat.json"
    return {"id": "t-persist", "chat_path": chat, "name": "测试"}


def test_append_chat_is_idempotent_and_keeps_user() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        proj = _proj(tmp)
        user = {"id": "u1", "role": "user", "text": "消毒房加洗衣机"}
        assistant = {"id": "a1", "role": "assistant", "text": "已记下"}
        projects.append_chat(proj, [user])
        data = projects.read_chat(proj)
        _ok("user-saved-first", [m["id"] for m in data["messages"]] == ["u1"])
        projects.append_chat(proj, [user, assistant])
        data = projects.read_chat(proj)
        ids = [m["id"] for m in data["messages"]]
        _ok("no-duplicate-user", ids == ["u1", "a1"], str(ids))
        projects.append_chat(proj, [assistant])
        data = projects.read_chat(proj)
        ids = [m["id"] for m in data["messages"]]
        _ok("no-duplicate-assistant", ids == ["u1", "a1"], str(ids))


def test_plan_chat_events_after_user_persist() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        proj = _proj(tmp)
        region = {"id": "sheet:1F", "bbox": [0, 0, 1, 1]}
        user = {"id": "u-sse", "role": "user", "text": "改一下", "region": region["id"]}
        assistant = {
            "id": "a-sse",
            "role": "assistant",
            "text": "方案",
            "plan": {"mode": "plan", "reply": "方案", "inserts": []},
            "region": region["id"],
            "mode": "plan",
        }
        projects.append_chat(proj, [user])
        events = list(app_main.plan_chat_events(proj, user, assistant, region, render=False))
        _ok("done-first", events and events[0]["type"] == "done")
        ids = [m["id"] for m in projects.read_chat(proj)["messages"]]
        _ok("turn-kept-once", ids == ["u-sse", "a-sse"], str(ids))


def main() -> int:
    test_append_chat_is_idempotent_and_keeps_user()
    test_plan_chat_events_after_user_persist()
    print("ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
