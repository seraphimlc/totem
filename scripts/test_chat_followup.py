"""Unit tests: in-session follow-ups are on-topic, and 可以的 after a critique nudges a plan."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import decision
from app.prompts import (
    detect_intent,
    is_progress_question,
    is_session_followup,
    is_short_agreement,
    is_visibility_followup,
    plan_nudge_for,
    visibility_short_circuit,
)


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name} failed {detail}".strip())
    print(f"ok  {name}")


def test_followups():
    for text in (
        "怎么样了",
        "怎么样了？",
        "进度怎么样了？做完了吗",
        "做完了吗",
        "可以的",
        "好的",
        "把蹲便改成坐便这个事，你到底能不能做？",
        "你现在给改了吗？",
    ):
        check(f"followup:{text}", is_session_followup(text))
        result = asyncio.run(decision.classify_off_topic(text, drawing_name="万商城", region_label="二层卫排水详图"))
        check(
            f"jev-skip:{text}",
            result.get("off_topic") is False and result.get("reason") == "session_followup",
            result,
        )

    check("weather not followup", not is_session_followup("今天天气怎么样了"))
    check("confirm is apply", detect_intent("确认") == "apply")
    check("可以的 is not apply", detect_intent("可以的") is None)
    check("可以的 is agreement", is_short_agreement("可以的"))
    check("long policy is not short agreement", not is_short_agreement(
        "可以的，那就不要硬放四个了。后面再有这种不合理的情况，你可以给出建议的。我确认后再做。可以吗？"
    ))
    check("progress", is_progress_question("做完了吗"))


def test_plan_nudge():
    critique = {
        "role": "assistant",
        "mode": "critique",
        "text": "懂，坐便应当背部靠墙。您确认后再调整，可以吗？",
        "plan": {"mode": "critique", "inserts": [], "replaces": [], "moves": []},
    }
    check("nudge after 可以的", bool(plan_nudge_for("可以的", [critique])))
    ack = {
        "role": "assistant",
        "mode": "answer",
        "text": "好的，就按这个方向调整，仅限当前二层卫排水详图的8处。",
        "plan": {"mode": "answer", "inserts": []},
    }
    check("nudge after 做完了吗", bool(plan_nudge_for("做完了吗", [critique, {"role": "user", "text": "可以的"}, ack])))
    reject = {
        "role": "assistant",
        "mode": "reject",
        "text": "图腾只讨论当前图纸。",
        "plan": {"mode": "reject"},
    }
    check(
        "nudge after reject then 进度",
        bool(plan_nudge_for("进度怎么样了？做完了吗", [critique, {"role": "user", "text": "可以的"}, ack, reject])),
    )
    planned = {
        "role": "assistant",
        "mode": "plan",
        "text": "请看右侧预览。",
        "plan": {"mode": "plan", "replaces": [{"block": "马桶A", "xy": [1, 2]}]},
    }
    check("no nudge after a real plan", plan_nudge_for("做完了吗", [critique, planned]) is None)
    check("no nudge on write-back confirm", plan_nudge_for("确认", [critique]) is None)
    check(
        "no nudge on long new instruction",
        plan_nudge_for(
            "可以的，那就不要硬放四个了。后面再有这种不合理的情况，你可以给出建议的。我确认后再做。可以吗？",
            [critique],
        )
        is None,
    )



def test_visibility_followup():
    phrases = (
        "预览里没有变化啊，还是三个台盆",
        "没看到呢，你是没做还是在渲染？",
        "没看到呢",
        "预览没有变化",
        "你是没做还是在渲染",
    )
    for text in phrases:
        check(f"visibility:{text}", is_visibility_followup(text))
        check(f"visibility-is-followup:{text}", is_session_followup(text))
        result = asyncio.run(
            decision.classify_off_topic(text, drawing_name="万商城", region_label="二层卫排水详图")
        )
        check(
            f"jev-skip-visibility:{text}",
            result.get("off_topic") is False and result.get("reason") == "session_followup",
            result,
        )

    check("weather still not visibility", not is_visibility_followup("今天天气怎么样了"))
    check("plain inventory not visibility", not is_visibility_followup("女卫里现在有几个台盆"))

    region = {
        "id": "subview:水施-007:二层卫排水详图",
        "bbox": [2955772.3, 504433.0, 2980607.0, 516475.8],
        "title": "二层卫排水详图",
    }
    pending = {
        "message_id": "plan-basin",
        "region": region["id"],
        "reply": "已在女卫下方墙边加了一个洗手盆，请看右侧预览。",
        "plan": {
            "mode": "plan",
            "reply": "已在女卫下方墙边加了一个洗手盆，请看右侧预览。",
            "room": "女卫",
            "sheet": "二层卫排水详图",
            "inserts": [{"block": "A$C5D954283", "xy": [2965250, 506800]}],
            "deletes": [],
            "replaces": [],
            "moves": [],
            "pipes": [],
            "texts": [],
        },
    }
    for text in ("预览里没有变化啊，还是三个台盆", "没看到呢，你是没做还是在渲染？"):
        hit = visibility_short_circuit(text, pending, region)
        check(f"short-circuit:{text}", bool(hit and hit.get("resend")), hit)
        check(f"short-circuit-no-new-plan:{text}", hit.get("plan") is pending["plan"])
        body = hit.get("text") or ""
        check(f"short-circuit-points-right:{text}", "右侧" in body, body)
        check(f"short-circuit-points-basin:{text}", "台盆" in body or "洗手盆" in body, body)
        check(f"short-circuit-points-place:{text}", "女卫" in body and "下方" in body, body)

    check("no short-circuit without pending", visibility_short_circuit("没看到呢", None, region) is None)
    check(
        "no short-circuit with empty plan",
        visibility_short_circuit("没看到呢", {"plan": {"mode": "answer", "inserts": []}}, region) is None,
    )
    other = dict(region)
    other["id"] = "other-region"
    check(
        "no short-circuit for other region",
        visibility_short_circuit("没看到呢", pending, other) is None,
    )

    critique = {
        "role": "assistant",
        "mode": "critique",
        "text": "女卫还可以再加一个洗手盆，您确认后再调整，可以吗？",
        "plan": {"mode": "critique", "inserts": []},
    }
    check(
        "visibility nudges a plan when none is pending",
        bool(plan_nudge_for("预览里没有变化啊，还是三个台盆", [critique])),
    )
    planned = {
        "role": "assistant",
        "mode": "plan",
        "text": "请看右侧预览。",
        "plan": pending["plan"],
    }
    check(
        "visibility does not nudge after a real plan",
        plan_nudge_for("没看到呢，你是没做还是在渲染？", [critique, planned]) is None,
    )


if __name__ == "__main__":
    test_followups()
    test_plan_nudge()
    test_visibility_followup()
    print("ALL PASSED")
