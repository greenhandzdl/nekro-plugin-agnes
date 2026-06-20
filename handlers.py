"""工具方法注册：Agent 可调用的沙盒方法"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import httpx
from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.base import SandboxMethodType

from .conf import config, plugin
from .models import TaskStatus
from .service import (
    create_video_task,
    extract_video_urls,
    get_video_task,
    prepare_generation_prompt,
    validate_size,
    validate_video_args,
)


async def _request_json(
    client: httpx.AsyncClient, method: str, path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """发送 JSON 请求并返回解析后的响应。"""
    from .service import _req
    return await _req(client, method, path, payload)


def _headers() -> dict[str, str]:
    from .service import _key
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# 文本生成
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="generate_text",
    description="使用 Agnes AI 生成文本。支持普通模式和流式模式。",
)
async def generate_text(
    _ctx: AgentCtx,
    prompt: str,
    system: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    stream: bool = False,
) -> str:
    """使用 Agnes AI 生成文本。"""
    messages: List[Dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
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
            data = await _request_json(client, "POST", "/v1/chat/completions", payload)
        content = data["choices"][0]["message"].get("content") if data.get("choices") else None
        if content:
            return content
        return json.dumps({"type": "text", "content": None, "raw": data}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"文本生成失败: {e}")
        return f"文本生成失败: {e}"


async def _handle_stream(client: httpx.AsyncClient, payload: Dict[str, Any]) -> str:
    """处理流式文本响应（SSE）。"""
    url = f"{config.BASE_URL}/v1/chat/completions"
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


# ---------------------------------------------------------------------------
# 图片生成
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="generate_image",
    description="使用 Agnes AI 生成或编辑图片。支持文生图和图生图。",
)
async def generate_image(
    _ctx: AgentCtx,
    prompt: str,
    size: str = "1024x768",
    input_image_url: Optional[str] = None,
    translate_prompt: bool = True,
) -> str:
    """使用 Agnes AI 生成或编辑图片，返回图片 URL。"""
    try:
        validate_size(size)
    except ValueError as e:
        return f"参数错误: {e}"

    try:
        async with httpx.AsyncClient() as client:
            prepared_prompt, _ = await prepare_generation_prompt(client, prompt, translate_prompt)

            payload: Dict[str, Any] = {"model": config.IMAGE_MODEL, "prompt": prepared_prompt}
            if size:
                payload["size"] = size

            extra: Dict[str, Any] = {"response_format": "url"}
            if input_image_url:
                extra["image"] = input_image_url
            payload["extra_body"] = extra

            data = await _request_json(client, "POST", "/v1/images/generations", payload)

        # 直接返回图片 URL
        urls = _extract_image_urls(data)
        if urls:
            return urls[0]

        # 如果没有 URL，返回完整响应 JSON
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"图片生成失败: {e}")
        return f"图片生成失败: {e}"


def _extract_image_urls(data: Dict[str, Any]) -> List[str]:
    """从图片生成响应中提取 URL 列表。"""
    urls: List[str] = []
    if isinstance(data.get("url"), str):
        urls.append(data["url"])
    if isinstance(data.get("image_url"), str):
        urls.append(data["image_url"])
    if isinstance(data.get("data"), list):
        for item in data["data"]:
            if isinstance(item, dict):
                for key in ("url", "image_url"):
                    if isinstance(item.get(key), str):
                        urls.append(item[key])
    return urls


# ---------------------------------------------------------------------------
# 创建视频
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="create_video",
    description="使用 Agnes AI 创建视频任务。支持文生视频、图生视频、多图视频和关键帧动画。任务在后台异步处理，完成后自动通知。",
)
async def create_video(
    _ctx: AgentCtx,
    prompt: str,
    image_url: Optional[str] = None,
    image_urls: Optional[List[str]] = None,
    mode: Optional[str] = None,
    height: int = 768,
    width: int = 1152,
    num_frames: int = 121,
    frame_rate: float = 24,
    num_inference_steps: Optional[int] = None,
    seed: Optional[int] = None,
    negative_prompt: Optional[str] = None,
    translate_prompt: bool = True,
) -> str:
    """创建 Agnes AI 视频生成任务（异步）。"""
    try:
        validate_video_args(num_frames, frame_rate, height, width)
    except ValueError as e:
        return f"参数错误: {e}"

    try:
        async with httpx.AsyncClient() as client:
            prepared_prompt, translated = await prepare_generation_prompt(client, prompt, translate_prompt)

        task_id = f"task_{int(time.time() * 1000)}"

        task = await create_video_task(
            task_id=task_id, prompt=prepared_prompt, ctx=_ctx,
            translated=translated, model=config.VIDEO_MODEL,
            height=height, width=width, num_frames=num_frames, frame_rate=frame_rate,
            nis=num_inference_steps, seed=seed,
            negative_prompt=negative_prompt, image_url=image_url,
            image_urls=image_urls, mode=mode,
        )

        return json.dumps({
            "type": "video-task", "task_id": task.task_id,
            "status": task.status.value, "prompt_used": prepared_prompt,
            "translated_prompt": translated,
            "next_steps": [f'调用 get_video(task_id="{task.task_id}") 查询视频状态'],
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"视频创建失败: {e}")
        return f"视频创建失败: {e}"


# ---------------------------------------------------------------------------
# 查询视频
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="get_video",
    description="查询 Agnes AI 视频任务的状态和结果。优先从本地缓存获取，force_refresh=true 时从 API 获取最新状态。",
)
async def get_video(
    _ctx: AgentCtx,
    task_id: str,
    force_refresh: bool = False,
) -> str:
    """查询 Agnes AI 视频任务的状态和结果。"""
    try:
        task = await get_video_task(task_id)

        # 本地缓存命中且已终结、非强制刷新 → 直接返回
        if task and not force_refresh and task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return _format_task_result(task)

        # 从 API 获取最新状态
        async with httpx.AsyncClient() as client:
            data = await _request_json(client, "GET", f"/v1/videos/{task_id}")

        if data.get("error"):
            return json.dumps(
                {"type": "video-error", "task_id": task_id, "error": data["error"]},
                ensure_ascii=False, indent=2,
            )

        urls = extract_video_urls(data)
        status_str = str(data.get("status", "")).lower()
        try:
            status_val = TaskStatus(status_str).value
        except ValueError:
            status_val = status_str

        result: Dict[str, Any] = {
            "type": "video-result", "task_id": task_id,
            "status": status_val, "urls": urls,
        }
        if not urls:
            result["next_steps"] = [f'调用 get_video(task_id="{task_id}") 继续查询']

        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"查询视频失败: {e}")
        return f"查询视频失败: {e}"


def _format_task_result(task: Any) -> str:
    """格式化本地缓存的任务结果。"""
    result: Dict[str, Any] = {
        "type": "video-result", "task_id": task.task_id,
        "status": task.status.value, "urls": task.video_urls,
    }
    if task.error_message:
        result["error_message"] = task.error_message
    if not task.video_urls:
        result["next_steps"] = [f'调用 get_video(task_id="{task.task_id}") 继续查询']
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 资源清理
# ---------------------------------------------------------------------------


@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件资源。"""
    logger.info("Agnes AI Generation 插件资源已清理。")
