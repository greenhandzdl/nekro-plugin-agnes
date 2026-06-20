"""视频生成工具 — 对齐 tongyi_wanx 架构

方法类型:
- BEHAVIOR: 创建/取消/审批/拒绝（Agent 触发，返回描述性文本）
- TOOL: 查询/列表/详情（返回结构化结果）
"""

import time
from typing import List

from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.base import SandboxMethodType
from nekro_agent.tools.common_util import limited_text_output

from .conf import config, plugin
from .models import TaskStatus
from .service import (
    approve_video_task,
    cancel_current_video_task as _cancel_task,
    create_video_task,
    format_task_info,
    get_tasks_page,
    get_video_task,
    prepare_generation_prompt,
    validate_video_args,
)


# ---------------------------------------------------------------------------
# Prompt 注入
# ---------------------------------------------------------------------------


@plugin.mount_prompt_inject_method(name="agnes_video_prompt_inject")
async def agnes_video_prompt_inject(_ctx: AgentCtx):
    """注入当前视频任务状态到 Agent 上下文"""
    if not _ctx.chat_key:
        return ""

    from .service import _load_chat, _load_tasks

    chat_data = await _load_chat(_ctx.chat_key)
    global_tasks = await _load_tasks()

    model = config.VIDEO_MODEL
    require_approval = config.REQUIRE_ADMIN_APPROVAL

    # 当前任务
    status_info = ""
    is_idle = True
    if chat_data.current_task_id:
        task = global_tasks.get_task(chat_data.current_task_id)
        if task:
            is_idle = False
            start_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.create_time))
            elapsed = int(time.time()) - task.create_time
            status_info = (
                f"[Current Video Task]\n"
                f"- TaskID: {task.task_id}\n"
                f"- Prompt: {task.prompt}\n"
                f"- Model: {task.model}\n"
                f"- Size: {task.width}x{task.height}\n"
                f"- Status: {task.status.value}\n"
                f"- Start: {start_time}\n"
                f"- Elapsed: {elapsed}s\n"
            )
            if task.reason:
                status_info += f"- Reason: {task.reason}\n"
            if task.error_message:
                status_info += f"- Error: {task.error_message}\n"
            if task.video_urls:
                urls_str = ", ".join(limited_text_output(u, limit=32) for u in task.video_urls)
                status_info += f"- VideoURL: {urls_str}\n"
    else:
        status_info = (
            f"[No active video task]\n"
            f"Use create_video(prompt, ...) to start a new task.\n"
            f"Default model: {model}.\n"
        )

    # 历史记录
    history_info = ""
    if chat_data.history_records:
        recent = sorted(chat_data.history_records, key=lambda x: x.create_time, reverse=True)[:config.DISPLAY_HISTORY]
        history_info = f"[Last {config.DISPLAY_HISTORY} History]\n"
        for i, record in enumerate(recent, 1):
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.create_time))
            urls_str = ", ".join(limited_text_output(u, limit=32) for u in record.video_urls)
            history_info += (
                f"{i}. Prompt: {record.prompt} ({t})\n"
                f"   TaskID: {record.task_id}\n"
                f"   VideoURL: {urls_str}\n"
            )

    background_info = (
        f"[Plugin Usage]\n"
        f"- create_video(prompt, ..., reason=...) to request video.\n"
        f"- get_video_by_task_id(task_id=...) to get video URL.\n"
        f"- cancel_current_video_task() to cancel.\n"
        f"- approve_video_task(task_id=...) / reject_video_task(task_id=...)\n"
        f"- list_video_tasks(page=...) / get_video_task_info(task_id=...)\n"
        f"- Model: {model}, approval: {'yes' if require_approval else 'no'}.\n"
        f"- State: {'idle' if is_idle else 'busy'}.\n"
        "Notice: Injected info is only visible to YOU, not to the user."
    )

    result = background_info + "\n" + status_info
    if history_info:
        result += "\n" + history_info
    return result


# ---------------------------------------------------------------------------
# 视频创建 (BEHAVIOR)
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="create_video",
    description="创建视频任务。支持文生视频、图生视频、多图视频和关键帧动画。",
)
async def create_video(
    _ctx: AgentCtx,
    prompt: str,
    reason: str = "",
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
    """创建 Agnes AI 视频生成任务（异步处理）。"""
    if not _ctx.chat_key:
        return "无法创建视频任务：未获取到聊天会话信息。"

    # 检查进行中任务
    from .service import _load_chat, _load_tasks
    chat_data = await _load_chat(_ctx.chat_key)
    if chat_data.current_task_id:
        gt = await _load_tasks()
        existing = gt.get_task(chat_data.current_task_id)
        if existing and existing.status in (TaskStatus.PENDING, TaskStatus.APPROVED, TaskStatus.PROCESSING):
            return (
                f"当前已有正在进行的视频任务，请等待完成后再创建。\n"
                f"任务ID: {existing.task_id}\n状态: {existing.status.value}\n"
                f"提示词: {existing.prompt}\n"
                f"可使用 get_video_by_task_id(task_id=\"{existing.task_id}\") 查询进度。"
            )

    try:
        validate_video_args(num_frames, frame_rate, height, width)
    except ValueError as e:
        return f"参数错误: {e}"

    try:
        async with __import__("httpx").AsyncClient() as client:
            prepared_prompt, _ = await prepare_generation_prompt(client, prompt, translate_prompt)

        task = await create_video_task(
            task_id=f"task_{int(time.time() * 1000)}",
            prompt=prepared_prompt, ctx=_ctx,
            reason=reason or None, model=config.VIDEO_MODEL,
            height=height, width=width, num_frames=num_frames, frame_rate=frame_rate,
            nis=num_inference_steps, seed=seed,
            neg=negative_prompt, img=image_url,
            imgs=image_urls, mode=mode,
        )

        approval_msg = " (需要管理员审批)" if config.REQUIRE_ADMIN_APPROVAL else ""
        return (
            f"视频任务已创建{approval_msg}\n"
            f"任务ID: {task.task_id}\n状态: {task.status.value}\n"
            f"提示词: {prepared_prompt}\n"
            f"使用 get_video_by_task_id(task_id=\"{task.task_id}\") 查询进度。"
        )
    except Exception as e:
        logger.exception(f"视频创建失败: {e}")
        return f"视频创建失败: {e}"


# ---------------------------------------------------------------------------
# 取消当前任务 (BEHAVIOR)
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="cancel_current_video_task",
    description="取消当前会话的视频生成任务。",
)
async def cancel_current_video_task(_ctx: AgentCtx) -> str:
    """取消当前会话的视频生成任务。"""
    if not _ctx.chat_key:
        return "未获取到聊天会话信息。"
    task = await _cancel_task(_ctx.chat_key)
    if not task:
        return "当前没有正在进行的视频任务。"
    return f"任务 {task.task_id} 已取消。"


# ---------------------------------------------------------------------------
# 按 task_id 获取视频 URL (TOOL)
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="get_video_by_task_id",
    description="按任务 ID 获取视频 URL。",
)
async def get_video_by_task_id(_ctx: AgentCtx, task_id: str) -> str:
    """按任务 ID 获取已完成视频任务的 URL。"""
    task = await get_video_task(task_id)
    if not task:
        return f"任务 {task_id} 不存在。"
    if not task.video_urls:
        return f"任务 {task_id} 暂无视频 URL。状态: {task.status.value}"
    return "\n".join(task.video_urls)


# ---------------------------------------------------------------------------
# 管理命令 (BEHAVIOR)
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="approve_video_task",
    description="批准一个待审批的视频生成任务。",
)
async def approve_video_task_handler(_ctx: AgentCtx, task_id: str) -> str:
    """批准视频生成任务。"""
    success = await approve_video_task(task_id)
    if success:
        return f"已批准任务 {task_id}，开始执行视频生成。"
    return f"批准任务 {task_id} 失败，请检查任务状态。"


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="reject_video_task",
    description="拒绝一个待审批的视频生成任务。",
)
async def reject_video_task_handler(_ctx: AgentCtx, task_id: str) -> str:
    """拒绝视频生成任务。"""
    success = await reject_video_task(task_id)
    if success:
        return f"已拒绝任务 {task_id}。"
    return f"拒绝任务 {task_id} 失败，请检查任务状态。"


# ---------------------------------------------------------------------------
# 任务查询 (TOOL)
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="list_video_tasks",
    description="分页查询所有视频任务列表。",
)
async def list_video_tasks(_ctx: AgentCtx, page: int = 1) -> str:
    """分页查询视频任务列表。"""
    try:
        page = max(1, int(page))
    except (ValueError, TypeError):
        return "页码必须是一个正整数。"

    tasks_page, total_pages, total_tasks = await get_tasks_page(page)
    if not tasks_page:
        return "没有找到任何任务。"

    info = f"任务列表 (第 {page}/{total_pages} 页，共 {total_tasks} 个任务):\n\n"
    for i, task in enumerate(tasks_page, 1):
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(task.create_time))
        info += (
            f"{i}. 任务ID: {task.task_id}\n"
            f"   提示词: {task.prompt}\n"
            f"   状态: {task.status.value}\n"
            f"   创建时间: {t}\n\n"
        )
    if page < total_pages:
        info += f"使用 list_video_tasks(page={page + 1}) 查看下一页"
    return info


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="get_video_task_info",
    description="查询指定任务的详细信息。",
)
async def get_video_task_info(_ctx: AgentCtx, task_id: str) -> str:
    """查询任务详情。"""
    task = await get_video_task(task_id)
    if not task:
        return f"任务 {task_id} 不存在。"
    return f"任务详情:\n\n{format_task_info(task)}"


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件资源。"""
    logger.info("Agnes AI Generation 插件资源已清理。")
