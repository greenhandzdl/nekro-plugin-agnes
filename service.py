"""业务逻辑：任务管理、API 调用、异步视频处理

状态流转:
  创建 → PENDING (需审批) / QUEUED (不需审批)
  PENDING → APPROVED (审批通过) / REJECTED (审批拒绝)
  APPROVED/QUEUED → PROCESSING (API 开始生成) → COMPLETED / FAILED
"""

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nekro_agent.api import message
from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.message_service import message_service

from .conf import config, store
from .models import ChatSessionData, GlobalTaskData, TaskStatus, VideoTask

SIZE_RE = re.compile(r"^[1-9]\d*x[1-9]\d*$")
_ENV_NAMES = ("AGNES_API_KEY", "AGNES_API_TOKEN", "APIHUB_AGNES_API_KEY")
_STORE_TASKS = "agnes_video_tasks"
_STORE_CHAT = "agnes_chat"
_TR_SYS = (
    "Translate the user's image/video generation prompt into fluent English. "
    "Preserve all concrete visual details, style words, camera motion, lighting, "
    "composition constraints, and negative instructions. Return only the English prompt."
)

# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


async def _load_tasks() -> GlobalTaskData:
    data = await store.get(chat_key="global", store_key=_STORE_TASKS)
    return GlobalTaskData.model_validate_json(data) if data else GlobalTaskData()


async def _save_tasks(s: GlobalTaskData) -> None:
    await store.set(chat_key="global", store_key=_STORE_TASKS, value=s.model_dump_json())


async def _load_chat(chat_key: str) -> ChatSessionData:
    data = await store.get(chat_key=chat_key, store_key=_STORE_CHAT)
    return ChatSessionData.model_validate_json(data) if data else ChatSessionData()


async def _save_chat(chat_key: str, data: ChatSessionData) -> None:
    await store.set(chat_key=chat_key, store_key=_STORE_CHAT, value=data.model_dump_json())


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
    raise RuntimeError("未找到 API Key。请设置 API_KEY 或环境变量 AGNES_API_KEY/AGNES_API_TOKEN/APIHUB_AGNES_API_KEY。")


def _hdrs() -> dict[str, str]:
    return {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}


async def _req(client: httpx.AsyncClient, method: str, path: str, payload: Optional[Dict] = None) -> Dict[str, Any]:
    base = config.BASE_URL.rstrip("/")
    url = f"{base}/{path.lstrip('/')}"
    logger.debug(f"API 请求: {method} {url}")
    try:
        r = await (client.get(url, headers=_hdrs(), timeout=config.TIMEOUT) if method == "GET"
                   else client.post(url, json=payload, headers=_hdrs(), timeout=config.TIMEOUT))
        r.raise_for_status()
        text = r.text.strip()
        if not text:
            logger.warning(f"API 返回空内容: {url}")
            return {}
        logger.debug(f"API 响应: {text[:200]}")
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败: {url}, 响应内容: {r.text[:200]!r}")
        raise RuntimeError(f"JSON 解析失败 {url}: {e}") from e
    except httpx.HTTPStatusError as e:
        d = e.response.text if e.response is not None else str(e)
        logger.error(f"HTTP 错误 {e.response.status_code}: {url}, 响应: {d[:200]}")
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
# 视频任务 — 创建
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
    task_id: str, prompt: str, ctx: AgentCtx,
    reason: Optional[str] = None, model: str = "",
    height: int = 768, width: int = 1152, num_frames: int = 121, frame_rate: float = 24,
    nis: Optional[int] = None, seed: Optional[int] = None, neg: Optional[str] = None,
    img: Optional[str] = None, imgs: Optional[List[str]] = None, mode: Optional[str] = None,
) -> VideoTask:
    """创建视频任务并提交到 Agnes API。"""
    gt = await _load_tasks()
    if not ctx.from_chat_key:
        raise ValueError("from_chat_key is required")

    payload = _build_payload(prompt, model, height, width, num_frames, frame_rate, nis, seed, neg, img, imgs, mode)

    # 初始状态: 需审批 → PENDING, 不需审批 → QUEUED
    initial_status = TaskStatus.PENDING if config.REQUIRE_ADMIN_APPROVAL else TaskStatus.QUEUED

    task = VideoTask.create(
        task_id=task_id, chat_key=ctx.from_chat_key, prompt=prompt,
        reason=reason, model=model, height=height, width=width,
        num_frames=num_frames, frame_rate=frame_rate, image_url=img, image_urls=imgs, mode=mode,
    )
    task.status = initial_status

    gt.add_task(task)
    await _save_tasks(gt)

    chat_data = await _load_chat(ctx.from_chat_key)
    chat_data.current_task_id = task.task_id
    await _save_chat(ctx.from_chat_key, chat_data)

    # 调用 API 创建任务，失败时标记 FAILED 并通知
    try:
        async with httpx.AsyncClient() as client:
            created = await _req(client, "POST", "/v1/videos", payload)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"创建视频任务 API 调用失败: {error_msg}")
        await update_task_status(task.task_id, TaskStatus.FAILED, error_message=error_msg)
        # 清理会话
        chat_data = await _load_chat(ctx.from_chat_key)
        if chat_data.current_task_id == task.task_id:
            chat_data.current_task_id = None
            await _save_chat(ctx.from_chat_key, chat_data)
        raise

    api_id = created.get("id")
    api_video_id = created.get("video_id") or created.get("videoId")
    api_st = str(created.get("status", "")) if created.get("status") is not None else None

    logger.info(f"创建任务响应: id={api_id}, video_id={api_video_id}, status={api_st}")

    # 用 API 返回的 id 更新 task_id（可能和本地生成的不同）
    if api_id and api_id != task.task_id:
        # 需要更新 store 中的 key
        gt = await _load_tasks()
        gt.tasks.pop(task.task_id, None)
        task.task_id = api_id
        gt.add_task(task)
        await _save_tasks(gt)
        # 更新会话中的 task_id
        chat_data = await _load_chat(ctx.from_chat_key)
        if chat_data.current_task_id == task_id:
            chat_data.current_task_id = api_id
            await _save_chat(ctx.from_chat_key, chat_data)

    if api_video_id:
        task.video_id = api_video_id
    if api_st:
        task.status = TaskStatus.from_api(api_st)
        # 同步更新 store
        gt = await _load_tasks()
        gt.update_task(task.task_id, status=task.status, video_id=task.video_id)
        await _save_tasks(gt)

    if config.REQUIRE_ADMIN_APPROVAL:
        # 发送审批消息
        manager_msg = (
            f"【视频生成申请】\n任务ID: {task.task_id}\n会话: {ctx.from_chat_key}\n"
            f"提示词: {prompt}\n模型: {model}\n尺寸: {width}x{height}\n"
            f"帧数: {num_frames}\n帧率: {frame_rate}\n"
        )
        if reason:
            manager_msg += f"原因: {reason}\n"
        manager_msg += (
            f"使用 approve_video_task(task_id=\"{task.task_id}\") 批准\n"
            f"使用 reject_video_task(task_id=\"{task.task_id}\") 拒绝"
        )
        try:
            target = config.MANAGER_CHAT_KEY or ctx.from_chat_key
            await message.send_text(chat_key=target, message=manager_msg, ctx=ctx, record=False)
        except Exception as e:
            logger.error(f"发送审批消息失败: {e}")
    else:
        # 不需审批，直接进入队列等待轮询
        asyncio.create_task(process_video_task(task.task_id))

    return task


# ---------------------------------------------------------------------------
# 视频任务 — 审批
# ---------------------------------------------------------------------------


_APPROVABLE_STATUSES = {TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.APPROVED}


async def approve_video_task(task_id: str) -> bool:
    """批准视频任务: PENDING/QUEUED/APPROVED → APPROVED → 开始轮询

    接受多种状态:
    - PENDING: 需审批模式下的初始状态
    - QUEUED: API 返回的初始状态（审批通过但轮询未开始）
    - APPROVED: 已经批准但轮询可能已停止（重启后恢复）
    """
    gt = await _load_tasks()
    task = gt.get_task(task_id)
    if not task or task.status not in _APPROVABLE_STATUSES:
        logger.warning(f"批准失败: {task_id} 不存在或状态不可审批: {task.status if task else 'N/A'}")
        return False
    # 只有非终态才需要更新（防止覆盖 CANCELED/REJECTED）
    if task.status != TaskStatus.APPROVED:
        await update_task_status(task_id, TaskStatus.APPROVED)
    asyncio.create_task(process_video_task(task_id))
    return True


async def reject_video_task(task_id: str) -> bool:
    """拒绝视频任务: PENDING → REJECTED (等同 CANCELED)"""
    gt = await _load_tasks()
    task = gt.get_task(task_id)
    if not task or task.status != TaskStatus.PENDING:
        logger.warning(f"拒绝失败: {task_id} 不存在或状态非 PENDING (当前: {task.status if task else 'N/A'})")
        return False
    await update_task_status(task_id, TaskStatus.REJECTED, error_message="管理员拒绝了请求")
    return True


# ---------------------------------------------------------------------------
# 视频任务 — 状态更新 + 通知
# ---------------------------------------------------------------------------


def _is_terminal_status(status: TaskStatus) -> bool:
    """是否为终态（不再轮询）"""
    return status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.REJECTED)


async def update_task_status(task_id: str, status: TaskStatus, **kwargs) -> None:
    """更新任务状态，终态时清理会话并通知。不允许从终态转移到其他状态。"""
    gt = await _load_tasks()
    existing = gt.get_task(task_id)
    if not existing:
        return
    # 终态不可覆盖（防止取消后被轮询覆盖）
    if _is_terminal_status(existing.status) and not _is_terminal_status(status):
        logger.warning(f"任务 {task_id} 已是终态 {existing.status.value}，不允许更新为 {status.value}")
        return
    if not gt.update_task(task_id, status=status, **kwargs):
        return
    await _save_tasks(gt)

    task = gt.get_task(task_id)
    if not task:
        return

    # 终态处理
    if _is_terminal_status(status):
        # 清理会话中的 current_task_id
        chat_data = await _load_chat(task.chat_key)
        if chat_data.current_task_id == task_id:
            chat_data.current_task_id = None
            await _save_chat(task.chat_key, chat_data)

    # 完成通知
    if status == TaskStatus.COMPLETED and task.video_urls:
        chat_data = await _load_chat(task.chat_key)
        chat_data.add_history(task.prompt, task.video_urls, task.task_id)
        await _save_chat(task.chat_key, chat_data)

        msg = (
            f"【视频生成完成】\n任务ID: {task_id}\n提示词: {task.prompt}\n"
            f"视频已生成完毕!\n视频URL:\n" + "\n".join(task.video_urls) +
            "\n(use `send_msg_file` to send the video)"
        )
        try:
            await message_service.push_system_message(chat_key=task.chat_key, agent_messages=msg, trigger_agent=True)
        except Exception as e:
            logger.error(f"发送完成通知失败: {e}")

    # 失败/拒绝通知
    elif status == TaskStatus.FAILED:
        err = task.error_message or "未知错误"
        msg = f"【视频生成失败】\n任务ID: {task_id}\n提示词: {task.prompt}\n错误信息: {err}"
        try:
            await message_service.push_system_message(chat_key=task.chat_key, agent_messages=msg, trigger_agent=True)
        except Exception as e:
            logger.error(f"发送失败通知失败: {e}")

    elif status == TaskStatus.REJECTED:
        msg = f"【视频生成已拒绝】\n任务ID: {task_id}\n提示词: {task.prompt}\n管理员拒绝了请求。"
        try:
            await message_service.push_system_message(chat_key=task.chat_key, agent_messages=msg, trigger_agent=True)
        except Exception as e:
            logger.error(f"发送拒绝通知失败: {e}")


# ---------------------------------------------------------------------------
# 视频任务 — 后台轮询
# ---------------------------------------------------------------------------


async def process_video_task(task_id: str) -> None:
    """后台轮询视频任务状态

    轮询状态流转: QUEUED/APPROVED → PROCESSING → COMPLETED/FAILED
    """
    gt = await _load_tasks()
    task = gt.get_task(task_id)
    if not task:
        return

    # 优先使用 video_id 轮询，回退到 task_id
    poll_id = task.video_id or task_id
    poll_url = f"/agnesapi?video_id={poll_id}" if task.video_id else f"/v1/videos/{task_id}"

    logger.info(f"开始轮询任务 {task_id} (video_id={task.video_id}, poll_url={poll_url}): {task.prompt}")

    async with httpx.AsyncClient() as client:
        for i in range(config.MAX_POLL_ATTEMPTS):
            await asyncio.sleep(config.POLL_INTERVAL)
            try:
                data = await _req(client, "GET", poll_url)
            except Exception as e:
                logger.warning(f"轮询 {task_id} 第 {i + 1} 次失败: {e}")
                continue

            logger.info(f"轮询 {task_id} 第 {i + 1} 次响应: {json.dumps(data, ensure_ascii=False)[:300]}")

            if data.get("error"):
                await update_task_status(task_id, TaskStatus.FAILED, error_message=json.dumps(data["error"], ensure_ascii=False))
                return

            st = TaskStatus.from_api(data.get("status", ""))

            # 非终态时同步更新本地状态（让 /agnes_info 能反映进度）
            if not _is_terminal_status(st) and task.status != st:
                task.status = st
                await _save_tasks(gt)

            if st == TaskStatus.COMPLETED:
                urls = extract_video_urls(data)
                await update_task_status(task_id, TaskStatus.COMPLETED, video_urls=urls)
                return
            if st == TaskStatus.FAILED:
                err = data.get("error_message") or data.get("message") or "未知错误"
                await update_task_status(task_id, TaskStatus.FAILED, error_message=str(err))
                return

            progress = data.get("progress")
            logger.info(f"{task_id}: status={data.get('status')} progress={progress} ({i + 1}/{config.MAX_POLL_ATTEMPTS})")

    await update_task_status(task_id, TaskStatus.FAILED, error_message="任务超时")


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_video_task(task_id: str) -> Optional[VideoTask]:
    gt = await _load_tasks()
    return gt.get_task(task_id)


async def cancel_current_video_task(chat_key: str) -> Optional[VideoTask]:
    """取消当前会话的视频任务（REJECTED 等同 CANCELED）"""
    chat_data = await _load_chat(chat_key)
    if not chat_data.current_task_id:
        return None

    task = await get_video_task(chat_data.current_task_id)
    if not task:
        chat_data.current_task_id = None
        await _save_chat(chat_key, chat_data)
        return None

    if _is_terminal_status(task.status):
        chat_data.current_task_id = None
        await _save_chat(chat_key, chat_data)
        return None

    # 使用 REJECTED 而非 CANCELED（等同处理）
    # 通过 update_task_status 确保后台轮询也能看到状态变化
    await update_task_status(
        chat_data.current_task_id,
        TaskStatus.REJECTED,
        error_message="用户取消",
    )

    # 清理会话
    chat_data = await _load_chat(chat_key)
    if chat_data.current_task_id == task.task_id:
        chat_data.current_task_id = None
        await _save_chat(chat_key, chat_data)

    return task


def format_task_info(task: VideoTask) -> str:
    """格式化任务信息"""
    create_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.create_time))
    update_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.update_time))

    info = (
        f"任务ID: {task.task_id}\n会话: {task.chat_key}\n提示词: {task.prompt}\n"
        f"状态: {task.status.value}\n模型: {task.model}\n"
        f"尺寸: {task.width}x{task.height}\n帧数: {task.num_frames}\n帧率: {task.frame_rate}\n"
        f"创建时间: {create_time}\n更新时间: {update_time}\n"
    )
    if task.reason:
        info += f"原因: {task.reason}\n"
    if task.video_urls:
        info += f"视频URL: {', '.join(task.video_urls)}\n"
    if task.error_message:
        info += f"错误信息: {task.error_message}\n"
    return info


async def get_tasks_page(page: int) -> Tuple[List[VideoTask], int, int]:
    gt = await _load_tasks()
    return (
        gt.get_tasks_page(page, config.ITEMS_PER_PAGE),
        gt.get_total_pages(config.ITEMS_PER_PAGE),
        len(gt.get_all_tasks()),
    )
