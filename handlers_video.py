"""视频生成工具"""

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
    validate_video_args,
)


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
    """创建 Agnes AI 视频生成任务（异步处理）。

    API 是异步的：创建后立即返回 task_id，后台自动轮询，
    完成后通过系统消息通知用户。可随时调用 get_video 查询。

    num_frames 必须满足 8n+1 且 <= 441（如 81, 89, 121）。

    Args:
        prompt: 视频描述。建议包含主体、动作、场景、风格、镜头、光照。
            英文效果更好，中文自动翻译。
            例如: "A cinematic shot of a cat walking on the beach at sunset"
        image_url: 单张输入图片 URL，用于图生视频。默认 None。
        image_urls: 多张输入图片 URL，用于多图视频或关键帧动画。默认 None。
        mode: 生成模式。"ti2vid"(图生视频) 或 "keyframes"(关键帧动画)。默认 None。
        height: 视频高度。默认 768。
        width: 视频宽度。默认 1152。
        num_frames: 帧数，满足 8n+1 <= 441。默认 121，快速测试可 81。
        frame_rate: 帧率 1-60。默认 24。
        num_inference_steps: 推理步数。默认 None。
        seed: 随机种子，可重复生成。默认 None。
        negative_prompt: 负面提示词。默认 None。
        translate_prompt: 是否自动翻译非英文提示词。默认 True。

    Returns:
        JSON: {type, task_id, status, prompt_used, translated_prompt, next_steps}

    Examples:
        create_video(prompt="A cat walking on the beach at sunset")
        create_video(prompt="Animate camera movement", image_url="https://example.com/img.png")
        create_video(prompt="Smooth keyframe transition",
                     image_urls=["https://a.png", "https://b.png"], mode="keyframes")
    """
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
    """查询 Agnes AI 视频任务的状态和结果。

    优先从本地缓存获取；任务已完成或失败时直接返回缓存。
    设置 force_refresh=True 强制从 API 获取最新状态。

    Args:
        task_id: 视频任务 ID，由 create_video 返回。
        force_refresh: 是否强制请求 API。默认 False。

    Returns:
        JSON: {type, task_id, status, urls, error_message, next_steps}

    Examples:
        get_video(task_id="task_123456")
        get_video(task_id="task_123456", force_refresh=True)
    """
    try:
        task = await get_video_task(task_id)

        # 本地缓存命中且已终结、非强制刷新 → 直接返回
        if task and not force_refresh and task.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return _format_task_result(task)

        # 从 API 获取最新状态
        async with httpx.AsyncClient() as client:
            from .service import _req
            data = await _req(client, "GET", f"/v1/videos/{task_id}")

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
