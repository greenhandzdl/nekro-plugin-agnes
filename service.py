"""业务逻辑：任务管理、API 调用、异步视频处理"""

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nekro_agent.api import message
from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.message_service import message_service

from .conf import config, store
from .models import TaskStatus, TaskStoreData, VideoTask

SIZE_RE = re.compile(r"^[1-9]\d*x[1-9]\d*$")

# ---------------------------------------------------------------------------
# 存储操作
# ---------------------------------------------------------------------------

_STORE_KEY = "agnes_video_tasks"


async def load_task_store() -> TaskStoreData:
    """加载全局任务数据"""
    data = await store.get(chat_key="global", store_key=_STORE_KEY)
    return TaskStoreData.model_validate_json(data) if data else TaskStoreData()


async def save_task_store(store_data: TaskStoreData) -> None:
    """保存全局任务数据"""
    await store.set(chat_key="global", store_key=_STORE_KEY, value=store_data.model_dump_json())


async def get_next_task_id() -> str:
    """获取下一个任务 ID"""
    store_data = await load_task_store()
    task_id = store_data.get_next_task_id()
    await save_task_store(store_data)
    return task_id


# ---------------------------------------------------------------------------
# HTTP 请求
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


async def request_json(
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
            resp = await client.post(url, json=payload, headers=_headers(), timeout=config.TIMEOUT)
        resp.raise_for_status()
        text = resp.text
        return json.loads(text) if text else {}
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text if exc.response is not None else str(exc)
        raise RuntimeError(f"HTTP {exc.response.status_code} from {path}: {detail}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"请求 {path} 失败: {exc}") from exc


# ---------------------------------------------------------------------------
# 提示词翻译
# ---------------------------------------------------------------------------


def needs_english_translation(prompt: str) -> bool:
    """检测提示词是否包含非 ASCII 字符。"""
    return any(ord(ch) > 127 for ch in prompt)


async def translate_prompt_to_english(client: httpx.AsyncClient, prompt: str) -> str:
    """调用 Agnes 文本模型将非英文提示词翻译为英文。"""
    payload = {
        "model": config.TEXT_MODEL,
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
    data = await request_json(client, "POST", "/v1/chat/completions", payload)
    try:
        translated = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"提示词翻译失败: {json.dumps(data, ensure_ascii=False)}") from exc
    if not translated:
        raise RuntimeError("提示词翻译失败: 翻译结果为空")
    return translated


async def prepare_generation_prompt(
    client: httpx.AsyncClient,
    prompt: str,
    translate: bool = True,
) -> Tuple[str, Optional[str]]:
    """准备生成提示词，必要时翻译为英文。"""
    if translate and needs_english_translation(prompt):
        translated = await translate_prompt_to_english(client, prompt)
        return translated, translated
    return prompt, None


# ---------------------------------------------------------------------------
# URL 提取
# ---------------------------------------------------------------------------


def extract_image_urls(data: Dict[str, Any]) -> List[str]:
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


def extract_video_urls(data: Dict[str, Any]) -> List[str]:
    """从视频响应中提取 URL 列表。"""
    urls: List[str] = []
    for key in ("video_url", "url", "remixed_from_video_id"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            urls.append(value)
    if isinstance(data.get("data"), list):
        for item in data["data"]:
            if isinstance(item, dict):
                urls.extend(extract_video_urls(item))
    return list(dict.fromkeys(urls))


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def validate_size(value: Optional[str]) -> None:
    """校验图片尺寸格式。"""
    if value and not SIZE_RE.match(value):
        raise ValueError(f"无效尺寸: {value}。期望格式为 WIDTHxHEIGHT，例如 1024x768。")


def validate_video_args(
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


# ---------------------------------------------------------------------------
# 视频任务创建
# ---------------------------------------------------------------------------


async def create_video_task(
    task_id: str,
    prompt: str,
    ctx: AgentCtx,
    translated_prompt: Optional[str],
    model: str,
    height: int = 768,
    width: int = 1152,
    num_frames: int = 121,
    frame_rate: float = 24,
    num_inference_steps: Optional[int] = None,
    seed: Optional[int] = None,
    negative_prompt: Optional[str] = None,
    image_url: Optional[str] = None,
    image_urls: Optional[List[str]] = None,
    mode: Optional[str] = None,
) -> VideoTask:
    """创建视频任务并提交到 Agnes API。

    返回创建好的 VideoTask（已从 API 响应中获取初始状态）。
    调用方应使用 asyncio.create_task() 在后台轮询结果。
    """
    store_data = await load_task_store()
    if not ctx.from_chat_key:
        raise ValueError("from_chat_key is required")

    # 构建 API 请求
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
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

    # 调用 API 创建任务
    async with httpx.AsyncClient() as client:
        created = await request_json(client, "POST", "/v1/videos", payload)

    api_task_id = created.get("id")
    api_status = str(created.get("status", "")) if created.get("status") is not None else None

    # 创建本地任务记录
    task = VideoTask.create(
        task_id=task_id,
        chat_key=ctx.from_chat_key,
        prompt=prompt,
        translated_prompt=translated_prompt,
        model=model,
        height=height,
        width=width,
        num_frames=num_frames,
        frame_rate=frame_rate,
        image_url=image_url,
        image_urls=image_urls,
        mode=mode,
    )
    # 用 API 返回的 id 和状态更新
    if api_task_id:
        task.task_id = api_task_id  # 优先使用 API 返回的 id
    if api_status:
        try:
            task.status = TaskStatus(api_status)
        except ValueError:
            pass

    # 持久化
    store_data.add_task(task)
    await save_task_store(store_data)

    # 启动后台轮询
    asyncio.create_task(process_video_task(task.task_id))

    return task


# ---------------------------------------------------------------------------
# 视频任务轮询（后台异步）
# ---------------------------------------------------------------------------


async def process_video_task(task_id: str) -> None:
    """后台轮询视频任务状态，完成后通知用户。

    采用有限次数轮询 + asyncio.sleep 模式，与 tongyi_wanx 一致。
    """
    store_data = await load_task_store()
    task = store_data.get_task(task_id)
    if not task:
        logger.warning(f"尝试处理不存在的任务: {task_id}")
        return

    logger.info(f"开始轮询任务 {task_id}: {task.prompt}")

    async with httpx.AsyncClient() as client:
        for attempt in range(config.MAX_POLL_ATTEMPTS):
            await asyncio.sleep(config.POLL_INTERVAL)

            try:
                data = await request_json(client, "GET", f"/v1/videos/{task_id}")
            except Exception as e:
                logger.warning(f"轮询任务 {task_id} 第 {attempt + 1} 次请求失败: {e}")
                continue

            if data.get("error"):
                error_msg = json.dumps(data["error"], ensure_ascii=False)
                await _update_task_status(task_id, TaskStatus.FAILED, error_message=error_msg)
                await _notify_task_failed(task_id, task.chat_key, error_msg)
                return

            status_str = str(data.get("status", "")).lower()
            try:
                status = TaskStatus(status_str)
            except ValueError:
                status = None

            if status == TaskStatus.COMPLETED:
                urls = extract_video_urls(data)
                await _update_task_status(task_id, TaskStatus.COMPLETED, video_urls=urls)
                await _notify_task_completed(task_id, task.chat_key, urls)
                logger.info(f"任务 {task_id} 完成: {urls}")
                return

            if status == TaskStatus.FAILED:
                error_msg = data.get("error_message") or data.get("message") or "未知错误"
                await _update_task_status(task_id, TaskStatus.FAILED, error_message=str(error_msg))
                await _notify_task_failed(task_id, task.chat_key, str(error_msg))
                logger.error(f"任务 {task_id} 失败: {error_msg}")
                return

            progress = data.get("progress")
            logger.info(f"任务 {task_id}: status={status_str} progress={progress} (attempt {attempt + 1}/{config.MAX_POLL_ATTEMPTS})")

        # 超过最大轮询次数
        await _update_task_status(task_id, TaskStatus.FAILED, error_message="任务超时")
        await _notify_task_failed(task_id, task.chat_key, "任务超时")
        logger.warning(f"任务 {task_id} 超时")


async def _update_task_status(task_id: str, status: TaskStatus, **kwargs) -> None:
    """更新任务状态并持久化。"""
    store_data = await load_task_store()
    store_data.update_task(task_id, status=status, **kwargs)
    await save_task_store(store_data)


async def _notify_task_completed(task_id: str, chat_key: str, urls: List[str]) -> None:
    """通知用户视频生成完成。"""
    urls_str = "\n".join(urls)
    completion_message = (
        f"【视频生成完成】\n"
        f"任务ID: {task_id}\n"
        f"视频已生成完毕!\n"
        f"视频URL:\n{urls_str}\n"
        "(use `send_msg_file` to send the video)"
    )
    try:
        await message_service.push_system_message(
            chat_key=chat_key,
            agent_messages=completion_message,
            trigger_agent=True,
        )
        logger.info(f"任务完成通知已发送: {task_id}")
    except Exception as e:
        logger.error(f"发送任务完成通知失败: {e}")


async def _notify_task_failed(task_id: str, chat_key: str, error_msg: str) -> None:
    """通知用户视频生成失败。"""
    failure_message = (
        f"【视频生成失败】\n"
        f"任务ID: {task_id}\n"
        f"错误信息: {error_msg}"
    )
    try:
        await message_service.push_system_message(
            chat_key=chat_key,
            agent_messages=failure_message,
            trigger_agent=True,
        )
        logger.info(f"任务失败通知已发送: {task_id}")
    except Exception as e:
        logger.error(f"发送任务失败通知失败: {e}")


# ---------------------------------------------------------------------------
# 查询视频任务
# ---------------------------------------------------------------------------


async def get_video_task(task_id: str) -> Optional[VideoTask]:
    """获取视频任务信息。"""
    store_data = await load_task_store()
    return store_data.get_task(task_id)


async def get_video_task_api(task_id: str) -> Dict[str, Any]:
    """从 Agnes API 获取视频任务的最新状态。"""
    async with httpx.AsyncClient() as client:
        return await request_json(client, "GET", f"/v1/videos/{task_id}")
