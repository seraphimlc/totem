from __future__ import annotations

from contextlib import asynccontextmanager

import asyncio
import base64
import json
import logging
import re
import traceback
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import apply, archive_queue, auth, cad_render, config, decision, init_proj, library, maas, placement, projects, scene
from .prompts import (
    SYSTEM_PROMPT,
    build_history_messages,
    build_repair_message,
    build_user_message,
    detect_intent,
    is_edit_plan,
    plan_has_edits,
    plan_nudge_for,
    reply_text,
    visibility_short_circuit,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tutan")

# 方案出图前最多回喂模型修一次坐标。再多就会拖慢对话，也让模型反复改坏。
MAX_PLAN_REPAIRS = 1

# Never log secrets. Mention only whether they exist.
log.info(
    "图腾 starting (maas_key=%s, auth=%s, jev=%s, base_host=%s)",
    "yes" if config.MAAS_KEY else "no",
    "yes" if config.auth_configured() else "no",
    "yes" if config.decision_configured() else "no",
    config.MAAS_BASE.split("/")[2] if "://" in config.MAAS_BASE else "(unset)",
)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    archive_queue.start_worker()
    init_proj.resume_unfinished()
    try:
        yield
    finally:
        init_proj.mark_running_interrupted()
        archive_queue.stop_worker()


app = FastAPI(title="图腾", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


_PUBLIC_API = {"/api/health", "/api/login", "/api/logout"}
# Overlay ids become file names under var/overlays/<project>/; keep them tame.
_OVERLAY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")


@app.middleware("http")
async def require_session_on_api(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in _PUBLIC_API:
        if not auth.current_user(request):
            return JSONResponse({"detail": "请先登录后再使用工作台。"}, status_code=401)
    return await call_next(request)



class LoginBody(BaseModel):
    username: str = Field(default="", max_length=80)
    password: str = Field(default="", max_length=200)


class ChatBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=8000)
    model: str = Field(default=config.DEFAULT_MODEL)
    region: Optional[str] = None


def _user(request: Request) -> str:
    return auth.require_user(request)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "name": "图腾",
        "models": [m["id"] for m in config.MODELS],
        "auth_configured": config.auth_configured(),
        "maas_configured": config.maas_configured(),
        "decision_configured": config.decision_configured(),
    }


@app.post("/api/login")
async def login(request: Request, response: Response):
    content_type = request.headers.get("content-type", "")
    username = password = ""
    wants_html = False
    if "application/json" in content_type:
        try:
            body = LoginBody.model_validate(await request.json())
        except Exception:
            raise HTTPException(status_code=400, detail="请提交用户名和密码。")
        username, password = body.username.strip(), body.password
    else:
        form = await request.form()
        username = str(form.get("username") or "").strip()
        password = str(form.get("password") or "")
        wants_html = True

    if not config.auth_configured():
        raise HTTPException(status_code=503, detail="工作台还没有配置登录账号。请在服务器的 .env 里设置 ACCESS_USERNAME、ACCESS_PASSWORD 和 SESSION_SECRET 后重启。")
    if not username or not password:
        raise HTTPException(status_code=400, detail="请填写用户名和密码后再进入。")
    if not auth.credentials_ok(username, password):
        raise HTTPException(status_code=401, detail="用户名或密码不对，请检查后再试。")

    token = auth.issue_session(username)
    # For form posts we redirect; for fetch we return JSON. Cookie is set on the response either way.
    if wants_html:
        from fastapi.responses import RedirectResponse
        out = RedirectResponse(url="/", status_code=303)
        auth.set_session_cookie(out, token)
        return out
    auth.set_session_cookie(response, token)
    return {"ok": True, "username": username}


@app.post("/api/logout")
def logout(response: Response, request: Request):
    auth.clear_session_cookie(response)
    return {"ok": True}


@app.get("/api/me")
def me(user: str = Depends(_user)):
    return {"username": user}


@app.get("/api/models")
def models(user: str = Depends(_user)):
    return {"models": list(config.MODELS), "default": config.DEFAULT_MODEL}


@app.get("/api/projects")
def api_projects(user: str = Depends(_user)):
    return {"projects": projects.list_projects()}


def _proj_or_404(project_id: str, *, need_file: bool = True, need_ready: bool = False) -> dict[str, Any]:
    try:
        proj = projects.resolve_project(project_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="没有这个会话。")
    root = Path(proj["root"]) if proj.get("root") else None
    kind = proj.get("kind") or "builtin"
    if need_file and not proj["available"]:
        if not (kind == "session" and root and root.is_dir()):
            raise HTTPException(status_code=404, detail="这个会话的图纸还没准备好，请检查目录和 CAD_DATA_DIR。")
    if need_ready and not init_proj.is_ready(proj):
        init_proj.start(proj)
        raise HTTPException(status_code=409, detail="图纸还在准备，准备完成后再开始对话。")
    return proj


def _init_payload(proj: dict[str, Any]) -> dict[str, Any]:
    try:
        return init_proj.public_status(proj)
    except Exception:
        return {"state": "error", "ready": False, "message": "读不到准备状态。"}


def _apply_payload(proj: dict[str, Any]) -> dict[str, Any]:
    try:
        return apply.status(proj)
    except Exception:
        return {"dirty": False, "can_undo": False, "pending_plan": None, "official_untouched": True}


@app.post("/api/projects/upload")
async def api_upload(
    file: UploadFile = File(...),
    name: Optional[str] = Form(default=None),
    user: str = Depends(_user),
):
    filename = file.filename or "drawing.dxf"
    data = await file.read()
    try:
        proj = await asyncio.to_thread(init_proj.create_session_from_upload, filename, data, name=name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("upload failed")
        raise HTTPException(status_code=500, detail="上传没有成功，请稍后重试。")
    return {
        "id": proj["id"],
        "name": proj["name"],
        "kind": "session",
        "ready": False,
        "init": _init_payload(proj),
    }


@app.get("/api/projects/{project_id}")
def api_project(project_id: str, user: str = Depends(_user)):
    proj = _proj_or_404(project_id, need_file=False)
    init_info = _init_payload(proj)
    if init_info.get("state") in {"pending", "running", "error"} or not init_info.get("ready"):
        if Path(proj["root"]).is_dir() if proj.get("root") else False:
            init_proj.start(proj)
            init_info = _init_payload(proj)
    regions = []
    default_id = None
    inventory = None
    if proj["available"] and projects.load_index(proj):
        try:
            regions = projects.iter_regions(proj)
            default_id = projects.default_region_id(proj, regions)
            if default_id:
                inventory = projects.region_inventory(proj, next(r for r in regions if r["id"] == default_id))
        except Exception:
            regions = []
    return {
        "id": proj["id"],
        "name": proj["name"],
        "kind": proj.get("kind") or "builtin",
        "note": proj.get("note") or "",
        "deliverable": proj.get("deliverable"),
        "available": proj["available"],
        "ready": bool(init_info.get("ready")),
        "default_region": default_id,
        "regions": regions,
        "init": init_info,
        "apply": _apply_payload(proj) if proj["available"] else None,
        "inventory": inventory,
    }


@app.get("/api/projects/{project_id}/chat")
def api_chat_history(project_id: str, user: str = Depends(_user)):
    proj = _proj_or_404(project_id)
    return projects.read_chat(proj)


@app.get("/api/projects/{project_id}/preview")
def api_preview(
    project_id: str,
    region: Optional[str] = Query(default=None),
    overlay: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    user: str = Depends(_user),
):
    if overlay and not _OVERLAY_ID_RE.fullmatch(overlay):
        raise HTTPException(status_code=400, detail="叠图编号不合法。")
    proj = _proj_or_404(project_id, need_ready=True)
    if overlay:
        path = cad_render.overlay_path_for(project_id, overlay)
        if path.is_file() and path.stat().st_size > 0:
            return FileResponse(path, media_type="image/png", filename=f"{project_id}-{overlay}.png")
        # Fall through to the current drawing if the plan preview is missing.
    try:
        region_info = projects.get_region(proj, region)
    except KeyError:
        raise HTTPException(status_code=404, detail="没有这个图幅或分区，请从列表里重新选一个。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        if (source or "").strip().lower() in {"official", "source", "original"}:
            dxf = apply.original_preview_dxf(proj)
        else:
            dxf = apply.current_dxf(proj)
        path = cad_render.base_preview(
            Path(dxf),
            region_info["bbox"],
            encoding=proj.get("encoding") or "utf-8",
        )
    except Exception:
        log.exception("preview failed for %s %s", project_id, region)
        raise HTTPException(status_code=500, detail="这张图没能画出来。请换一个分区，或检查服务器上的中文字体和交付 DXF。")
    return FileResponse(path, media_type="image/png", filename=f"{project_id}-preview.png")


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")


async def _await_with_heartbeats(fn, *args, interval: float = 5.0, **kwargs):
    """Run a blocking function off the event loop, yielding SSE heartbeats until it finishes."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        while not task.done():
            done, _pending = await asyncio.wait({task}, timeout=interval)
            if not done:
                yield _sse({"type": "heartbeat"})
        yield task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise


def plan_chat_events(proj: dict[str, Any], user_msg: dict[str, Any], assistant: dict[str, Any],
                     region_info: dict[str, Any], *, render: bool = True, renderer=None):
    """Persist the assistant reply, then yield done before any overlay file is written.

    After `done`, optionally render the post-edit preview and yield a `preview` event.
    `renderer(proj, plan, region, dest)` can be injected in tests.
    """
    assistant = dict(assistant)
    assistant_id = assistant.get("id") or projects.new_id()
    assistant["id"] = assistant_id
    plan = assistant.get("plan")
    need = bool(is_edit_plan(plan) and plan_has_edits(plan))
    assistant["overlay"] = None
    assistant["preview_pending"] = need
    projects.append_chat(proj, [user_msg, assistant])
    yield {"type": "done", "message": dict(assistant), "preview_pending": need}
    if not need or not render:
        return
    dest = cad_render.overlay_path_for(str(proj.get("id") or "project"), assistant_id)
    try:
        fn = renderer or apply.render_plan_preview
        fn(proj, plan, region_info, dest)
    except Exception:
        log.exception("plan preview failed")
        projects.update_chat_message(proj, assistant_id, {"preview_pending": False})
        yield {
            "type": "status",
            "text": "方案已经拿到，但右侧预览没能生成改后图面，仍显示当前图纸。",
        }
        return
    projects.update_chat_message(
        proj,
        assistant_id,
        {"overlay": assistant_id, "preview_pending": False},
    )
    yield {"type": "preview", "overlay": assistant_id, "message_id": assistant_id}


async def _offload(fn, *args, **kwargs):
    """Run a sync function in a worker thread so the event loop stays free."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _check_plan_coords(proj: dict[str, Any], plan: dict[str, Any], region_info: dict[str, Any]):
    """在内存工作图上跑坐标校验。只读 DXF，不写盘、不出快照。"""
    doc = apply._read_doc(Path(apply.current_dxf(proj)), proj.get("encoding") or "utf-8")
    return placement.check_plan(
        doc,
        plan,
        bbox=(region_info or {}).get("bbox"),
        region=region_info,
    )


def _error_issues(issues) -> list[dict[str, Any]]:
    return [i for i in (issues or []) if isinstance(i, dict) and i.get("severity") == "error"]


def _xy_changes(old: dict[str, Any], new: dict[str, Any]) -> int:
    """数一数修复后被挪了坐标的项，用于给用户一句可核对的提示。"""
    changed = 0
    pairs = (("inserts", "xy"), ("replaces", "xy"), ("moves", "to"))
    for key, field in pairs:
        before = old.get(key) or []
        after = new.get(key) or []
        for a, b in zip(before, after):
            if not isinstance(a, dict) or not isinstance(b, dict):
                continue
            pa, pb = a.get(field), b.get(field)
            if not pa or not pb:
                continue
            try:
                if abs(float(pa[0]) - float(pb[0])) > 1.0 or abs(float(pa[1]) - float(pb[1])) > 1.0:
                    changed += 1
            except (TypeError, ValueError, IndexError):
                continue
    return changed


async def _collect_completion(**kwargs):
    """收完一次模型输出，但不把 delta 交给前端；等待期间只发心跳。"""
    chunks: list[str] = []

    async def _drain():
        async for ev in maas.complete(**kwargs):
            if ev.get("type") == "delta":
                chunks.append(ev.get("text") or "")
            elif ev.get("type") == "end":
                return

    task = asyncio.create_task(_drain())
    try:
        while True:
            done, _pending = await asyncio.wait({task}, timeout=5.0)
            if done:
                break
            yield {"type": "heartbeat"}
        task.result()
    except asyncio.CancelledError:
        task.cancel()
        raise
    yield {"type": "text", "text": "".join(chunks).strip()}


def _geometry_hint(corrected: int, issues: list[dict[str, Any]]) -> str:
    """方案后面追加的一句短提示；没发生校正也没有值得说的项时返回空串。"""
    if corrected > 0:
        return f"其中 {corrected} 处坐标按图面几何做过校正，可在右侧预览里核对。"
    # 块尺寸缺失只是「核不了」，不值得打扰用户。
    real = [
        i for i in (issues or [])
        if isinstance(i, dict) and (i.get("severity") == "error" or i.get("code") != "missing_size")
    ]
    if real:
        return f"有 {len(real)} 处坐标需要留意，可在右侧预览里核对。"
    return ""


@app.post("/api/projects/{project_id}/chat")
async def api_chat(project_id: str, body: ChatBody, user: str = Depends(_user)):
    proj = await _offload(_proj_or_404, project_id, need_file=True, need_ready=True)
    model = body.model or config.DEFAULT_MODEL
    if model not in config.ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="请选择 Tifa-6.1-sol 或 Tifa-6-astra。")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="请先输入问题或改图要求。")
    try:
        region_info = await _offload(projects.get_region, proj, body.region)
    except KeyError:
        raise HTTPException(status_code=404, detail="没有这个图幅或分区，请从右侧列表里重新选一个。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    user_msg = {
        "id": projects.new_id(),
        "role": "user",
        "text": text,
        "ts": projects.now_iso(),
        "region": region_info["id"],
        "model": model,
    }
    # Persist the user turn immediately so switching sessions or a dropped
    # SSE connection cannot erase what they just sent.
    await _offload(projects.append_chat, proj, [user_msg])

    async def gen():
        assistant_id = projects.new_id()
        saved_assistant = False
        try:
            yield _sse({"type": "ack", "user": user_msg, "assistant_id": assistant_id})
            intent = detect_intent(text)
            if intent:
                result = assistant = None
                try:
                    async for item in _await_with_heartbeats(
                        _handle_intent, proj, intent, user_msg, assistant_id, region_info
                    ):
                        if isinstance(item, (bytes, bytearray)):
                            yield item
                        else:
                            result, assistant = item
                except ValueError as exc:
                    yield _sse({"type": "error", "message": str(exc)})
                    return
                await _offload(projects.append_chat, proj, [user_msg, assistant])
                saved_assistant = True
                yield _sse({"type": "done", "message": assistant, "apply": result})
                # Skip pushing a full vector scene over SSE; the crop preview is enough.
                return
            gate = await decision.classify_off_topic(
                text,
                drawing_name=str(proj.get("name") or ""),
                region_label=projects.region_label(region_info),
                session_id=proj.get("id"),
            )
            if gate.get("off_topic"):
                plan = decision.reject_plan()
                assistant = {
                    "id": assistant_id,
                    "role": "assistant",
                    "text": plan["reply"],
                    "ts": projects.now_iso(),
                    "model": "system",
                    "region": region_info["id"],
                    "mode": "reject",
                    "plan": plan,
                    "overlay": None,
                    "gate": "jev",
                }
                await _offload(projects.append_chat, proj, [user_msg, assistant])
                saved_assistant = True
                yield _sse({"type": "done", "message": assistant})
                return
            pending = await _offload(apply.last_pending_plan, proj)
            replay = visibility_short_circuit(text, pending, region_info)
            if replay:
                overlay_id = None
                mid = replay.get("message_id")
                if mid:
                    overlay_path = cad_render.overlay_path_for(project_id, mid)
                    if overlay_path.is_file() and overlay_path.stat().st_size > 0:
                        overlay_id = mid
                need_crop = overlay_id is None
                assistant = {
                    "id": assistant_id,
                    "role": "assistant",
                    "text": replay["text"],
                    "ts": projects.now_iso(),
                    "model": "system",
                    "region": region_info["id"],
                    "mode": "answer",
                    "plan": None,
                    "overlay": overlay_id,
                    "visibility_followup": True,
                    "preview_pending": need_crop,
                }
                await _offload(projects.append_chat, proj, [user_msg, assistant])
                saved_assistant = True
                yield _sse({"type": "done", "message": dict(assistant), "preview_pending": need_crop})
                if overlay_id:
                    yield _sse({"type": "preview", "overlay": overlay_id, "message_id": mid})
                elif need_crop and mid:
                    yield _sse({"type": "status", "text": "方案已出，正在生成改后预览…"})
                    error = None

                    def _render_replay():
                        apply.render_plan_preview(
                            proj,
                            replay["plan"],
                            region_info,
                            cad_render.overlay_path_for(project_id, mid),
                        )

                    try:
                        async for item in _await_with_heartbeats(_render_replay):
                            if isinstance(item, (bytes, bytearray)):
                                yield item
                    except Exception:
                        log.exception("visibility follow-up crop failed")
                        error = True
                    if error:
                        await _offload(projects.update_chat_message, proj, assistant_id, {"preview_pending": False})
                        if mid:
                            await _offload(projects.update_chat_message, proj, mid, {"preview_pending": False})
                    else:
                        await _offload(
                            projects.update_chat_message,
                            proj, assistant_id, {"overlay": mid, "preview_pending": False},
                        )
                        if mid:
                            await _offload(
                                projects.update_chat_message,
                                proj, mid, {"overlay": mid, "preview_pending": False},
                            )
                        yield _sse({"type": "preview", "overlay": mid, "message_id": mid})
                return

            def _prepare_prompt():
                excerpt = projects.excerpt_index(proj, region_info, text)
                working = apply.excerpt_working(proj)
                if working:
                    excerpt = f"{excerpt}\n\n{working}"
                lib_ex = library.excerpt(text)
                if lib_ex:
                    excerpt = f"{excerpt}\n\n{lib_ex}"
                user_payload = build_user_message(text, excerpt, projects.region_label(region_info))
                prior = projects.read_chat(proj).get("messages") or []
                nudge = plan_nudge_for(text, prior)
                if nudge:
                    user_payload = f"{user_payload}\n\n{nudge}"
                history = build_history_messages(prior)
                return user_payload, history

            user_payload = history = None
            async for item in _await_with_heartbeats(_prepare_prompt):
                if isinstance(item, (bytes, bytearray)):
                    yield item
                else:
                    user_payload, history = item
            image_url = None
            spec = next(m for m in config.MODELS if m["id"] == model)
            # Astra always gets a crop; Sol tries, then falls back if rejected.
            want_image = spec.get("image") in {"yes", "fallback"}
            if want_image:
                try:
                    raw = None

                    def _load_base():
                        preview = cad_render.base_preview(
                            Path(apply.current_dxf(proj)),
                            region_info["bbox"],
                            encoding=proj.get("encoding") or "utf-8",
                        )
                        data = Path(preview).read_bytes()
                        # keep request size reasonable
                        if len(data) > 1_200_000:
                            from PIL import Image
                            import io
                            im = Image.open(io.BytesIO(data))
                            im.thumbnail((1600, 1600))
                            buf = io.BytesIO()
                            im.save(buf, format="PNG", optimize=True)
                            data = buf.getvalue()
                        return data

                    async for item in _await_with_heartbeats(_load_base):
                        if isinstance(item, (bytes, bytearray)) and item.startswith(b"data: "):
                            yield item
                        else:
                            raw = item
                    image_url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
                    yield _sse({"type": "status", "text": "已带上当前图面摘录和局部预览。"})
                except Exception:
                    log.exception("preview for prompt failed")
                    image_url = None
                    yield _sse({"type": "status", "text": "局部预览没能生成，改为只发送文字摘录。"})

            collected: list[str] = []

            async def run(with_image: Optional[str]):
                collected.clear()
                async for ev in maas.complete(
                    model=model,
                    system=SYSTEM_PROMPT,
                    user_text=user_payload,
                    image_data_url=with_image,
                    history=history,
                ):
                    if ev["type"] == "delta":
                        collected.append(ev["text"])
                        yield ev["text"]
                    elif ev["type"] == "end":
                        return

            used_image = bool(image_url)
            try:
                async for chunk in run(image_url):
                    yield _sse({"type": "delta", "text": chunk})
            except maas.MaasError as exc:
                if used_image and exc.retryable_without_image:
                    yield _sse({"type": "status", "text": "这个模型不接受图片，已改用文字摘录重新请求。"})
                    used_image = False
                    async for chunk in run(None):
                        yield _sse({"type": "delta", "text": chunk})
                else:
                    raise

            raw_text = "".join(collected).strip()
            plan = maas.extract_assistant_payload(raw_text)
            edit_plan = is_edit_plan(plan)
            corrected = 0
            issues: list[dict[str, Any]] = []

            # 方案出完先按图面几何校一遍坐标；有 error 就回喂模型只修坐标，
            # 中间结果不推给前端，最多修 MAX_PLAN_REPAIRS 轮，出错就用原方案。
            if edit_plan and plan_has_edits(plan):
                try:
                    async for item in _await_with_heartbeats(_check_plan_coords, proj, plan, region_info):
                        if isinstance(item, (bytes, bytearray)):
                            yield item
                        else:
                            issues = item
                except Exception:
                    log.exception("plan coordinate check failed")
                    issues = []
                errors = _error_issues(issues)
                repairs = 0
                while errors and repairs < MAX_PLAN_REPAIRS:
                    repairs += 1
                    yield _sse({"type": "status", "text": "正在按图面校正坐标…"})
                    try:
                        repair_payload = f"{user_payload}\n\n{build_repair_message(errors, plan)}"
                        repaired_raw = ""
                        async for item in _collect_completion(
                            model=model,
                            system=SYSTEM_PROMPT,
                            user_text=repair_payload,
                            image_data_url=None,
                            history=history,
                        ):
                            if isinstance(item, dict) and item.get("type") == "text":
                                repaired_raw = item.get("text") or ""
                            elif isinstance(item, dict):
                                yield _sse(item)
                        candidate = maas.extract_assistant_payload(repaired_raw)
                        if not (candidate and is_edit_plan(candidate)):
                            break
                        candidate_issues = await _offload(_check_plan_coords, proj, candidate, region_info)
                        if len(_error_issues(candidate_issues)) >= len(errors):
                            break
                        corrected = _xy_changes(plan, candidate)
                        plan = candidate
                        raw_text = repaired_raw
                        edit_plan = True
                        issues = candidate_issues
                        errors = _error_issues(issues)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("plan coordinate repair failed, keep original plan")
                        break
            if edit_plan and isinstance(plan, dict):
                plan["issues"] = issues

            reply = reply_text(plan, raw_text) or "这次没有给出完整回答，请换一种说法再试，或换一个模型。"
            hint = _geometry_hint(corrected, issues)
            if hint and edit_plan:
                reply = f"{reply}{hint}"
            assistant = {
                "id": assistant_id,
                "role": "assistant",
                "text": reply,
                "ts": projects.now_iso(),
                "model": model,
                "region": region_info["id"],
                "mode": (plan.get("mode") if plan else None) or ("plan" if edit_plan else "answer"),
                "plan": plan,
                "overlay": None,
                "raw": raw_text if plan else None,
                "used_image": used_image,
            }
            saved_assistant = True

            def _persist_plan():
                return list(plan_chat_events(proj, user_msg, assistant, region_info, render=False))

            events = None
            async for item in _await_with_heartbeats(_persist_plan):
                if isinstance(item, (bytes, bytearray)):
                    yield item
                else:
                    events = item
            for ev in events or []:
                yield _sse(ev)
                if ev.get("type") == "done" and ev.get("preview_pending"):
                    yield _sse({"type": "status", "text": "方案已出，正在生成改后预览…"})
                    result = None
                    error = None

                    def _render():
                        apply.render_plan_preview(proj, plan, region_info, cad_render.overlay_path_for(project_id, assistant_id))

                    try:
                        async for item in _await_with_heartbeats(_render):
                            if isinstance(item, (bytes, bytearray)):
                                yield item
                            else:
                                result = item
                    except Exception:
                        log.exception("plan preview failed")
                        error = True
                    if error:
                        await _offload(projects.update_chat_message, proj, assistant_id, {"preview_pending": False})
                        yield _sse({"type": "status", "text": "方案已经拿到，但右侧预览没能生成改后图面，仍显示当前图纸。"})
                    else:
                        await _offload(
                            projects.update_chat_message,
                            proj,
                            assistant_id,
                            {"overlay": assistant_id, "preview_pending": False},
                        )
                        yield _sse({"type": "preview", "overlay": assistant_id, "message_id": assistant_id})
        except asyncio.CancelledError:
            if not saved_assistant:
                try:
                    await asyncio.shield(_offload(
                        projects.append_chat,
                        proj,
                        [{
                            "id": assistant_id,
                            "role": "assistant",
                            "text": "回复还没完成就中断了。你刚才那句话还在，请再发一次，或换个说法继续。",
                            "ts": projects.now_iso(),
                            "model": "system",
                            "region": region_info["id"],
                            "mode": "answer",
                            "interrupted": True,
                        }],
                    ))
                except Exception:
                    log.exception("failed to persist interrupted assistant turn")
            raise
        except maas.MaasError as exc:
            log.warning("maas error: %s", exc)
            yield _sse({"type": "error", "message": str(exc)})
        except Exception:
            log.exception("chat failed")
            yield _sse({"type": "error", "message": "回复时出了问题。请稍后重试；若反复失败，查看服务器日志。"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )




class ApplyBody(BaseModel):
    message_id: Optional[str] = Field(default=None)
    region: Optional[str] = Field(default=None)


def _handle_intent(proj, intent: str, user_msg: dict[str, Any], assistant_id: str, region_info: dict[str, Any]):
    if intent == "apply":
        result = _do_apply(proj, message_id=None, region=region_info)
        text = (
            f"已把方案写到当前图纸新图层「{result.get('layer')}」。"
            f"成功 {result.get('ok_count')} 处"
            + (f"，有 {result.get('failed_count')} 处没写上" if result.get("failed_count") else "")
            + "。可以撤回上一步。"
        )
    elif intent == "undo":
        result = apply.undo_last(proj)
        if result.get("restored_official"):
            text = "已撤回上一步，当前图纸已恢复到改前状态。"
        else:
            text = "已撤回上一步，当前图纸已恢复到上一版。"
    else:
        raise ValueError("无法识别这个操作。")
    assistant = {
        "id": assistant_id,
        "role": "assistant",
        "text": text,
        "ts": projects.now_iso(),
        "model": "system",
        "region": region_info.get("id"),
        "mode": intent,
        "plan": None,
        "overlay": None,
        "apply": result,
    }
    return result, assistant


def _do_apply(proj: dict[str, Any], *, message_id: Optional[str], region: Optional[dict[str, Any]]):
    pending = apply.last_pending_plan(proj)
    if message_id:
        messages = projects.read_chat(proj).get("messages") or []
        pending = None
        for msg in messages:
            if msg.get("id") == message_id and msg.get("role") == "assistant":
                pending = {"message_id": msg["id"], "plan": msg.get("plan"), "region": msg.get("region"), "reply": msg.get("text") or ""}
                break
    if not pending or not plan_has_edits(pending.get("plan")):
        raise ValueError("没有待确认的改图方案。请先提出改图要求。")
    region_info = region
    if not region_info:
        try:
            region_info = projects.get_region(proj, pending.get("region"))
        except Exception:
            region_info = None
    return apply.apply_plan(
        proj,
        pending["plan"],
        region=region_info,
        message_id=pending.get("message_id"),
        user_text=pending.get("reply") or "",
    )


@app.get("/api/projects/{project_id}/init")
def api_init_status(project_id: str, user: str = Depends(_user)):
    proj = _proj_or_404(project_id, need_file=False)
    # Keep the job running if the user is watching this session.
    st = _init_payload(proj)
    if not st.get("ready") and st.get("state") != "error":
        init_proj.start(proj)
        st = _init_payload(proj)
    return st


@app.post("/api/projects/{project_id}/init")
def api_init_retry(
    project_id: str,
    force: bool = Query(False),
    user: str = Depends(_user),
):
    proj = _proj_or_404(project_id, need_file=False)
    return init_proj.start(proj, force=force)


@app.get("/api/projects/{project_id}/apply")
def api_apply_status(project_id: str, user: str = Depends(_user)):
    proj = _proj_or_404(project_id, need_file=False)
    return _apply_payload(proj)


@app.post("/api/projects/{project_id}/apply")
def api_apply(project_id: str, body: ApplyBody, user: str = Depends(_user)):
    proj = _proj_or_404(project_id, need_ready=True)
    try:
        region = projects.get_region(proj, body.region) if body.region else None
        result = _do_apply(proj, message_id=body.message_id, region=region)
    except KeyError:
        raise HTTPException(status_code=404, detail="没有这个图幅或分区。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("apply failed")
        raise HTTPException(status_code=500, detail="写入工作图时出了问题。")
    # Record a system note in the chat so the transcript stays complete.
    projects.append_chat(
        proj,
        [
            {
                "id": projects.new_id(),
                "role": "assistant",
                "text": (
                    f"已把方案写到当前图纸新图层「{result.get('layer')}」。"
                    f"成功 {result.get('ok_count')} 处。可以撤回。"
                ),
                "ts": projects.now_iso(),
                "model": "system",
                "mode": "apply",
                "apply": result,
            }
        ],
    )
    return result


@app.post("/api/projects/{project_id}/undo")
def api_undo(project_id: str, user: str = Depends(_user)):
    proj = _proj_or_404(project_id, need_ready=True)
    try:
        result = apply.undo_last(proj)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("undo failed")
        raise HTTPException(status_code=500, detail="撤回时出了问题。")
    text = "已撤回上一步，当前图纸已恢复到改前状态。" if result.get("restored_official") else "已撤回上一步，当前图纸已恢复到上一版。"
    projects.append_chat(
        proj,
        [
            {
                "id": projects.new_id(),
                "role": "assistant",
                "text": text,
                "ts": projects.now_iso(),
                "model": "system",
                "mode": "undo",
                "apply": result,
            }
        ],
    )
    return result


@app.get("/api/projects/{project_id}/download")
def api_download(
    project_id: str,
    source: str = Query(default="work"),
    user: str = Depends(_user),
):
    proj = _proj_or_404(project_id, need_ready=True)
    if source in {"original"}:
        path = apply.original_preview_dxf(proj)
        label = "原图"
    elif source in {"official", "source"}:
        path = apply.official_dxf(proj)
        label = "当前图纸"
    else:
        path = apply.current_dxf(proj)
        label = "工作图" if apply.is_dirty(proj) else "当前图纸"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="还没有可下载的图纸。")
    name = f"{proj['name']}-{label}.dxf"
    return FileResponse(path, media_type="application/dxf", filename=name)



@app.get("/api/projects/{project_id}/pdf")
def api_pdf(
    project_id: str,
    region: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    user: str = Depends(_user),
):
    proj = _proj_or_404(project_id, need_ready=True)
    try:
        region_info = projects.get_region(proj, region)
    except KeyError:
        raise HTTPException(status_code=404, detail="没有这个图幅或分区。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    title = region_info.get("title") or "图幅"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in title)
    dest = config.VAR_DIR / "exports" / project_id / f"{safe}.pdf"
    try:
        result = apply.export_sheet_pdf(proj, region_info, dest, source=source)
    except Exception:
        log.exception("pdf export failed for %s %s", project_id, region)
        raise HTTPException(status_code=500, detail="这份 PDF 没能导出，请换一个分区再试。")
    filename = f"{proj['name']}-{title}"
    if result.get("pending"):
        filename += "-方案预览"
    filename += ".pdf"
    return FileResponse(dest, media_type="application/pdf", filename=filename)



class ScenePreviewBody(BaseModel):
    plan: dict[str, Any]
    region: Optional[str] = Field(default=None)


@app.get("/api/projects/{project_id}/scene")
def api_scene(
    project_id: str,
    region: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None),
    pending: Optional[str] = Query(default=None),
    user: str = Depends(_user),
):
    proj = _proj_or_404(project_id, need_ready=True)
    try:
        region_info = projects.get_region(proj, region)
    except KeyError:
        raise HTTPException(status_code=404, detail="没有这个图幅或分区，请从列表里重新选一个。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    want_pending = str(pending or "").strip().lower() in {"1", "true", "yes", "pending"}
    try:
        return scene.build_project_scene(proj, region_info, source=source, pending=want_pending)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("scene failed for %s %s", project_id, region)
        raise HTTPException(status_code=500, detail="这张图没能生成图面。请换一个分区，或改用右侧的图片预览。")


@app.post("/api/projects/{project_id}/scene/preview")
def api_scene_preview(project_id: str, body: ScenePreviewBody, user: str = Depends(_user)):
    proj = _proj_or_404(project_id, need_ready=True)
    try:
        region_info = projects.get_region(proj, body.region)
    except KeyError:
        raise HTTPException(status_code=404, detail="没有这个图幅或分区，请从列表里重新选一个。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        return scene.preview_plan_scene(proj, body.plan, region_info)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("scene preview failed for %s", project_id)
        raise HTTPException(status_code=500, detail="方案图面没能生成。请检查方案后再试，或改用图片预览。")


@app.get("/api/projects/{project_id}/region")

def api_region_inventory(
    project_id: str,
    region: Optional[str] = Query(default=None),
    user: str = Depends(_user),
):
    proj = _proj_or_404(project_id, need_ready=True)
    try:
        region_info = projects.get_region(proj, region)
    except KeyError:
        raise HTTPException(status_code=404, detail="没有这个图幅或分区。")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"region": region_info, "inventory": projects.region_inventory(proj, region_info)}


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "请求无法完成。"
    return JSONResponse({"detail": detail}, status_code=exc.status_code)


@app.get("/")
def index():
    index_path = config.WEB_DIR / "index.html"
    if not index_path.is_file():
        raise HTTPException(status_code=500, detail="找不到前端页面，请确认 web/index.html 已部署。")
    return FileResponse(index_path, media_type="text/html; charset=utf-8")


if config.WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")
