"""Off-topic gate via the OpenRouter Decisions API (Jev)."""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from . import config
from .prompts import is_session_followup

log = logging.getLogger("tutan")

REJECT_REPLY = "图腾只讨论当前图纸。天气、新闻、闲聊或其他跟这张图无关的问题，我这边不回答。"

EMPTY_EDITS: dict[str, Any] = {
    "inserts": [],
    "deletes": [],
    "replaces": [],
    "moves": [],
    "pipes": [],
    "texts": [],
    "text_edits": [],
    "do_not_touch": [],
}

QUESTIONS = {
    "off_topic": {
        "type": "noul",
        "instructions": (
            "Is the user's message off-topic for 图腾, a CAD drawing workbench "
            "that only discusses the current drawing and related edit work?"
        ),
        "criteria": {
            "true": (
                "The message is unrelated to the current drawing or the workbench: "
                "weather, news, chitchat, poetry, writing code, politics, medical advice, "
                "general knowledge, prices, or any request that does not need this drawing."
            ),
            "false": (
                "The message is about this drawing or the workbench: inventory, rooms, "
                "equipment, layers, blocks, dimensions, critique, edit requests, confirm, "
                "undo, preview, export, regions, questions about what is on the sheet, "
                "progress or status of a current edit (e.g. 怎么样了, 做完了吗), "
                "whether a preview has updated or is still rendering "
                "(e.g. 没看到, 预览没有变化, 你是没做还是在渲染), "
                "whether the workbench can make a requested change, "
                "or a short confirmation of a suggested change (e.g. 可以的, 好的)."
            ),
        },
    }
}


def reject_plan(reply: str = REJECT_REPLY) -> dict[str, Any]:
    return {
        "mode": "reject",
        "reply": reply,
        "room": "",
        "sheet": "",
        "analysis": "",
        **{k: list(v) for k, v in EMPTY_EDITS.items()},
    }


def _probability(payload: dict[str, Any]) -> Optional[float]:
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        return None
    ans = answers.get("off_topic")
    if not isinstance(ans, dict) or ans.get("type") != "noul":
        return None
    try:
        return float(ans.get("noul"))
    except (TypeError, ValueError):
        return None


async def classify_off_topic(
    text: str,
    *,
    drawing_name: str = "",
    region_label: str = "",
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return whether the user turn is off-topic.

    If Jev is not configured or the call fails, off_topic is False (fail open)
    so the drawing model can still apply its own reject rule.
    Short in-session follow-ups (进度、能不能做、可以的) skip Jev and are on-topic.
    """
    if is_session_followup(text):
        return {"ok": True, "off_topic": False, "skipped": True, "reason": "session_followup"}
    if not config.decision_configured():
        return {"ok": False, "off_topic": False, "skipped": True, "reason": "not_configured"}
    body = {
        "model": config.DECISION_MODEL,
        "state": {
            "product": "图腾",
            "product_role": "建筑与给排水 CAD 改图工作台，只讨论当前图纸及改图相关操作。",
            "drawing": drawing_name or "",
            "region": region_label or "",
            "user_message": (text or "").strip(),
        },
        "questions": QUESTIONS,
    }
    if session_id:
        body["session_id"] = session_id
    headers = {
        "Authorization": f"Bearer {config.DECISION_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://cad.visitworld.me",
        "X-Title": "Tuteng CAD",
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(config.DECISION_BASE_URL, headers=headers, json=body)
    except Exception as exc:
        log.warning("jev decision call failed: %s", type(exc).__name__)
        return {"ok": False, "off_topic": False, "error": type(exc).__name__}
    if resp.status_code >= 400:
        log.warning("jev decision http %s", resp.status_code)
        return {"ok": False, "off_topic": False, "error": f"http_{resp.status_code}"}
    try:
        payload = resp.json()
    except Exception:
        log.warning("jev decision returned non-json")
        return {"ok": False, "off_topic": False, "error": "bad_json"}
    prob = _probability(payload)
    if prob is None:
        log.warning("jev decision missing noul answer")
        return {"ok": False, "off_topic": False, "error": "missing_answer"}
    threshold = config.DECISION_REJECT_THRESHOLD
    off_topic = prob >= threshold
    log.info("jev off_topic=%.3f threshold=%.2f reject=%s", prob, threshold, off_topic)
    return {
        "ok": True,
        "off_topic": off_topic,
        "probability": prob,
        "threshold": threshold,
        "model": payload.get("model") or config.DECISION_MODEL,
    }
