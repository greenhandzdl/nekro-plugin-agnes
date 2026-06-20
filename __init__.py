"""Agnes AI 生成插件

提供文本、图片、视频生成功能，封装 Agnes 官方 API。
支持文生图、图生图、文生视频、图生视频、多图视频、关键帧动画。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.base import SandboxMethodType

from .conf import config, plugin
from .models import TaskStatus
from .service import (
    create_video_task,
    extract_image_urls,
    extract_video_urls,
    get_video_task,
    get_video_task_api,
    prepare_generation_prompt,
    request_json,
    validate_size,
    validate_video_args,
)


def _headers() -> dict[str, str]:
    from .service import _resolve_api_key

    return {
        "Authorization": f"Bearer {_resolve_api_key()}",
        "Content-Type": "application/json",
    }

# ---------------------------------------------------------------------------
# 工具方法：文本生成
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
    """使用 Agnes AI 生成文本。

    Args:
        prompt: 用户提示词。
        system: 系统提示词（可选）。
        temperature: 采样温度，越高越随机。默认 0.7。
        max_tokens: 最大输出 token 数。默认 1024。
        stream: 是否使用流式模式。默认关闭。

    Returns:
        str: 生成的文本内容，或包含 content / events / done 的 JSON 字符串（流式模式）。

    Example:
        普通文本生成:
        generate_text(prompt="写一句产品标语")
        流式文本:
        generate_text(prompt="写一段产品介绍", stream=True)
    """
    messages: List[Dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: Dict[str, Any] = {
        "model": config.TEXT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if stream:
        payload["stream"] = True

    try:
        async with httpx.AsyncClient() as client:
            if stream:
                # 流式模式：手动解析 SSE
                url = f"{config.BASE_URL}/v1/chat/completions"
                content_parts: List[str] = []
                event_count = 0
                done = False
                raw_chunks: List[str] = []
                async with client.stream(
                    "POST", url, json=payload,
                    headers=_headers(), timeout=config.TIMEOUT,
                ) as resp:
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
                raw_text = "\n".join(raw_chunks)
                result = {
                    "type": "text-stream",
                    "content": "".join(content_parts) or None,
                    "events": event_count,
                    "done": done,
                    "raw_prefix": raw_text[:200],
                }
                return json.dumps(result, ensure_ascii=False, indent=2)

            # 普通模式
            data = await request_json(client, "POST", "/v1/chat/completions", payload)
        content = data["choices"][0]["message"].get("content") if data.get("choices") else None
        if content:
            return content
        return json.dumps({"type": "text", "content": None, "raw": data}, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"文本生成失败: {e}")
        return f"文本生成失败: {e}"


# ---------------------------------------------------------------------------
# 工具方法：图片生成
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
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
    """使用 Agnes AI 生成或编辑图片。

    Args:
        prompt: 图片描述提示词。非英文提示词会自动翻译为英文以提高生成质量。
        size: 输出图片尺寸，格式为 WIDTHxHEIGHT。默认 1024x768。
        input_image_url: 输入图片 URL（用于图生图）。默认 None（文生图）。
        translate_prompt: 是否自动翻译非英文提示词。默认开启。

    Returns:
        str: 包含图片 URL 和元信息的 JSON 字符串。

    Example:
        文生图:
        generate_image(prompt="A luminous floating city above a misty canyon at sunrise, cinematic realism")
        图生图:
        generate_image(prompt="Turn the scene into a rainy cyberpunk night", input_image_url="https://example.com/input.png")
        中文提示词:
        generate_image(prompt="一座高信息密度的未来城市集市，拥挤人群，飞行汽车，全息招牌，电影感写实风格")
    """
    try:
        validate_size(size)
    except ValueError as e:
        return f"参数错误: {e}"

    try:
        async with httpx.AsyncClient() as client:
            prepared_prompt, translated = await prepare_generation_prompt(client, prompt, translate_prompt)

            payload: Dict[str, Any] = {
                "model": config.IMAGE_MODEL,
                "prompt": prepared_prompt,
            }
            if size:
                payload["size"] = size

            extra: Dict[str, Any] = {"response_format": "url"}
            if input_image_url:
                extra["image"] = input_image_url
            payload["extra_body"] = extra

            data = await request_json(client, "POST", "/v1/images/generations", payload)

        urls = extract_image_urls(data)
        mode = "image-to-image" if input_image_url else "text-to-image"
        result = {
            "type": mode,
            "urls": urls,
            "prompt_used": prepared_prompt,
            "translated_prompt": translated,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"图片生成失败: {e}")
        return f"图片生成失败: {e}"


# ---------------------------------------------------------------------------
# 工具方法：创建视频
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
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
    """创建 Agnes AI 视频生成任务。

    Agnes 视频 API 是异步的：创建任务后立即返回 task_id，
    后台轮询状态直到完成，完成后自动通知用户。
    使用 get_video 可随时查询任务状态。

    Args:
        prompt: 视频描述提示词。非英文会自动翻译。
        image_url: 单张输入图片 URL（图生视频）。
        image_urls: 多张输入图片 URL（多图视频或关键帧动画）。
        mode: 生成模式，可选 "ti2vid" 或 "keyframes"。
        height: 视频高度。默认 768。
        width: 视频宽度。默认 1152。
        num_frames: 帧数，必须满足 8n+1 且 <= 441。默认 121。
        frame_rate: 帧率，范围 1-60。默认 24。
        num_inference_steps: 推理步数（可选）。
        seed: 随机种子，用于可重复生成。
        negative_prompt: 负面提示词。
        translate_prompt: 是否自动翻译非英文提示词。默认开启。

    Returns:
        str: 包含任务 ID 和初始状态的 JSON 字符串。

    Example:
        文生视频:
        create_video(prompt="A cinematic shot of a cat walking on the beach at sunset")
        图生视频:
        create_video(prompt="Animate subtle camera movement", image_url="https://example.com/image.png")
        关键帧动画:
        create_video(prompt="Smooth transition between keyframes",
                     image_urls=["https://example.com/a.png", "https://example.com/b.png"],
                     mode="keyframes")
    """
    try:
        validate_video_args(num_frames, frame_rate, height, width)
    except ValueError as e:
        return f"参数错误: {e}"

    try:
        async with httpx.AsyncClient() as client:
            prepared_prompt, translated = await prepare_generation_prompt(
                client, prompt, translate_prompt
            )

        task_id = f"task_{int(__import__('time').time() * 1000)}"

        task = await create_video_task(
            task_id=task_id,
            prompt=prepared_prompt,
            ctx=_ctx,
            translated_prompt=translated,
            model=config.VIDEO_MODEL,
            height=height,
            width=width,
            num_frames=num_frames,
            frame_rate=frame_rate,
            num_inference_steps=num_inference_steps,
            seed=seed,
            negative_prompt=negative_prompt,
            image_url=image_url,
            image_urls=image_urls,
            mode=mode,
        )

        result = {
            "type": "video-task",
            "task_id": task.task_id,
            "status": task.status.value,
            "prompt_used": prepared_prompt,
            "translated_prompt": translated,
            "next_steps": [
                f"调用 get_video(task_id=\"{task.task_id}\") 查询视频状态",
            ],
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"视频创建失败: {e}")
        return f"视频创建失败: {e}"


# ---------------------------------------------------------------------------
# 工具方法：查询视频
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="get_video",
    description="查询 Agnes AI 视频任务的状态和结果。优先从本地缓存获取，force_refresh=true 时从 API 获取最新状态。",
)
async def get_video(
    _ctx: AgentCtx,
    task_id: str,
    force_refresh: bool = False,
) -> str:
    """查询 Agnes AI 视频任务的状态和结果。

    Args:
        task_id: 视频任务 ID（创建视频时返回）。
        force_refresh: 是否强制从 API 获取最新状态。默认 False（使用本地缓存）。

    Returns:
        str: 包含任务状态、视频 URL（如果已完成）的 JSON 字符串。

    Example:
        get_video(task_id="task_123456")
        get_video(task_id="task_123456", force_refresh=True)
    """
    try:
        # 优先从本地缓存获取
        task = await get_video_task(task_id)

        # 如果本地有缓存且任务已完成/失败，且非强制刷新，直接返回
        if task and not force_refresh:
            if task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                result = {
                    "type": "video-result",
                    "task_id": task.task_id,
                    "status": task.status.value,
                    "urls": task.video_urls,
                }
                if task.error_message:
                    result["error_message"] = task.error_message
                if not task.video_urls:
                    result["next_steps"] = [f"调用 get_video(task_id=\"{task_id}\") 继续查询"]
                return json.dumps(result, ensure_ascii=False, indent=2)

        # 从 API 获取最新状态
        async with httpx.AsyncClient() as client:
            data = await request_json(client, "GET", f"/v1/videos/{task_id}")

        if data.get("error"):
            return json.dumps(
                {"type": "video-error", "task_id": task_id, "error": data["error"]},
                ensure_ascii=False, indent=2,
            )

        urls = extract_video_urls(data)
        status_str = str(data.get("status", "")).lower()
        try:
            status = TaskStatus(status_str)
        except ValueError:
            status_str_val = status_str
        else:
            status_str_val = status.value

        result = {
            "type": "video-result",
            "task_id": task_id,
            "status": status_str_val,
            "urls": urls,
        }
        if not urls:
            result["next_steps"] = [f"调用 get_video(task_id=\"{task_id}\") 继续查询"]

        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"查询视频失败: {e}")
        return f"查询视频失败: {e}"


# ---------------------------------------------------------------------------
# 资源清理
# ---------------------------------------------------------------------------


@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件资源。"""
    logger.info("Agnes AI Generation 插件资源已清理。")
