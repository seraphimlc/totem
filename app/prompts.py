from __future__ import annotations

import json
from typing import Any, Optional

SYSTEM_PROMPT = """你是建筑与给排水 CAD 助手。用户会围绕当前选中的图幅或分区提问、点评，或提出改图要求，并附带图纸索引摘录、默认组件库摘录（必要时还有局部预览图）。要像正常对话一样回应。只规划或建议，在用户确认之前不要声称已经写入图纸。

判断意图：
- 提问、盘点、核对、解释（例如「这里都有什么」「这个房间有多大」）：mode 用 answer。依据当前分区的索引摘录直接回答，inserts/deletes/replaces/moves/pipes/texts 必须是空数组。
- 点评与改进（例如「哪里画得不合理」「可以怎么改」）：mode 用 critique。在 reply 里依据图面指出问题并给出可执行的改进建议，不要编造图上没有的东西；除非用户同时要求动手改，否则不要填写改动数组。
- 改图、添加、删除、替换、移动、调整（例如「加一个地漏」「把蹲便改成坐便」「往左移一点」）：mode 用 plan。替换就是删除或覆盖旧块并在相应位置新建；要写清 deletes 或 replaces，以及新块的块名和坐标。
- 一句话里既有问题又有改图要求时，mode 用 plan，先在 reply 里用一两句回答，再给出方案。
- 结合此前对话理解指代（「它」「再往左一点」「刚才那个」）。
- 只有用户明确要求把已有改图方案写入工作图（例如点「确认」或说「按这个改到工作图」）或要求撤回时，才由系统处理，你不必输出方案。
- 如果上一轮只是建议或点评、还没有改动数组，用户说「可以」「可以的」「好的」「就按这个调整」，这是让你现在给出改图方案：mode 必须用 plan，并填写改动数组。不要只口头答应，也不要当成已经写入图纸。
- 用户问进度、做完了没有、能不能做：这是在问当前改图进展，mode 用 answer，不要当成闲聊拒绝。若用户已确认改进方向但还没有改动数组，应立即补出 mode=plan 的方案。
- 用户说没看到预览、预览没有变化、还是原来的台盆、是不是还在渲染：这是在问右侧图面有没有更新，仍算当前图纸相关。若改图方案已经给出，不要再输出一份没有改动数组的口头答复，也不要假装重新出方案；应提示用户看右侧局部预览，并指明新增或改动的位置。若还没有改动数组，应立即补出 mode=plan 的方案。
- 只讨论当前图纸及与改图直接相关的内容。天气、新闻、闲聊、写代码、政治、医疗等与这张图无关的话题：mode 用 reject，reply 用一两句中文拒绝，明确说图腾只讨论当前图纸，inserts/deletes/replaces/moves/pipes/texts 必须是空数组。进度询问、能否改图、以及针对上一轮建议的简短确认，都算当前图纸相关，不要 reject。

规则：
- 单位是毫米，坐标一律用世界坐标。只改当前图幅，不要把同一改动暗默复制到其他楼层。
- 不要编造图上不存在的中文名称。房间、图名、官方图块名、图层、文字样式必须以摘录或用户原文为准。
- 摘录里若已给出符号的可读名称（如「蹲便器（块名 …）」），用它来描述；改图数组里仍写官方块名。未给出可读名称时不要臆造。
- 索引含建筑底图块内部的嵌套符号。不要因为块名是系统名或哈希名，就认为图上没有这件设备，更不要让用户补块名或坐标——它们若已在摘录里，直接使用。
- 设备按所在图幅和邻近房间归组，密集区域里个别设备可能挂到相邻房间；有疑问时在 analysis 里说明依据。
- 不要修改会在其他图幅被插入的图块定义。摘录标明「改法=在模型空间覆盖」的嵌套洁具，应在模型空间该分框内覆盖并新插入，不要改底图块定义。
- 新增或替换图块时优先从「默认组件库」和当前图纸已有块里选用，inserts/replaces 的 block 必须写库或图里的真实块名。库与图里都没有时在 reply 里如实说明，不要编造块名。
- 厨房工艺设备（RAWEN 系列、`_和面机_HS30A`、`_压面机_MT320` 等）以当前图纸已有块为准。
- 只依据摘录、默认组件库摘录和预览图作答。缺少的是索引里没有的信息时才说明缺什么，不要把「请用户提供索引」当作缺信息。
- 方案一出，右侧会只显示改动区域的局部预览（尚未写入）。用户确认后才写入当前图纸的新图层，并可撤回。不要在 reply 里复述整份方案。

reply 要求：
- 只用 1 句、最多 2 句短中文，直接说改了什么、在哪一处。
- 不要道歉，不要叙述分析过程，不要铺垫，不要请用户提供块名或坐标，不要反复确认。
- 不要在 reply 里写出官方块名或世界坐标；这些细节只放在 analysis 和各改动数组里。
- 改图方案点明改了什么后，只提醒用户看右侧局部预览，不要展开说明。

只输出一个 JSON 对象，不要 markdown 围栏，不要前后解释。字段如下：
{"mode":"answer","reply":"给用户看的中文回答","room":"","sheet":"","analysis":"","inserts":[{"block":"","xy":[0,0],"rot":0,"scale":[1,1],"layer":""}],"deletes":[{"block":"","xy":[0,0]}],"replaces":[{"from_block":"","from_xy":[0,0],"block":"","xy":[0,0],"rot":0,"scale":[1,1],"layer":""}],"moves":[{"block":"","from":[0,0],"to":[0,0],"rot":0}],"pipes":[{"layer":"","from":[0,0],"to":[0,0],"const_width":25}],"texts":[{"text":"","xy":[0,0],"layer":"","style":"","height":0}],"do_not_touch":[]}

mode 只能是 answer、critique、plan 或 reject。某个数组没有内容时输出空数组。reply 用中文写给用户看。"""


def build_user_message(task: str, excerpt: str, region_label: str) -> str:
    return (
        f"当前图幅：{region_label}\n\n"
        f"用户说：\n{task.strip()}\n\n"
        f"图纸索引摘录（单位 mm）：\n{excerpt.strip()}\n"
    )


def plan_has_edits(plan: Optional[dict[str, Any]]) -> bool:
    if not plan or not isinstance(plan, dict):
        return False
    for key in ("inserts", "pipes", "texts", "deletes", "replaces", "moves"):
        val = plan.get(key)
        if isinstance(val, list) and any(item for item in val):
            return True
    return False


def is_edit_plan(plan: Optional[dict[str, Any]]) -> bool:
    if not plan or not isinstance(plan, dict):
        return False
    mode = str(plan.get("mode") or "").strip().lower()
    if mode in {"answer", "critique", "reject"} and not plan_has_edits(plan):
        return False
    if mode == "plan" or plan_has_edits(plan):
        return True
    return False


def reply_text(plan: Optional[dict[str, Any]], raw_text: str) -> str:
    if plan and isinstance(plan, dict):
        val = plan.get("reply")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return (raw_text or "").strip()


def summarize_plan(plan: Optional[dict[str, Any]]) -> str:
    """One-line plan summary for chat history. No coordinates or full arrays."""
    if not plan or not isinstance(plan, dict):
        return ""
    labels = (
        ("replaces", "替换"),
        ("inserts", "新增"),
        ("deletes", "删除"),
        ("moves", "移动"),
        ("pipes", "管线"),
        ("texts", "文字"),
    )
    bits = []
    for key, label in labels:
        val = plan.get(key)
        if isinstance(val, list) and val:
            bits.append(f"{label}{len(val)}处")
    if not bits:
        return ""
    where = (plan.get("sheet") or plan.get("room") or "").strip()
    suffix = f"，{where}" if where else "，本图幅"
    return "当时方案：" + "、".join(bits) + suffix + "。"


def build_history_messages(
    messages: list[dict[str, Any]],
    *,
    max_turns: int = 8,
    max_chars: int = 3000,
) -> list[dict[str, str]]:
    """Compact prior turns for the model. The current user turn is not included."""
    if not messages:
        return []
    picked = [m for m in messages if m.get("role") in {"user", "assistant"}]
    picked = picked[-(max_turns * 2) :]
    out: list[dict[str, str]] = []
    for m in picked:
        role = m["role"]
        content = (m.get("text") or "").strip()
        if role == "assistant":
            plan = m.get("plan") if isinstance(m.get("plan"), dict) else None
            summary = summarize_plan(plan)
            if summary:
                content = (content + "\n" + summary).strip()
        if len(content) > 800:
            content = content[:780] + "…"
        if content:
            out.append({"role": role, "content": content})
    while out and sum(len(m["content"]) for m in out) > max_chars:
        out.pop(0)
    return out


import re

_APPLY_RE = re.compile(
    r"^(请)?(直接)?(确认|应用|按这个|就按这个|可以改了|改吧|照这个改|按方案改|按这个改)(改图|方案|吧|了|到工作图|到图纸)?。?$"
)
_UNDO_RE = re.compile(
    r"^(请)?(撤回|撤销|还原)(上一步|上次|这次|这个|该)?(修改|改动|方案)?。?$"
)


def detect_intent(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None
    if _UNDO_RE.match(t):
        return "undo"
    if _APPLY_RE.match(t):
        return "apply"
    return None


_SHORT_FOLLOWUP_RE = re.compile(
    r"^\s*(?:"
    r"(?:进度)?怎么样了|"
    r"(?:进度)?怎样了|"
    r"如何了|"
    r"(?:现在)?(?:做完了吗|改完了吗|改了吗|做好了吗|弄好了吗|做了吗)|"
    r"(?:你)?(?:现在)?(?:到底)?(?:能不能做|能做吗|可以做吗|会做吗|做得到吗)|"
    r"(?:你现在)?(?:给)?改了吗|"
    r"可以的?|"
    r"好的|"
    r"行|"
    r"对|"
    r"嗯|"
    r"继续|"
    r"那就这样|"
    r"就这样|"
    r"就按这个(?:调整|改|做|来)?|"
    r"按你说的(?:来|做|改)?|"
    r"确认"
    r")[。．\.？\?！!]*\s*$"
)
_PROGRESS_OR_CAPABILITY_RE = re.compile(
    r"(怎么样了|怎样了|如何了|做完了吗|改完了吗|改了吗|做好了吗|进度怎么样|"
    r"能不能做|能做吗|可以做吗|做得到吗|你到底能不能)"
)
_OFFTOPIC_HINT_RE = re.compile(
    r"(天气|新闻|股市|股价|诗歌|笑话|政治|看病|写代码|Python|JavaScript)"
)
_AGREE_RE = re.compile(
    r"^\s*(可以的?|好的|行|对|嗯|就这样|那就这样|就按这个(?:调整|改|做|来)?|"
    r"按你说的(?:来|做|改)?|按这个来)[。．\.！!]*\s*$"
)
_PROGRESS_RE = re.compile(
    r"(怎么样了|怎样了|如何了|做完了吗|改完了吗|改了吗|做好了吗|弄好了吗|进度)"
)


def is_session_followup(text: str) -> bool:
    """Short in-session status, capability, or confirmation — not off-topic chitchat."""
    t = (text or "").strip()
    if not t:
        return False
    if _OFFTOPIC_HINT_RE.search(t) and not _AGREE_RE.match(t):
        return False
    if _SHORT_FOLLOWUP_RE.match(t):
        return True
    if len(t) <= 80 and _PROGRESS_OR_CAPABILITY_RE.search(t):
        return True
    if is_visibility_followup(t):
        return True
    return False


_VISIBILITY_RE = re.compile(
    r"("
    r"没看到|没看见|看不到|看不见|"
    r"预览(?:里|中)?没(?:有)?变化|没有变化|"
    r"还是三个台盆|还是原来的|"
    r"还在渲染|是不是在渲染|还在出图|是没做还是(?:在)?渲染"
    r")"
)


def is_visibility_followup(text: str) -> bool:
    """User cannot see the preview change, or asks if it is still rendering."""
    t = (text or "").strip()
    if not t or _OFFTOPIC_HINT_RE.search(t):
        return False
    if len(t) > 120:
        return False
    return bool(_VISIBILITY_RE.search(t))


def is_short_agreement(text: str) -> bool:
    return bool(_AGREE_RE.match((text or "").strip()))


def is_progress_question(text: str) -> bool:
    t = (text or "").strip()
    if not t or _OFFTOPIC_HINT_RE.search(t):
        return False
    if len(t) > 80:
        return False
    return bool(_PROGRESS_RE.search(t))


def awaiting_plan_after_suggestion(messages: list[dict[str, Any]]) -> bool:
    """True if the latest substantive assistant turn proposed a change but left no edit arrays."""
    for m in reversed(messages or []):
        if m.get("role") != "assistant":
            continue
        mode = str(m.get("mode") or "").strip().lower()
        plan = m.get("plan") if isinstance(m.get("plan"), dict) else {}
        if plan_has_edits(plan) or mode == "plan":
            return False
        if mode == "reject":
            continue
        text = (m.get("text") or "").strip()
        if mode == "critique":
            return True
        if mode == "answer" and any(
            s in text for s in ("可以吗", "确认后再", "您确认后", "等您确认", "就按这个", "就按")
        ):
            return True
        if mode == "answer":
            continue
        return False
    return False


def plan_nudge_for(text: str, messages: list[dict[str, Any]]) -> Optional[str]:
    """When the user agrees to a suggestion or asks if it is done, require a real plan."""
    if detect_intent(text):
        return None
    if not awaiting_plan_after_suggestion(messages):
        return None
    if is_short_agreement(text) or is_progress_question(text) or is_visibility_followup(text):
        return (
            "系统提示：用户已确认上一轮改进建议，或在追问这项改图是否完成、预览有没有变化。"
            "请立即给出 mode=plan 的改图方案并填写完整改动数组，不要只口头答应，也不要声称已经写入图纸。"
        )
    return None


def _fixture_label(block: str) -> str:
    name = (block or "").strip()
    if not name:
        return ""
    try:
        from .indexing import KNOWN_TYPOLOGIES

        typo = KNOWN_TYPOLOGIES.get(name) or {}
        label = str(typo.get("display_name") or "").strip()
        if label:
            return label
        aliases = typo.get("aliases") or []
        if aliases:
            return str(aliases[0]).strip()
    except Exception:
        pass
    return ""


def _plan_room(plan: dict[str, Any]) -> str:
    room = str((plan or {}).get("room") or "").strip()
    if room:
        return room
    blob = " ".join(
        str((plan or {}).get(k) or "") for k in ("reply", "analysis", "sheet")
    )
    for token in ("女卫", "男卫", "卫生间", "盥洗"):
        if token in blob:
            return token
    return ""


def _relative_place(xy, region: Optional[dict[str, Any]]) -> str:
    if not xy or len(xy) < 2:
        return ""
    bbox = (region or {}).get("bbox") if isinstance(region, dict) else None
    if not bbox or len(bbox) != 4:
        return ""
    try:
        x, y = float(xy[0]), float(xy[1])
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return ""
    h = max(y1 - y0, 1.0)
    w = max(x1 - x0, 1.0)
    parts: list[str] = []
    if y <= y0 + h * 0.4:
        parts.append("下方")
    elif y >= y0 + h * 0.6:
        parts.append("上方")
    if x <= x0 + w * 0.25:
        parts.append("左侧")
    elif x >= x0 + w * 0.75:
        parts.append("右侧")
    return "".join(parts)


def _pending_fixture(plan: Optional[dict[str, Any]]) -> tuple[str, Optional[list]]:
    if not plan or not isinstance(plan, dict):
        return "", None
    for key in ("inserts", "replaces"):
        items = plan.get(key)
        if not isinstance(items, list) or not items:
            continue
        item = items[-1] if isinstance(items[-1], dict) else {}
        block = str(item.get("block") or "").strip()
        xy = item.get("xy") or item.get("to") or item.get("from_xy")
        return block, xy if isinstance(xy, (list, tuple)) and len(xy) >= 2 else None
    for key in ("moves", "deletes"):
        items = plan.get(key)
        if not isinstance(items, list) or not items:
            continue
        item = items[-1] if isinstance(items[-1], dict) else {}
        block = str(item.get("block") or item.get("from_block") or "").strip()
        xy = item.get("to") or item.get("xy") or item.get("from") or item.get("from_xy")
        return block, xy if isinstance(xy, (list, tuple)) and len(xy) >= 2 else None
    return "", None


def visibility_followup_reply(pending: dict[str, Any], region: Optional[dict[str, Any]] = None) -> str:
    """Brief Chinese pointer to the already-pending change on the right-hand view."""
    plan = pending.get("plan") if isinstance(pending, dict) else None
    plan = plan if isinstance(plan, dict) else {}
    room = _plan_room(plan)
    block, xy = _pending_fixture(plan)
    label = _fixture_label(block)
    place = _relative_place(xy, region if region is not None else pending)
    where = f"{room}{place}" if (room or place) else "刚才改动的位置"
    if label:
        if plan.get("inserts"):
            pointer = f"{where}的新{label}"
        elif plan.get("replaces") or plan.get("moves"):
            pointer = f"{where}改过的{label}"
        else:
            pointer = f"{where}的{label}"
        return f"方案已经在右侧局部预览里了，请看{pointer}。确认后才会写入工作图。"
    return "方案已经在右侧局部预览里了，改动就在刚才那一处。确认后才会写入工作图。"


def visibility_short_circuit(
    text: str,
    pending: Optional[dict[str, Any]],
    region: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """If the user cannot see an already-pending plan, replay it instead of calling the model."""
    if not is_visibility_followup(text):
        return None
    if not pending or not plan_has_edits(pending.get("plan") if isinstance(pending, dict) else None):
        return None
    pregion = pending.get("region") if isinstance(pending, dict) else None
    region_id = (region or {}).get("id") if isinstance(region, dict) else None
    if pregion and region_id and pregion != region_id:
        return None
    return {
        "text": visibility_followup_reply(pending, region),
        "resend": True,
        "plan": pending.get("plan"),
        "message_id": pending.get("message_id"),
        "region": pregion,
    }
