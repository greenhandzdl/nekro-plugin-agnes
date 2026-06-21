"""文本生成工具"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.base import SandboxMethodType

from .conf import config, plugin


def _headers() -> dict[str, str]:
    from .service import _key
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="generate_text",
    description="使用 Agnes AI 生成文本。支持普通模式和流式模式，支持多模态输入（文本+图片）。",
)
async def generate_text(
    _ctx: AgentCtx,
    prompt: str,
    images: Optional[List[str]] = None,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    stream: bool = False,
) -> str:
    """使用 Agnes AI 生成文本，支持文本+图片多模态输入。

    普通模式直接返回文本；流式模式聚合 SSE 事件返回 JSON (content/events/done/raw_prefix)。

    当提供 images 参数时，会构建 OpenAI 兼容的多模态消息格式：
    content 为 [{type: "text", text: prompt}, {type: "image_url", image_url: {url: ...}}, ...]

    Args:
        prompt: 用户提示词。建议包含主题、风格、长度、语言等。
        images: 图片列表，支持 HTTP(S) URL 或 Data URI (base64)。
            传入后模型会结合图片内容和文本提示进行推理。
            例如: ["https://example.com/img.png"] 或 ["data:image/png;base64,..."]
        system: 系统提示词，设定 AI 角色。例如: "你是技术文档工程师"
        temperature: 采样温度 0-2，越低越确定。默认 0.7。
        max_tokens: 最大输出 token 数。默认 1024。
        stream: 是否流式模式。默认 False。

    Returns:
        普通模式返回文本；流式模式返回 JSON。

    Examples:
        纯文本:
        generate_text(prompt="写一句产品标语")

        带图片(视觉理解):
        generate_text(prompt="描述这张图片的内容", images=["https://example.com/img.png"])

        多图+中文提示:
        generate_text(prompt="比较这两张图片的差异",
                     images=["https://a.png", "https://b.png"])

        Data URI:
        generate_text(prompt="这是什么", images=["data:image/png;base64,iVBORw0KGgo..."])
    """
    if config.DISABLE_TEXT_GENERATION:
        return "文本生成功能当前不可用，请稍后再试。"

    messages: List[Dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})

    # 构建用户消息：纯文本 or 多模态
    if images:
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": img}})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})

    payload: Dict[str, Any] = {
        "model": config.TEXT_MODEL, "messages": messages,
        "temperature": temperature, "max_tokens": max_tokens,
    }
    if stream:
        payload["stream"] = True

    try:
        async with httpx.AsyncClient() as client:
            if stream:
                return await _handle_stream(client, payload)
            data = await _req(client, "POST", "/v1/chat/completions", payload)
        content = data["choices"][0]["message"].get("content") if data.get("choices") else None
        if content:
            return content
        return json.dumps({"type": "text", "content": None, "raw": data}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"文本生成失败: {e}")
        return f"文本生成失败: {e}"


async def _handle_stream(client: httpx.AsyncClient, payload: Dict[str, Any]) -> str:
    """处理流式文本响应（SSE）。"""
    from .service import _get_base_url
    base = _get_base_url()
    url = f"{base}/v1/chat/completions"
    content_parts: List[str] = []
    event_count = 0
    done = False
    raw_chunks: List[str] = []

    async with client.stream("POST", url, json=payload, headers=_headers(), timeout=config.TIMEOUT) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            raw_chunks.append(data)
            if data == "[DONE]":
                done = True
            elif data:
                event_count += 1
                try:
                    event = json.loads(data)
                    delta = event["choices"][0].get("delta", {})
                    content = delta.get("content")
                    if isinstance(content, str):
                        content_parts.append(content)
                except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                    continue

    return json.dumps({
        "type": "text-stream", "content": "".join(content_parts) or None,
        "events": event_count, "done": done, "raw_prefix": "\n".join(raw_chunks)[:200],
    }, ensure_ascii=False, indent=2)


async def _req(client: httpx.AsyncClient, method: str, path: str, payload: Optional[Dict] = None) -> Dict[str, Any]:
    from .service import _req as svc_req
    return await svc_req(client, method, path, payload)
