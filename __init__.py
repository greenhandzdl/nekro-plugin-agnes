"""Agnes AI 生成插件

提供文本、图片、视频生成功能，封装 Agnes 官方 API。
支持文生图、图生图、文生视频、图生视频、多图视频、关键帧动画。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core import logger
from nekro_agent.services.plugin.base import ConfigBase, NekroPlugin, SandboxMethodType
from pydantic import Field

# ---------------------------------------------------------------------------
# 插件注册
# ---------------------------------------------------------------------------

plugin = NekroPlugin(
    name="Agnes AI Generation",
    module_name="agnes_ai_generation",
    description="通过 Agnes AI API 进行文本、图片和视频生成",
    version="1.0.0",
    author="Yacey",
    url="https://github.com/Yacey/agnes-ai-generation-skill",
)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

TEXT_MODEL = "agnes-2.0-flash"
IMAGE_MODEL = "agnes-image-2.1-flash"
VIDEO_MODEL = "agnes-video-v2.0"
SIZE_RE = re.compile(r"^[1-9]\d*x[1-9]\d*$")


@plugin.mount_config()
class AgnesConfig(ConfigBase):
    """Agnes AI 插件配置"""

    API_KEY: str = Field(
        default="",
        title="Agnes API Key",
        description="Agnes AI 平台的 API Key，留空则从环境变量读取",
    )
    BASE_URL: str = Field(
        default="https://apihub.agnes-ai.com",
        title="API 基础地址",
        description="Agnes API 的基础 URL",
    )
    TIMEOUT: int = Field(
        default=120,
        title="请求超时时间",
        description="API 请求的超时时间（秒）",
    )


config: AgnesConfig = plugin.get_config(AgnesConfig)

# ---------------------------------------------------------------------------
# 内部工具函数
# ---------------------------------------------------------------------------


def _resolve_api_key() -> str:
    """获取 API Key：优先使用配置，回退到环境变量。"""
    if config.API_KEY:
        return config.API_KEY
    for name in ("AGNES_API_KEY", "AGNES_API_TOKEN", "APIHUB_AGNES_API_KEY"):
        value = os.environ.get(name)
        if value:
            return value
    raise RuntimeError(
        "未找到 API Key。请在插件配置中设置 API_KEY，"
        "或设置环境变量 AGNES_API_KEY / AGNES_API_TOKEN / APIHUB_AGNES_API_KEY。"
    )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_resolve_api_key()}",
        "Content-Type": "application/json",
    }


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """发送 JSON 请求并返回解析后的响应。"""
    url = f"{config.BASE_URL}{path}"
    try:
        if method == "GET":
            resp = await client.get(url, headers=_headers(), timeout=config.TIMEOUT)
        else:
            resp = await client.post(
                url,
                json=payload,
                headers=_headers(),
                timeout=config.TIMEOUT,
            )
        resp.raise_for_status()
        text = resp.text
        return json.loads(text) if text else {}
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise RuntimeError(f"HTTP {exc.response.status_code} from {path}: {detail}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"请求 {path} 失败: {exc}") from exc


async def _request_text(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """发送请求并返回原始文本。"""
    url = f"{config.BASE_URL}{path}"
    try:
        if method == "GET":
            resp = await client.get(url, headers=_headers(), timeout=config.TIMEOUT)
        else:
            resp = await client.post(
                url,
                json=payload,
                headers=_headers(),
                timeout=config.TIMEOUT,
            )
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise RuntimeError(f"HTTP {exc.response.status_code} from {path}: {detail}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"请求 {path} 失败: {exc}") from exc


async def _stream_summary(client: httpx.AsyncClient, payload: Dict[str, Any]) -> Dict[str, Any]:
    """解析流式文本响应，聚合内容。"""
    raw_chunks: List[str] = []
    event_count = 0
    done = False
    content_parts: List[str] = []

    url = f"{config.BASE_URL}/v1/chat/completions"
    try:
        async with client.stream(
            "POST",
            url,
            json=payload,
            headers=_headers(),
            timeout=config.TIMEOUT,
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
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise RuntimeError(f"HTTP {exc.response.status_code} from /v1/chat/completions: {detail}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"流式请求失败: {exc}") from exc

    raw_text = "\n".join(raw_chunks)
    return {
        "type": "text-stream",
        "content": "".join(content_parts) or None,
        "events": event_count,
        "done": done,
        "raw_prefix": raw_text[:200],
    }


def _needs_english_translation(prompt: str) -> bool:
    """检测提示词是否包含非 ASCII 字符（需要翻译为英文）。"""
    return any(ord(ch) > 127 for ch in prompt)


async def _translate_prompt_to_english(client: httpx.AsyncClient, prompt: str) -> str:
    """调用 Agnes 文本模型将非英文提示词翻译为英文。"""
    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate the user's image/video generation prompt into fluent English. "
                    "Preserve all concrete visual details, style words, camera motion, lighting, "
                    "composition constraints, and negative instructions. Return only the English prompt."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 800,
    }
    data = await _request_json(client, "POST", "/v1/chat/completions", payload)
    try:
        translated = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"提示词翻译失败: {json.dumps(data, ensure_ascii=False)}") from exc
    if not translated:
        raise RuntimeError("提示词翻译失败: 翻译结果为空")
    return translated


async def _prepare_generation_prompt(
    client: httpx.AsyncClient,
    prompt: str,
    translate: bool = True,
) -> tuple[str, Optional[str]]:
    """准备生成提示词，必要时翻译为英文。"""
    if translate and _needs_english_translation(prompt):
        translated = await _translate_prompt_to_english(client, prompt)
        return translated, translated
    return prompt, None


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


def _extract_video_urls(data: Dict[str, Any]) -> List[str]:
    """从视频响应中提取 URL 列表。"""
    urls: List[str] = []
    for key in ("video_url", "url", "remixed_from_video_id"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            urls.append(value)
    if isinstance(data.get("data"), list):
        for item in data["data"]:
            if isinstance(item, dict):
                urls.extend(_extract_video_urls(item))
    # 去重保序
    return list(dict.fromkeys(urls))


def _validate_size(value: Optional[str]) -> None:
    """校验图片尺寸格式。"""
    if value and not SIZE_RE.match(value):
        raise ValueError(f"无效尺寸: {value}。期望格式为 WIDTHxHEIGHT，例如 1024x768。")


def _validate_video_args(
    num_frames: Optional[int],
    frame_rate: Optional[float],
    height: Optional[int],
    width: Optional[int],
) -> None:
    """校验视频参数。"""
    if num_frames is not None:
        if num_frames > 441 or (num_frames - 1) % 8 != 0:
            raise ValueError("无效 num_frames: 必须 <= 441 且满足 8n+1，例如 81 或 121。")
    if frame_rate is not None and not (1 <= frame_rate <= 60):
        raise ValueError("无效 frame_rate: 支持范围为 1-60。")
    for name, val in [("height", height), ("width", width)]:
        if val is not None and val <= 0:
            raise ValueError(f"无效 {name}: 必须为正整数。")


async def _poll_video(
    client: httpx.AsyncClient,
    task_id: str,
    timeout: int = 900,
    interval: int = 10,
) -> Dict[str, Any]:
    """轮询视频任务直到完成或超时。"""
    deadline = asyncio.get_event_loop().time() + timeout
    last: Dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        last = await _request_json(client, "GET", f"/v1/videos/{task_id}")
        if last.get("error"):
            raise RuntimeError(f"视频任务 {task_id} 返回错误: {json.dumps(last, ensure_ascii=False)}")
        status = str(last.get("status", "")).lower()
        progress = last.get("progress")
        logger.info(f"视频 {task_id}: status={status} progress={progress}")
        if status in {"completed", "failed"}:
            return last
        await asyncio.sleep(interval)
    raise RuntimeError(
        f"等待视频任务 {task_id} 超时。最后响应: {json.dumps(last, ensure_ascii=False)}"
    )


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
        "model": TEXT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if stream:
        payload["stream"] = True

    try:
        async with httpx.AsyncClient() as client:
            if stream:
                result = await _stream_summary(client, payload)
                return json.dumps(result, ensure_ascii=False, indent=2)
            data = await _request_json(client, "POST", "/v1/chat/completions", payload)
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
        _validate_size(size)
    except ValueError as e:
        return f"参数错误: {e}"

    try:
        async with httpx.AsyncClient() as client:
            prepared_prompt, translated = await _prepare_generation_prompt(
                client, prompt, translate_prompt
            )

            payload: Dict[str, Any] = {
                "model": IMAGE_MODEL,
                "prompt": prepared_prompt,
            }
            if size:
                payload["size"] = size

            extra: Dict[str, Any] = {"response_format": "url"}
            if input_image_url:
                extra["image"] = input_image_url
            payload["extra_body"] = extra

            data = await _request_json(client, "POST", "/v1/images/generations", payload)

        urls = _extract_image_urls(data)
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
    description="使用 Agnes AI 创建视频任务。支持文生视频、图生视频、多图视频和关键帧动画。",
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
    poll: bool = False,
    poll_timeout: int = 900,
    poll_interval: int = 10,
) -> str:
    """创建 Agnes AI 视频生成任务。

    Agnes 视频 API 是异步的：先创建任务，再通过 get_video 查询结果。

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
        poll: 是否轮询等待结果。默认 False（仅创建任务）。
        poll_timeout: 轮询超时时间（秒）。默认 900。
        poll_interval: 轮询间隔（秒）。默认 10。

    Returns:
        str: 包含任务 ID、状态和（若已完成）视频 URL 的 JSON 字符串。

    Example:
        文生视频:
        create_video(prompt="A cinematic shot of a cat walking on the beach at sunset", poll=True)
        图生视频:
        create_video(prompt="Animate subtle camera movement", image_url="https://example.com/image.png", poll=True)
        关键帧动画:
        create_video(prompt="Smooth transition between keyframes",
                     image_urls=["https://example.com/a.png", "https://example.com/b.png"],
                     mode="keyframes", poll=True)
    """
    try:
        _validate_video_args(num_frames, frame_rate, height, width)
    except ValueError as e:
        return f"参数错误: {e}"

    try:
        async with httpx.AsyncClient() as client:
            prepared_prompt, translated = await _prepare_generation_prompt(
                client, prompt, translate_prompt
            )

            payload: Dict[str, Any] = {
                "model": VIDEO_MODEL,
                "prompt": prepared_prompt,
                "height": height,
                "width": width,
                "num_frames": num_frames,
                "frame_rate": frame_rate,
            }
            if num_inference_steps is not None:
                payload["num_inference_steps"] = num_inference_steps
            if seed is not None:
                payload["seed"] = seed
            if negative_prompt:
                payload["negative_prompt"] = negative_prompt

            # 处理图片输入
            if image_urls and len(image_urls) >= 2:
                payload["extra_body"] = {"image": image_urls}
                if mode:
                    payload["extra_body"]["mode"] = mode
            elif image_url:
                if mode:
                    payload["extra_body"] = {"image": image_url, "mode": mode}
                else:
                    payload["image"] = image_url

            # 创建任务
            created = await _request_json(client, "POST", "/v1/videos", payload)
            task_id = created.get("id")
            status = str(created.get("status", "")) if created.get("status") is not None else None

            result: Dict[str, Any] = {
                "type": "video-task",
                "task_id": task_id,
                "status": status,
                "prompt_used": prepared_prompt,
                "translated_prompt": translated,
            }

            if not poll:
                result["next_steps"] = [
                    f"调用 get_video(task_id=\"{task_id}\") 查询视频状态",
                ]
                return json.dumps(result, ensure_ascii=False, indent=2)

            # 轮询等待
            if not task_id:
                return json.dumps(
                    {
                        "type": "video-task",
                        "error": f"创建响应中未包含 task_id: {json.dumps(created, ensure_ascii=False)}",
                    },
                    ensure_ascii=False,
                    indent=2,
                )

            logger.info(f"视频任务 {task_id} 已创建，开始轮询...")
            data = await _poll_video(client, str(task_id), poll_timeout, poll_interval)
            urls = _extract_video_urls(data)
            result.update({
                "type": "video-result",
                "status": str(data.get("status", "")),
                "urls": urls,
                "raw": data,
            })
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
    description="查询 Agnes AI 视频任务的状态和结果。",
)
async def get_video(
    _ctx: AgentCtx,
    task_id: str,
) -> str:
    """查询 Agnes AI 视频任务的状态和结果。

    Args:
        task_id: 视频任务 ID（创建视频时返回）。

    Returns:
        str: 包含任务状态、视频 URL（如果已完成）的 JSON 字符串。

    Example:
        get_video(task_id="task_123456")
    """
    try:
        async with httpx.AsyncClient() as client:
            data = await _request_json(client, "GET", f"/v1/videos/{task_id}")

        if data.get("error"):
            return json.dumps(
                {"type": "video-error", "task_id": task_id, "error": data["error"]},
                ensure_ascii=False,
                indent=2,
            )

        urls = _extract_video_urls(data)
        status = str(data.get("status", "")) if data.get("status") is not None else None
        result: Dict[str, Any] = {
            "type": "video-result",
            "task_id": task_id,
            "status": status,
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
