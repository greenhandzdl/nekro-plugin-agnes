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
_ENV_NAMES = ("AGNES_API_KEY", "AGNES_API_TOKEN", "APIHUB_AGNES_API_KEY")
_STORE = "agnes_video_tasks"
_TR_SYS = (
    "Translate the user's image/video generation prompt into fluent English. "
    "Preserve all concrete visual details, style words, camera motion, lighting, "
    "composition constraints, and negative instructions. Return only the English prompt."
)

# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


async def _load() -> TaskStoreData:
    data = await store.get(chat_key="global", store_key=_STORE)
    return TaskStoreData.model_validate_json(data) if data else TaskStoreData()


async def _save(s: TaskStoreData) -> None:
    await store.set(chat_key="global", store_key=_STORE, value=s.model_dump_json())


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _key() -> str:
    if config.API_KEY:
        return config.API_KEY
    for n in _ENV_NAMES:
        v = os.environ.get(n)
        if v:
            return v
    raise RuntimeError("未找到 API Key。请设置插件配置 API_KEY 或环境变量 AGNES_API_KEY/AGNES_API_TOKEN/APIHUB_AGNES_API_KEY。")


def _hdrs() -> dict[str, str]:
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}


async def _req(client: httpx.AsyncClient, method: str, path: str, payload: Optional[Dict] = None) -> Dict[str, Any]:
    url = f"{config.BASE_URL}{path}"
    try:
        if method == "GET":
            r = await client.get(url, headers=_hdrs(), timeout=config.TIMEOUT)
        else:
            r = await client.post(url, json=payload, headers=_hdrs(), timeout=config.TIMEOUT)
        r.raise_for_status()
        return json.loads(r.text) if r.text else {}
    except httpx.HTTPStatusError as e:
        d = e.response.text if e.response is not None else str(e)
        raise RuntimeError(f"HTTP {e.response.status_code} from {path}: {d}") from e
    except httpx.RequestError as e:
        raise RuntimeError(f"请求 {path} 失败: {e}") from e


# ---------------------------------------------------------------------------
# 提示词翻译
# ---------------------------------------------------------------------------


def _needs_en(prompt: str) -> bool:
    return any(ord(c) > 127 for c in prompt)


async def _translate(client: httpx.AsyncClient, prompt: str) -> str:
    data = await _req(client, "POST", "/v1/chat/completions", {
        "model": config.TEXT_MODEL,
        "messages": [{"role": "system", "content": _TR_SYS}, {"role": "user", "content": prompt}],
        "temperature": 0, "max_tokens": 800,
    })
    try:
        t = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"提示词翻译失败: {json.dumps(data, ensure_ascii=False)}") from e
    if not t:
        raise RuntimeError("提示词翻译失败: 结果为空")
    return t


async def prepare_generation_prompt(client: httpx.AsyncClient, prompt: str, translate: bool = True) -> Tuple[str, Optional[str]]:
    if translate and _needs_en(prompt):
        t = await _translate(client, prompt)
        return t, t
    return prompt, None


# ---------------------------------------------------------------------------
# URL 提取
# ---------------------------------------------------------------------------


def extract_image_urls(data: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for k in ("url", "image_url"):
        if isinstance(data.get(k), str):
            urls.append(data[k])
    for item in data.get("data", []):
        if isinstance(item, dict):
            for k in ("url", "image_url"):
                if isinstance(item.get(k), str):
                    urls.append(item[k])
    return urls


def extract_video_urls(data: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for k in ("video_url", "url", "remixed_from_video_id"):
        v = data.get(k)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            urls.append(v)
    for item in data.get("data", []):
        if isinstance(item, dict):
            urls.extend(extract_video_urls(item))
    return list(dict.fromkeys(urls))


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def validate_size(value: Optional[str]) -> None:
    if value and not SIZE_RE.match(value):
        raise ValueError(f"无效尺寸: {value}。期望 WIDTHxHEIGHT，例如 1024x768。")


def validate_video_args(nf: Optional[int], fr: Optional[float], h: Optional[int], w: Optional[int]) -> None:
    if nf is not None and (nf > 441 or (nf - 1) % 8 != 0):
        raise ValueError("无效 num_frames: 必须 <= 441 且满足 8n+1，例如 81 或 121。")
    if fr is not None and not (1 <= fr <= 60):
        raise ValueError("无效 frame_rate: 范围 1-60。")
    for name, val in [("height", h), ("width", w)]:
        if val is not None and val <= 0:
            raise ValueError(f"无效 {name}: 必须为正整数。")


# ---------------------------------------------------------------------------
# 视频任务
# ---------------------------------------------------------------------------


def _build_payload(
    prompt: str, model: str, height: int, width: int, num_frames: int, frame_rate: float,
    nis: Optional[int] = None, seed: Optional[int] = None, neg: Optional[str] = None,
    img: Optional[str] = None, imgs: Optional[List[str]] = None, mode: Optional[str] = None,
) -> Dict[str, Any]:
    p: Dict[str, Any] = {
        "model": model, "prompt": prompt, "height": height, "width": width,
        "num_frames": num_frames, "frame_rate": frame_rate,
    }
    if nis is not None:
        p["num_inference_steps"] = nis
    if seed is not None:
        p["seed"] = seed
    if neg:
        p["negative_prompt"] = neg
    if imgs and len(imgs) >= 2:
        p["extra_body"] = {"image": imgs, **({"mode": mode} if mode else {})}
    elif img:
        if mode:
            p["extra_body"] = {"image": img, "mode": mode}
        else:
            p["image"] = img
    return p


async def create_video_task(
    task_id: str, prompt: str, ctx: AgentCtx, translated: Optional[str], model: str,
    height: int = 768, width: int = 1152, num_frames: int = 121, frame_rate: float = 24,
    nis: Optional[int] = None, seed: Optional[int] = None, neg: Optional[str] = None,
    img: Optional[str] = None, imgs: Optional[List[str]] = None, mode: Optional[str] = None,
) -> VideoTask:
    """创建视频任务并提交到 Agnes API，后台轮询结果。"""
    sd = await _load()
    if not ctx.from_chat_key:
        raise ValueError("from_chat_key is required")

    payload = _build_payload(prompt, model, height, width, num_frames, frame_rate, nis, seed, neg, img, imgs, mode)

    async with httpx.AsyncClient() as client:
        created = await _req(client, "POST", "/v1/videos", payload)

    api_id = created.get("id")
    api_st = str(created.get("status", "")) if created.get("status") is not None else None

    task = VideoTask.create(
        task_id=task_id, chat_key=ctx.from_chat_key, prompt=prompt,
        translated_prompt=translated, model=model, height=height, width=width,
        num_frames=num_frames, frame_rate=frame_rate, image_url=img, image_urls=imgs, mode=mode,
    )
    if api_id:
        task.task_id = api_id
    if api_st:
        task.status = TaskStatus.from_api(api_st)

    sd.add_task(task)
    await _save(sd)
    asyncio.create_task(_poll(task.task_id))
    return task


# ---------------------------------------------------------------------------
# 后台轮询
# ---------------------------------------------------------------------------


async def _poll(tid: str) -> None:
    """后台轮询，完成后通知。"""
    sd = await _load()
    task = sd.get_task(tid)
    if not task:
        return

    logger.info(f"轮询 {tid}: {task.prompt}")

    async with httpx.AsyncClient() as client:
        for i in range(config.MAX_POLL_ATTEMPTS):
            await asyncio.sleep(config.POLL_INTERVAL)
            try:
                data = await _req(client, "GET", f"/v1/videos/{tid}")
            except Exception as e:
                logger.warning(f"轮询 {tid} 第 {i + 1} 次失败: {e}")
                continue

            if data.get("error"):
                await _fail(tid, task.chat_key, json.dumps(data["error"], ensure_ascii=False))
                return

            st = TaskStatus.from_api(data.get("status", ""))

            if st == TaskStatus.COMPLETED:
                urls = extract_video_urls(data)
                await _upd(tid, TaskStatus.COMPLETED, video_urls=urls)
                await _notify(tid, task.chat_key, urls=urls)
                return
            if st == TaskStatus.FAILED:
                err = data.get("error_message") or data.get("message") or "未知错误"
                await _fail(tid, task.chat_key, str(err))
                return

            logger.info(f"{tid}: status={data.get('status')} progress={data.get('progress')} ({i + 1}/{config.MAX_POLL_ATTEMPTS})")

    await _fail(tid, task.chat_key, "任务超时")


async def _upd(tid: str, status: TaskStatus, **kw) -> None:
    s = await _load()
    s.update_task(tid, status=status, **kw)
    await _save(s)


async def _fail(tid: str, chat_key: str, err: str) -> None:
    await _upd(tid, TaskStatus.FAILED, error_message=err)
    await _notify(tid, chat_key, error=err)


async def _notify(tid: str, chat_key: str, urls: Optional[List[str]] = None, error: Optional[str] = None) -> None:
    if urls:
        msg = f"【视频生成完成】\n任务ID: {tid}\n视频已生成完毕!\n视频URL:\n" + "\n".join(urls) + "\n(use `send_msg_file` to send the video)"
    elif error:
        msg = f"【视频生成失败】\n任务ID: {tid}\n错误信息: {error}"
    else:
        return
    try:
        await message_service.push_system_message(chat_key=chat_key, agent_messages=msg, trigger_agent=True)
    except Exception as e:
        logger.error(f"通知发送失败: {e}")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_video_task(tid: str) -> Optional[VideoTask]:
    s = await _load()
    return s.get_task(tid)
