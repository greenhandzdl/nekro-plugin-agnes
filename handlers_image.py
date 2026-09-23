"""图片生成工具"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.base import SandboxMethodType

from .conf import config, plugin
from .service import _req, extract_image_urls, prepare_generation_prompt, validate_ratio, validate_size


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="generate_image",
    description="使用 Agnes AI 生成或编辑图片。支持文生图和图生图（含多图参考）。",
)
async def generate_image(
    _ctx: AgentCtx,
    prompt: str,
    size: str = "1K",
    ratio: str = "1:1",
    image_urls: Optional[List[str]] = None,
    translate_prompt: bool = True,
    send_to_chat: bool = False,
) -> str:
    """使用 Agnes AI 生成或编辑图片。

    支持文生图、图生图和多图合成。非英文提示词会自动翻译为英文。
    尺寸使用档位制（1K/2K/3K/4K）配合宽高比（ratio）控制输出。

    Args:
        prompt: 图片描述。建议包含主体、风格、光照、构图、细节。
            英文效果更好，中文自动翻译。
            例如: "A luminous floating city above a misty canyon at sunrise, cinematic realism"
        size: 输出尺寸档位: "1K" | "2K" | "3K" | "4K"。默认 "1K"。
            也兼容历史精确尺寸写法（如 "1024x768"），但可能被标准化。
        ratio: 宽高比，与档位制 size 配合。支持 "1:1"(默认) "3:4" "4:3"
            "16:9" "9:16" "2:3" "3:2" "21:9"。
        image_urls: 输入图片列表，支持 HTTP(S) URL 或 Data URI (base64)。
            用于图生图/多图合成。默认 None（文生图）。
            单图: ["https://example.com/img.png"] 或 ["data:image/png;base64,..."]
            多图: ["https://a.png", "https://b.png"]
        translate_prompt: 是否自动翻译非英文提示词。默认 True。
        send_to_chat: 是否将生成的图片发送到当前聊天。默认 False。

    Returns:
        沙盒图片文件路径，或错误信息。

    Examples:
        generate_image(prompt="A cute orange cat on a windowsill, watercolor style")
        generate_image(prompt="未来城市，飞行汽车，电影感", size="2K", ratio="16:9", send_to_chat=True)
        generate_image(prompt="Turn into cyberpunk night", image_urls=["https://example.com/img.png"])
        generate_image(prompt="融合这两张图", image_urls=["https://a.png", "https://b.png"])
    """
    try:
        validate_size(size)
        validate_ratio(ratio)
    except ValueError as e:
        return f"参数错误: {e}"

    try:
        async with httpx.AsyncClient() as client:
            prepared_prompt, _ = await prepare_generation_prompt(client, prompt, translate_prompt)

            payload: Dict[str, Any] = {
                "model": config.IMAGE_MODEL, "prompt": prepared_prompt,
                "size": size, "ratio": ratio,
            }

            extra: Dict[str, Any] = {"response_format": "url"}
            if image_urls:
                extra["image"] = image_urls
            payload["extra_body"] = extra

            data = await _req(client, "POST", "/v1/images/generations", payload)

        urls = extract_image_urls(data)
        if not urls:
            return json.dumps(data, ensure_ascii=False, indent=2)

        # 转换为沙盒文件路径
        sandbox_file = await _ctx.fs.mixed_forward_file(urls[0])

        # 发送到聊天
        if send_to_chat:
            await _ctx.send_image(sandbox_file)

        return sandbox_file
    except Exception as e:
        logger.exception(f"图片生成失败: {e}")
        return f"图片生成失败: {e}"
