from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Optional

import httpx

from . import config

_IMAGE_REJECT = re.compile(
    r"image|vision|multimodal|does not support|不支持.*(图|图片|视觉)|unsupported.*content",
    re.I,
)


class MaasError(Exception):
    def __init__(self, message: str, *, status: Optional[int] = None, retryable_without_image: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable_without_image = retryable_without_image


def _headers() -> dict[str, str]:
    if not config.MAAS_KEY:
        raise MaasError("服务器还没有配置模型接口密钥。请在 .env 里设置 MAAS_KEY 后重启。")
    return {
        "Authorization": f"Bearer {config.MAAS_KEY}",
        "Content-Type": "application/json",
    }


def _url() -> str:
    return config.MAAS_BASE.rstrip("/") + "/chat/completions"


def extract_json(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    stripped = text.strip()
    # drop markdown fences if the model ignores instructions
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.S)
    if fence:
        stripped = fence.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    chunk = stripped[start : end + 1]
    try:
        data = json.loads(chunk)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        # try to close a truncated object softly — give up cleanly
        return None


def looks_like_image_rejection(status: Optional[int], body: str) -> bool:
    if not body:
        return False
    if _IMAGE_REJECT.search(body):
        return True
    # some gateways use 400/415/422
    if status in {400, 415, 422} and re.search(r"image|content|type", body, re.I):
        return True
    return False


def extract_assistant_payload(text: str) -> Optional[dict[str, Any]]:
    data = extract_json(text)
    if not data:
        return None
    keys = {"mode", "reply", "analysis", "inserts", "pipes", "texts", "deletes", "replaces", "moves", "do_not_touch", "room", "sheet"}
    if keys.intersection(data.keys()):
        return data
    return None


async def complete(
    *,
    model: str,
    system: str,
    user_text: str,
    image_data_url: Optional[str],
    history: Optional[list[dict[str, Any]]] = None,
    max_tokens: int = 2500,
    temperature: float = 0,
    stream: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Yield {type: delta|done|error, ...}. Tries stream, then non-stream. Image fallback is handled by caller."""
    content: Any
    if image_data_url:
        content = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ]
    else:
        content = user_text
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    if history:
        for item in history:
            role = item.get("role")
            hist_content = item.get("content")
            if role in {"user", "assistant"} and hist_content:
                messages.append({"role": role, "content": hist_content})
    messages.append({"role": "user", "content": content})
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }
    timeout = httpx.Timeout(30.0, read=180.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if stream:
            try:
                async for ev in _stream(client, payload):
                    yield ev
                return
            except MaasError as exc:
                if exc.retryable_without_image:
                    raise
                # fall through to non-stream if stream is not supported
                if exc.status not in {400, 404, 415, 422} and "stream" not in str(exc).lower():
                    raise
                payload = {**payload, "stream": False}
        async for ev in _once(client, payload):
            yield ev


async def _stream(client: httpx.AsyncClient, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    async with client.stream("POST", _url(), headers=_headers(), json=payload) as resp:
        body_preview = ""
        if resp.status_code >= 400:
            body_preview = (await resp.aread()).decode("utf-8", errors="replace")
            err = _error_from_body(resp.status_code, body_preview)
            raise err
        async for line in resp.aiter_lines():
            if not line:
                continue
            if line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                yield {"type": "end"}
                return
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("error"):
                raise _error_from_body(resp.status_code, json.dumps(chunk, ensure_ascii=False))
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            text = delta.get("content") or ""
            if text:
                yield {"type": "delta", "text": text}
            if choice.get("finish_reason") and choice.get("finish_reason") != "null":
                yield {"type": "end"}
                return
        yield {"type": "end"}


async def _once(client: httpx.AsyncClient, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    resp = await client.post(_url(), headers=_headers(), json=payload)
    text = resp.text
    if resp.status_code >= 400:
        raise _error_from_body(resp.status_code, text)
    try:
        data = resp.json()
    except Exception as exc:
        raise MaasError(f"模型接口返回的不是 JSON，请稍后重试。（{exc}）") from exc
    if data.get("error"):
        raise _error_from_body(resp.status_code, text)
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text") or "" for part in content if isinstance(part, dict)
        )
    if content:
        yield {"type": "delta", "text": content}
    yield {"type": "end"}


def _error_from_body(status: int, body: str) -> MaasError:
    detail = body
    try:
        parsed = json.loads(body)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            detail = err.get("message") or err.get("code") or body
        elif isinstance(err, str):
            detail = err
    except Exception:
        pass
    detail = (detail or body or f"HTTP {status}")[:500]
    retry = looks_like_image_rejection(status, body) or looks_like_image_rejection(status, detail)
    if status in {401, 403}:
        msg = "模型接口拒绝了请求，请检查服务器上的 MAAS_KEY 是否有效。"
    elif status == 429:
        msg = "模型接口正忙，请稍等再提出方案。"
    elif retry:
        msg = "这个模型不接受图片，将改用文字再试一次。"
    else:
        msg = f"模型接口调用失败（{status}）。{detail}"
    return MaasError(msg, status=status, retryable_without_image=retry)
