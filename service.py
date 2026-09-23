"""业务逻辑：任务管理、API 调用、异步视频处理（框架任务系统驱动）

状态流转:
  创建 → PENDING (需审批) / QUEUED (不需审批)
  PENDING → APPROVED (审批通过) / REJECTED (审批拒绝/超时)
  APPROVED/QUEUED → PROCESSING (API 生成中) → COMPLETED / FAILED

视频轮询由框架异步任务系统 (mount_async_task + TaskRunner) 承载，
插件重启后由 mount_init_method 恢复未到终态的任务。
"""

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx
from nekro_agent.api.core import logger
from nekro_agent.api.message import push_system
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.task import AsyncTaskHandle, TaskCtl, task as task_api

from .conf import config, plugin, store
from .models import ChatSessionData, GlobalTaskData, TaskStatus, VideoTask

IMAGE_SIZE_TIERS = {"1K", "2K", "3K", "4K"}
VIDEO_SIZE_TIERS = {"720P", "1080P", "1K", "2K"}
RATIO_SET = {"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"}
# 视频比例是图片比例的子集：2:3 / 3:2 会被 API 拒绝
VIDEO_RATIO_SET = {"16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
VIDEO_MODES = {"text", "keyframe", "reference"}
_ENV_NAMES = ("AGNES_API_KEY", "AGNES_API_TOKEN", "APIHUB_AGNES_API_KEY")
_STORE_TASKS = "agnes_video_tasks"
_STORE_CHAT = "agnes_chat"
_TASK_TYPE = "agnes_video"
_TR_SYS = (
    "Translate the user's image/video generation prompt into fluent English. "
    "Preserve all concrete visual details, style words, camera motion, lighting, "
    "composition constraints, and negative instructions. Return only the English prompt."
)


# ---------------------------------------------------------------------------
# 旧配置兼容：v2.x 不再构建的模型归一
# ---------------------------------------------------------------------------


def _normalize_video_model(model: str) -> str:
    """旧配置的 agnes-video-v2.0 归一到 2.5-flash（免费）。

    v2.0 仍能在 GET /v1/models 里列到，但它要求旧的 ti2vid/keyframes/
    multi_reference 参数形态，本插件已不再构建这类请求。
    """
    if not model or model.startswith("agnes-video-v2.0"):
        logger.warning(f"视频模型 {model!r} 不再受支持，自动改用 agnes-video-2.5-flash")
        return "agnes-video-2.5-flash"
    return model


def _normalize_text_model(model: str) -> str:
    """1.x 默认模型 agnes-2.0-flash 归一到 agnes-2.5-flash。"""
    if not model or model == "agnes-2.0-flash":
        return "agnes-2.5-flash"
    return model


def _is_video_flash(model: str) -> bool:
    return model.endswith("2.5-flash")


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


def _get_base_url() -> str:
    """获取清理后的 BASE_URL（去除末尾空格和 /）"""
    return config.BASE_URL.strip().rstrip("/")


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
    base = _get_base_url()
    url = f"{base}/{path.strip().lstrip('/')}"
    logger.debug(f"API 请求: {method} {url}")
    r = None
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
        "model": _normalize_text_model(config.TEXT_MODEL),
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
    """提取视频 URL。2.5 系列完成后在顶层 `url`，兼容 `video_url` 与 data 数组。"""
    urls: List[str] = []
    for k in ("url", "video_url"):
        v = data.get(k)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            urls.append(v)
    meta = data.get("metadata")
    if isinstance(meta, dict):
        urls.extend(extract_video_urls(meta))
    for item in data.get("data", []):
        if isinstance(item, dict):
            urls.extend(extract_video_urls(item))
    return list(dict.fromkeys(urls))


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def validate_size(value: Optional[str]) -> None:
    """图片尺寸：兼容档位制 (1K/2K/3K/4K) 与历史精确尺寸写法。"""
    if not value:
        return
    if value.upper() in IMAGE_SIZE_TIERS:
        return
    if re.match(r"^[1-9]\d*x[1-9]\d*$", value):
        return
    raise ValueError(f"无效尺寸: {value}。期望档位 1K/2K/3K/4K 或 WIDTHxHEIGHT（如 1024x768）。")


def validate_ratio(value: Optional[str]) -> None:
    if value and value not in RATIO_SET:
        raise ValueError(f"无效宽高比: {value}。支持: {', '.join(sorted(RATIO_SET))}")


def derive_mode(
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    images: Optional[List[str]] = None,
    audios: Optional[List[str]] = None,
    videos: Optional[List[Dict[str, Any]]] = None,
) -> str:
    if first_frame or last_frame:
        return "keyframe"
    if images or audios or videos:
        return "reference"
    return "text"


def validate_video_args(
    mode: str,
    seconds: str,
    size: str,
    model: str,
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    images: Optional[List[str]] = None,
    audios: Optional[List[str]] = None,
    videos: Optional[List[Dict[str, Any]]] = None,
    aspect_ratio: str = "16:9",
) -> None:
    """按 Agnes Video 2.5 系列规则校验参数。"""
    if mode not in VIDEO_MODES:
        raise ValueError(f"无效 mode: {mode}。支持 text/keyframe/reference。")
    if aspect_ratio not in VIDEO_RATIO_SET:
        raise ValueError(f"无效 aspect_ratio: {aspect_ratio}。视频支持: {', '.join(sorted(VIDEO_RATIO_SET))}。")
    try:
        secs = int(seconds)
    except (TypeError, ValueError):
        raise ValueError(f"无效 seconds: {seconds}。必须是 4-12 的整数字符串，例如 \"5\"。")
    if not 4 <= secs <= 12:
        raise ValueError(f"无效 seconds: {seconds}。支持 4-12 秒。")
    if size not in VIDEO_SIZE_TIERS:
        raise ValueError(f"无效 size: {size}。支持 {', '.join(sorted(VIDEO_SIZE_TIERS))}。")
    if images and len(images) > 8:
        raise ValueError("参考图片最多 8 张。")
    if audios and len(audios) > 3:
        raise ValueError("参考音频最多 3 段。")
    if videos and len(videos) > 1:
        raise ValueError("参考视频最多 1 个。")
    if _is_video_flash(model):
        if size != "720P":
            raise ValueError("agnes-video-2.5-flash 仅支持 size=\"720P\"。")
        if images and len(images) > 5:
            raise ValueError("agnes-video-2.5-flash 参考图片最多 5 张。")
        if videos:
            raise ValueError("agnes-video-2.5-flash 不支持参考视频输入。")
    media_count = sum(bool(x) for x in (first_frame, last_frame)) + len(images or []) + len(audios or []) + len(videos or [])
    if mode == "text" and media_count:
        raise ValueError("text 模式不接受任何媒体输入（first_frame/last_frame/images/audios/videos）。")
    if mode == "keyframe" and not (first_frame or last_frame):
        raise ValueError("keyframe 模式需要 first_frame 或 last_frame 至少一个。")
    if mode == "reference" and not (images or audios or videos):
        raise ValueError("reference 模式需要 images/audios/videos 至少一类非空。")
    if mode == "keyframe" and (images or audios or videos):
        raise ValueError("keyframe 模式不允许 images/audios/videos。")
    if mode == "reference" and (first_frame or last_frame):
        raise ValueError("reference 模式不允许 first_frame/last_frame。")
    if media_count > 12:
        raise ValueError("单次请求媒体文件总数不得超过 12 个。")


# ---------------------------------------------------------------------------
# Payload 构建
# ---------------------------------------------------------------------------


def build_video_payload(
    prompt: str,
    model: str,
    mode: str,
    seconds: str,
    size: str,
    aspect_ratio: str,
    seed: Optional[int] = None,
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    images: Optional[List[str]] = None,
    audios: Optional[List[str]] = None,
    videos: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """构建 POST /v1/videos 请求体（Agnes Video 2.5 系列）。"""
    model = _normalize_video_model(model)
    p: Dict[str, Any] = {
        "model": model, "prompt": prompt, "mode": mode,
        "seconds": str(seconds), "size": size, "aspect_ratio": aspect_ratio,
    }
    if seed is not None:
        p["seed"] = seed
    if mode == "keyframe":
        if first_frame:
            p["first_frame"] = first_frame
        if last_frame:
            p["last_frame"] = last_frame
    elif mode == "reference":
        if images:
            p["images"] = images
        if audios:
            p["audios"] = audios
        if videos:
            p["videos"] = videos
    return p


def _poll_path(video_id: str, model: str) -> str:
    return f"/agnesapi?video_id={quote(video_id)}&model_name={quote(model)}"


# ---------------------------------------------------------------------------
# 视频任务 — 创建 / 审批 / 取消
# ---------------------------------------------------------------------------


async def create_video_task(
    prompt: str, ctx: AgentCtx,
    reason: Optional[str] = None, model: str = "",
    mode: str = "text", seconds: str = "5", size: str = "720P", aspect_ratio: str = "16:9",
    seed: Optional[int] = None,
    first_frame: Optional[str] = None, last_frame: Optional[str] = None,
    images: Optional[List[str]] = None, audios: Optional[List[str]] = None,
    videos: Optional[List[Dict[str, Any]]] = None,
) -> VideoTask:
    """创建视频任务并挂到框架异步任务系统。

    API 提交与轮询都在任务协程内完成，本函数只负责落库和启动。
    """
    if not ctx.from_chat_key:
        raise ValueError("from_chat_key is required")
    model = _normalize_video_model(model or config.VIDEO_MODEL)
    validate_video_args(
        mode, seconds, size, model, first_frame, last_frame, images, audios, videos,
        aspect_ratio=aspect_ratio,
    )

    gt = await _load_tasks()
    task = VideoTask.create(
        task_id=gt.get_next_task_id(), chat_key=ctx.from_chat_key, prompt=prompt,
        reason=reason, model=model, mode=mode, seconds=seconds, size=size,
        aspect_ratio=aspect_ratio, seed=seed,
        first_frame=first_frame, last_frame=last_frame,
        image_urls=images, audio_urls=audios, video_refs=videos,
    )
    task.status = TaskStatus.PENDING if config.REQUIRE_ADMIN_APPROVAL else TaskStatus.QUEUED

    gt.add_task(task)
    await _save_tasks(gt)

    chat_data = await _load_chat(ctx.from_chat_key)
    chat_data.current_task_id = task.task_id
    await _save_chat(ctx.from_chat_key, chat_data)

    await task_api.start(_TASK_TYPE, task.task_id, task.chat_key, plugin, task.task_id)
    return task


async def approve_video_task(task_id: str) -> bool:
    """批准视频任务。

    - 有活着的任务协程在等审批: 标记 APPROVED 并 notify 唤醒
    - 协程不存在（重启后）: 标记 APPROVED 并重新挂载任务协程
    """
    gt = await _load_tasks()
    task = gt.get_task(task_id)
    if not task or task.status not in (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.APPROVED):
        logger.warning(f"批准失败: {task_id} 不存在或状态不可审批: {task.status if task else 'N/A'}")
        return False

    if task.status == TaskStatus.PENDING:
        await update_task_status(task_id, TaskStatus.APPROVED)

    handle = task_api.get_handle(_TASK_TYPE, task_id)
    if handle and handle.notify("approval", True):
        return True
    if not task_api.is_running(_TASK_TYPE, task_id):
        await task_api.start(_TASK_TYPE, task_id, task.chat_key, plugin, task_id)
    return True


async def reject_video_task(task_id: str) -> bool:
    """拒绝视频任务: PENDING/QUEUED → REJECTED (等同 CANCELED)"""
    gt = await _load_tasks()
    task = gt.get_task(task_id)
    if not task or task.status not in (TaskStatus.PENDING, TaskStatus.QUEUED):
        logger.warning(f"拒绝失败: {task_id} 不存在或状态不可拒绝 (当前: {task.status if task else 'N/A'})")
        return False
    await update_task_status(task_id, TaskStatus.REJECTED, error_message="管理员拒绝了请求")
    handle = task_api.get_handle(_TASK_TYPE, task_id)
    if handle:
        handle.notify("approval", False)
    return True


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

    await update_task_status(chat_data.current_task_id, TaskStatus.REJECTED, error_message="用户取消")
    handle = task_api.get_handle(_TASK_TYPE, task.task_id)
    if handle:
        handle.notify("approval", False)
    await task_api.cancel(_TASK_TYPE, task.task_id)

    chat_data = await _load_chat(chat_key)
    if chat_data.current_task_id == task.task_id:
        chat_data.current_task_id = None
        await _save_chat(chat_key, chat_data)

    return task


# ---------------------------------------------------------------------------
# 视频任务 — 状态更新
# ---------------------------------------------------------------------------


def _is_terminal_status(status: TaskStatus) -> bool:
    """是否为终态（不再轮询）"""
    return status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.REJECTED)


async def update_task_status(task_id: str, status: TaskStatus, **kwargs) -> None:
    """更新任务状态，终态时清理会话。不允许从终态转移到其他状态。

    通知消息由调用方（任务协程 / 命令处理器）负责发送。
    """
    gt = await _load_tasks()
    existing = gt.get_task(task_id)
    if not existing:
        return
    if _is_terminal_status(existing.status) and not _is_terminal_status(status):
        logger.warning(f"任务 {task_id} 已是终态 {existing.status.value}，不允许更新为 {status.value}")
        return
    if not gt.update_task(task_id, status=status, **kwargs):
        return
    await _save_tasks(gt)

    task = gt.get_task(task_id)
    if not task:
        return

    if _is_terminal_status(status):
        chat_data = await _load_chat(task.chat_key)
        if chat_data.current_task_id == task_id:
            chat_data.current_task_id = None
            await _save_chat(task.chat_key, chat_data)

    if status == TaskStatus.COMPLETED and task.video_urls:
        chat_data = await _load_chat(task.chat_key)
        chat_data.add_history(task.prompt, task.video_urls, task.task_id)
        await _save_chat(task.chat_key, chat_data)


# ---------------------------------------------------------------------------
# 视频任务 — 异步任务协程（框架 TaskRunner 承载）
# ---------------------------------------------------------------------------


@plugin.mount_async_task(_TASK_TYPE)
async def agnes_video_task(handle: AsyncTaskHandle, task_id: str):
    """视频任务生命周期：审批等待 → API 提交 → 轮询 → 终态通知。"""
    gt = await _load_tasks()
    task = gt.get_task(task_id)
    if not task:
        yield TaskCtl.fail(f"任务 {task_id} 不存在")
        return
    if _is_terminal_status(task.status):
        yield TaskCtl.cancel("任务已是终态")
        return

    # --- 审批阶段 ---
    if task.status == TaskStatus.PENDING:
        await _send_approval_request(task)
        yield TaskCtl.report_progress("等待管理员审批")
        try:
            approved = await handle.wait("approval", timeout=config.APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            await update_task_status(task_id, TaskStatus.REJECTED, error_message="审批超时")
            await handle.notify_agent(f"【视频生成已取消】\n任务ID: {task_id}\n审批超时，任务已拒绝。", trigger=False)
            yield TaskCtl.fail("审批超时")
            return
        except asyncio.CancelledError:
            return
        gt = await _load_tasks()
        task = gt.get_task(task_id)
        if not task or _is_terminal_status(task.status):
            yield TaskCtl.cancel("任务已终止")
            return
        if not approved:
            if task.status != TaskStatus.REJECTED:
                await update_task_status(task_id, TaskStatus.REJECTED, error_message="审批未通过")
            yield TaskCtl.fail("审批被拒绝")
            return
        if task.status != TaskStatus.APPROVED:
            await update_task_status(task_id, TaskStatus.APPROVED)

    # --- 提交阶段 ---
    if not task.video_id:
        payload = build_video_payload(
            task.prompt, task.model, task.mode, task.seconds, task.size, task.aspect_ratio,
            seed=task.seed, first_frame=task.first_frame, last_frame=task.last_frame,
            images=task.image_urls, audios=task.audio_urls, videos=task.video_refs,
        )
        try:
            async with httpx.AsyncClient() as client:
                created = await _req(client, "POST", "/v1/videos", payload)
        except Exception as e:
            error_msg = str(e)
            logger.error(f"创建视频任务 API 调用失败: {error_msg}")
            await update_task_status(task_id, TaskStatus.FAILED, error_message=error_msg)
            await handle.notify_agent(
                f"【视频生成失败】\n任务ID: {task_id}\n提示词: {task.prompt}\n错误信息: {error_msg}", trigger=True
            )
            yield TaskCtl.fail(error_msg)
            return

        api_video_id = created.get("video_id") or created.get("videoId") or created.get("id")
        api_st = str(created.get("status", "")) if created.get("status") is not None else ""
        if not api_video_id:
            error_msg = f"API 未返回 video_id: {json.dumps(created, ensure_ascii=False)[:200]}"
            await update_task_status(task_id, TaskStatus.FAILED, error_message=error_msg)
            await handle.notify_agent(
                f"【视频生成失败】\n任务ID: {task_id}\n提示词: {task.prompt}\n错误信息: {error_msg}", trigger=True
            )
            yield TaskCtl.fail(error_msg)
            return
        logger.info(f"视频任务已提交: {task_id} video_id={api_video_id} status={api_st}")
        gt = await _load_tasks()
        gt.update_task(task_id, video_id=api_video_id, status=TaskStatus.from_api(api_st) if api_st else TaskStatus.QUEUED)
        await _save_tasks(gt)
        yield TaskCtl.report_progress("任务已提交，等待生成")

    # --- 轮询阶段 ---
    async with httpx.AsyncClient() as client:
        for i in range(config.MAX_POLL_ATTEMPTS):
            await asyncio.sleep(config.POLL_INTERVAL)
            if handle.is_cancelled:
                yield TaskCtl.cancel("任务已取消")
                return

            gt = await _load_tasks()
            task = gt.get_task(task_id)
            if not task or _is_terminal_status(task.status):
                logger.info(f"任务 {task_id} 已处于终态 {task.status.value if task else 'N/A'}，停止轮询")
                yield TaskCtl.cancel("任务已终止")
                return

            try:
                data = await _req(client, "GET", _poll_path(task.video_id, task.model))
            except Exception as e:
                logger.warning(f"轮询 {task_id} 第 {i + 1} 次失败: {e}")
                continue

            status_raw = str(data.get("status", "")).lower()

            if status_raw == "completed":
                urls = extract_video_urls(data)
                if not urls:
                    error_msg = f"任务完成但未找到视频 URL: {json.dumps(data, ensure_ascii=False)[:200]}"
                    await update_task_status(task_id, TaskStatus.FAILED, error_message=error_msg)
                    await handle.notify_agent(
                        f"【视频生成失败】\n任务ID: {task_id}\n提示词: {task.prompt}\n错误信息: {error_msg}", trigger=True
                    )
                    yield TaskCtl.fail(error_msg)
                    return
                await update_task_status(task_id, TaskStatus.COMPLETED, video_urls=urls, progress=100)
                msg = (
                    f"【视频生成完成】\n任务ID: {task_id}\n提示词: {task.prompt}\n"
                    f"视频已生成完毕!\n视频URL:\n" + "\n".join(urls) +
                    "\n(use `send_msg_file` to send the video)"
                )
                await handle.notify_agent(msg, trigger=True)
                yield TaskCtl.success("视频生成完成", data=urls)
                return

            if status_raw == "failed":
                err = data.get("error") or data.get("error_message") or data.get("message") or "未知错误"
                if isinstance(err, dict):
                    err = err.get("message") or json.dumps(err, ensure_ascii=False)
                err = str(err)
                await update_task_status(task_id, TaskStatus.FAILED, error_message=err)
                await handle.notify_agent(
                    f"【视频生成失败】\n任务ID: {task_id}\n提示词: {task.prompt}\n错误信息: {err}", trigger=True
                )
                yield TaskCtl.fail(err)
                return

            st = TaskStatus.from_api(status_raw)
            progress = _parse_progress(data.get("progress"))
            gt = await _load_tasks()
            gt.update_task(task_id, status=st, progress=progress)
            await _save_tasks(gt)
            yield TaskCtl.report_progress(f"生成中: {status_raw or st.value} {progress}%", percent=progress)

    error_msg = "任务超时"
    await update_task_status(task_id, TaskStatus.FAILED, error_message=error_msg)
    await handle.notify_agent(
        f"【视频生成失败】\n任务ID: {task_id}\n提示词: {task.prompt}\n错误信息: {error_msg}", trigger=True
    )
    yield TaskCtl.fail(error_msg)


def _parse_progress(value: Any) -> int:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return 0
    if p <= 1:
        p *= 100
    return max(0, min(100, int(p)))


async def _send_approval_request(task: VideoTask) -> None:
    manager_msg = (
        f"【视频生成申请】\n任务ID: {task.task_id}\n会话: {task.chat_key}\n"
        f"提示词: {task.prompt}\n模型: {task.model}\n模式: {task.mode}\n"
        f"时长: {task.seconds}s 尺寸: {task.size} 比例: {task.aspect_ratio}\n"
    )
    if task.reason:
        manager_msg += f"原因: {task.reason}\n"
    manager_msg += (
        f"批准: /agnes_y {task.task_id}\n"
        f"拒绝: /agnes_n {task.task_id}"
    )
    try:
        target = config.MANAGER_CHAT_KEY or task.chat_key
        await push_system(chat_key=target, message=manager_msg)
    except Exception as e:
        logger.error(f"发送审批消息失败: {e}")


async def recover_unfinished_tasks() -> int:
    """插件启动时恢复未到终态的视频任务。返回恢复数量。"""
    gt = await _load_tasks()
    legacy = [
        t for t in gt.get_all_tasks()
        if not _is_terminal_status(t.status) and not t.model.startswith("agnes-video-2.5")
    ]
    if legacy:
        for t in legacy:
            gt.update_task(
                t.task_id, status=TaskStatus.FAILED,
                error_message="插件已升级到 Agnes Video 2.5 系列，1.x 任务无 video_id 可续轮询",
            )
        await _save_tasks(gt)
        ids = ", ".join(t.task_id for t in legacy[:5])
        logger.warning(f"{len(legacy)} 个 1.x 遗留任务已标记失败（插件不再支持其模型与参数形态）: {ids}")

    recovered = 0
    for t in gt.get_all_tasks():
        if _is_terminal_status(t.status):
            continue
        if task_api.is_running(_TASK_TYPE, t.task_id):
            continue
        try:
            await task_api.start(_TASK_TYPE, t.task_id, t.chat_key, plugin, t.task_id)
            recovered += 1
            logger.info(f"恢复视频任务轮询: {t.task_id} (status={t.status.value})")
        except ValueError as e:
            logger.warning(f"恢复任务 {t.task_id} 失败: {e}")
    return recovered


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


async def get_video_task(task_id: str) -> Optional[VideoTask]:
    gt = await _load_tasks()
    return gt.get_task(task_id)


def format_task_info(task: VideoTask) -> str:
    """格式化任务信息"""
    create_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.create_time))
    update_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.update_time))

    info = (
        f"任务ID: {task.task_id}\n会话: {task.chat_key}\n提示词: {task.prompt}\n"
        f"状态: {task.status.value}\n模型: {task.model}\n"
        f"模式: {task.mode}\n时长: {task.seconds}s\n分辨率: {task.size}\n比例: {task.aspect_ratio}\n"
        f"创建时间: {create_time}\n更新时间: {update_time}\n"
    )
    if task.status in (TaskStatus.QUEUED, TaskStatus.PROCESSING) and task.progress:
        info += f"进度: {task.progress}%\n"
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
