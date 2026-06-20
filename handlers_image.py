"""图片生成工具"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.base import SandboxMethodType

from .conf import config, plugin
from .service import prepare_generation_prompt, validate_size


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
    """使用 Agnes AI 生成或编辑图片。

    支持文生图和图生图。非英文提示词会自动翻译为英文。

    Args:
        prompt: 图片描述。建议包含主体、风格、光照、构图、细节。
            英文效果更好，中文自动翻译。
            例如: "A luminous floating city above a misty canyon at sunrise, cinematic realism"
        size: 尺寸 WIDTHxHEIGHT。默认 "1024x768"。
        input_image_url: 输入图片 URL，用于图生图。默认 None。
        translate_prompt: 是否自动翻译非英文提示词。默认 True。

    Returns:
        图片 URL，或错误信息。

    Examples:
        generate_image(prompt="A cute orange cat on a windowsill, watercolor style")
        generate_image(prompt="未来城市，飞行汽车，电影感")
        generate_image(prompt="Turn into cyberpunk night", input_image_url="https://example.com/img.png")
    """
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

            from .service import _req
            data = await _req(client, "POST", "/v1/images/generations", payload)

        urls = _extract_image_urls(data)
        if urls:
            return urls[0]
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception(f"图片生成失败: {e}")
        return f"图片生成失败: {e}"
