"""视频生成工具 — Agnes Video 2.5 系列 + 框架命令/任务系统

方法类型:
- BEHAVIOR: 创建/取消/审批/拒绝（Agent 触发，返回描述性文本，不触发再次调用）
- TOOL: 查询/列表/详情（返回结构化结果，Agent 可继续处理）
- mount_command: 管理员命令（/agnes_y, /agnes_n, /agnes_list, /agnes_info, /agnes_help）

状态流转:
  创建 → PENDING (需审批) / QUEUED (不需审批)
  PENDING → APPROVED (审批通过) / REJECTED (审批拒绝)
  APPROVED/QUEUED → PROCESSING (API 生成中) → COMPLETED / FAILED
"""

import time
from typing import Annotated, Any, Dict, List, Optional

import httpx
from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.command.base import CommandPermission
from nekro_agent.services.command.ctl import CmdCtl
from nekro_agent.services.command.schemas import Arg, CommandExecutionContext, CommandResponse
from nekro_agent.services.plugin.base import SandboxMethodType
from nekro_agent.tools.common_util import limited_text_output

from .conf import config, plugin
from .models import TaskStatus
from .service import (
    _load_chat,
    _load_tasks,
    approve_video_task as _approve_task,
    cancel_current_video_task as _cancel_task,
    create_video_task,
    derive_mode,
    format_task_info,
    get_tasks_page,
    get_video_task,
    prepare_generation_prompt,
    recover_unfinished_tasks,
    reject_video_task as _reject_task,
)


# ---------------------------------------------------------------------------
# 启动恢复 — 插件重载/框架重启后续跑未终态任务
# ---------------------------------------------------------------------------


@plugin.mount_init_method()
async def init_recovery():
    """插件初始化：恢复未到终态的视频任务轮询/审批。"""
    recovered = await recover_unfinished_tasks()
    if recovered:
        logger.info(f"Agnes 视频插件启动恢复了 {recovered} 个未完成任务")


# ---------------------------------------------------------------------------
# Prompt 注入 — Agent 每次对话都会看到这段信息
# ---------------------------------------------------------------------------


@plugin.mount_prompt_inject_method(name="agnes_video_prompt_inject")
async def agnes_video_prompt_inject(_ctx: AgentCtx):
    """注入当前视频任务状态到 Agent 上下文。

    Agent 通过这段信息了解:
    - 插件有哪些功能、怎么调用
    - 当前是否有正在进行的任务
    - 最近的历史记录
    """
    if not _ctx.chat_key:
        return ""

    chat_data = await _load_chat(_ctx.chat_key)
    global_tasks = await _load_tasks()

    model = config.VIDEO_MODEL
    require_approval = config.REQUIRE_ADMIN_APPROVAL

    # 当前任务状态
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
                f"- Model: {task.model} | Mode: {task.mode}\n"
                f"- Duration: {task.seconds}s | Size: {task.size} | Ratio: {task.aspect_ratio}\n"
                f"- Status: {task.status.value} (progress {task.progress}%)\n"
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

    # 插件使用说明（Agent 通过这段文字学习如何调用）
    background_info = (
        f"[Agnes Video Generation Plugin]\n"
        f"Model: {model} | Approval: {'required' if require_approval else 'not required'} | State: {'idle' if is_idle else 'busy'}\n\n"
        f"## Available Functions:\n"
        f"1. create_video(prompt, mode=None, images=[...], first_frame=..., last_frame=..., "
        f"audios=[...], videos=[...], seconds=\"5\", size=\"720P\", aspect_ratio=\"16:9\", reason=...) -> str\n"
        f"   Create a video generation task (Agnes Video 2.5 API).\n"
        f"   - prompt: Video description (English works best, Chinese auto-translated)\n"
        f"   - mode: 'text' | 'keyframe' | 'reference'; auto-derived if omitted\n"
        f"   - first_frame/last_frame: keyframe mode (at least one)\n"
        f"   - images/audios/videos: reference mode (at least one non-empty)\n"
        f"   - seconds: \"4\"-\"12\"; size: 720P/1080P/1K/2K (flash model: 720P only)\n"
        f"   - reason: Why the user wants this video (optional, shown for approval)\n\n"
        f"2. get_video_by_task_id(task_id=...) -> str\n"
        f"   Get the video URL for a completed task.\n\n"
        f"3. cancel_current_video_task() -> str\n"
        f"   Cancel the current session's active video task.\n\n"
        f"4. approve_video_task(task_id=...) / reject_video_task(task_id=...) -> str\n"
        f"   Approve or reject a pending video task.\n\n"
        f"5. list_video_tasks(page=1) -> str\n"
        f"   List all video tasks with pagination.\n\n"
        f"6. get_video_task_info(task_id=...) -> str\n"
        f"   Get detailed info of a specific task.\n\n"
        f"## Status Flow:\n"
        f"PENDING/QUEUED -> APPROVED -> PROCESSING -> COMPLETED/FAILED\n\n"
        f"Note: Injected info is only visible to YOU, not to the user."
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
    description="创建视频任务。支持文生视频、首尾帧关键帧动画和多模态参考生成（Agnes Video 2.5）。",
)
async def create_video(
    _ctx: AgentCtx,
    prompt: str,
    reason: str = "",
    mode: Optional[str] = None,
    first_frame: Optional[str] = None,
    last_frame: Optional[str] = None,
    images: Optional[List[str]] = None,
    audios: Optional[List[str]] = None,
    videos: Optional[List[Dict[str, Any]]] = None,
    seconds: str = "5",
    size: str = "720P",
    aspect_ratio: str = "16:9",
    seed: Optional[int] = None,
    translate_prompt: bool = True,
) -> str:
    """Create a video generation task (Agnes Video 2.5 series).

    Creates a video generation task with Agnes AI. If admin approval is required,
    the task waits for approval before submission; otherwise it starts immediately.
    Generation and progress polling run as a framework background task; you will be
    notified when the video completes or fails.

    Args:
        prompt: Video description. Include subject, action, scene, style, camera, lighting.
            English works best; Chinese will be auto-translated.
            In reference mode you can refer to inputs with <Picture N>, <Audio N>, <Video N>.
            Example: "A cinematic shot of a cat walking on the beach at sunset"
        reason: Why the user wants this video. Shown during approval. Optional.
        mode: 'text' | 'keyframe' | 'reference'. Auto-derived when omitted:
            first/last frame -> keyframe, images/audios/videos -> reference, else text.
        first_frame: First frame image URL (keyframe mode). At least one of first/last.
        last_frame: Last frame image URL (keyframe mode).
        images: Reference image URLs (reference mode), max 8 (flash model: max 5).
        audios: Reference audio URLs (reference mode), max 3, 2-12s each.
        videos: Reference videos, list of {"url": ..., "start_seconds": ..., "require_audio": ...}.
            Max 1, 2-12s. NOT supported by the flash model.
        seconds: Video duration, string "4"-"12". Default "5".
        size: Resolution tier: "720P" | "1080P" | "1K" | "2K". Default "720P".
            The free agnes-video-2.5-flash model only supports "720P".
        aspect_ratio: "16:9" (default), "9:16", "1:1", "4:3", "3:4", "21:9".
        seed: Random seed for reproducibility. Optional.
        translate_prompt: Auto-translate non-English prompts. Default True.

    Returns:
        Text describing task creation result, including task_id and status.
        On failure, returns error message.

    Examples:
        Text-to-video:
        create_video(prompt="A cat walking on the beach at sunset")

        Keyframe animation (single image as first frame):
        create_video(prompt="Animate subtle camera push-in",
                     first_frame="https://example.com/image.png")

        Keyframe transition between two frames:
        create_video(prompt="Smooth cinematic transition",
                     first_frame="https://a.png", last_frame="https://b.png")

        Reference generation (image + audio):
        create_video(prompt="<Picture 1> walks to the microphone and sings <Audio 1>...",
                     images=["https://a.png"], audios=["https://b.mp3"])

        With approval reason:
        create_video(prompt="Funny cat video", reason="User wants a birthday gift")
    """
    if not _ctx.chat_key:
        return "无法创建视频任务：未获取到聊天会话信息。"

    # 检查进行中任务
    chat_data = await _load_chat(_ctx.chat_key)
    if chat_data.current_task_id:
        gt = await _load_tasks()
        existing = gt.get_task(chat_data.current_task_id)
        if existing and existing.status in (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.APPROVED, TaskStatus.PROCESSING):
            return (
                f"当前已有正在进行的视频任务，请等待完成后再创建。\n"
                f"任务ID: {existing.task_id}\n状态: {existing.status.value}\n"
                f"提示词: {existing.prompt}\n"
                f"可使用 get_video_by_task_id(task_id=\"{existing.task_id}\") 查询进度。"
            )

    resolved_mode = mode or derive_mode(first_frame, last_frame, images, audios, videos)

    try:
        prepared_prompt = prompt
        if translate_prompt:
            async with httpx.AsyncClient() as client:
                prepared_prompt, _ = await prepare_generation_prompt(client, prompt, True)
        task = await create_video_task(
            prompt=prepared_prompt, ctx=_ctx,
            reason=reason or None, model=config.VIDEO_MODEL,
            mode=resolved_mode, seconds=seconds, size=size, aspect_ratio=aspect_ratio,
            seed=seed, first_frame=first_frame, last_frame=last_frame,
            images=images, audios=audios, videos=videos,
        )
    except ValueError as e:
        return f"参数错误: {e}"
    except Exception as e:
        logger.exception(f"视频创建失败: {e}")
        return f"视频创建失败: {e}"

    approval_msg = " (需要管理员审批)" if config.REQUIRE_ADMIN_APPROVAL else ""
    return (
        f"视频任务已创建{approval_msg}\n"
        f"任务ID: {task.task_id}\n状态: {task.status.value}\n"
        f"模式: {task.mode} | 时长: {task.seconds}s | 分辨率: {task.size} {task.aspect_ratio}\n"
        f"提示词: {prepared_prompt}\n"
        f"生成完成后会自动通知；可用 get_video_task_info(task_id=\"{task.task_id}\") 查询进度。"
    )


# ---------------------------------------------------------------------------
# 取消当前任务 (BEHAVIOR)
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="cancel_current_video_task",
    description="取消当前会话的视频生成任务。",
)
async def cancel_current_video_task(_ctx: AgentCtx) -> str:
    """Cancel the current session's video generation task.

    Cancels the active video task for this session, if any.
    The task status will be set to REJECTED.

    Returns:
        Text describing cancellation result.

    Examples:
        cancel_current_video_task()
    """
    if not _ctx.chat_key:
        return "未获取到聊天会话信息。"
    task = await _cancel_task(_ctx.chat_key)
    if not task:
        return "当前没有正在进行的视频任务。"
    return f"任务 {task.task_id} 已取消。"


# ---------------------------------------------------------------------------
# 审批/拒绝 (BEHAVIOR)
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="approve_video_task",
    description="批准一个待审批的视频生成任务。",
)
async def approve_video_task(_ctx: AgentCtx, task_id: str) -> str:
    """Approve a pending video generation task.

    Marks the task APPROVED and resumes the background task (wakes it from the
    approval wait, or restarts polling after a framework restart).

    Args:
        task_id: The task ID to approve.

    Returns:
        Text describing approval result.

    Examples:
        approve_video_task(task_id="task_000001")
    """
    success = await _approve_task(task_id)
    if success:
        return f"已批准任务 {task_id}，开始执行视频生成。"
    return f"批准任务 {task_id} 失败，请检查任务状态。"


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="reject_video_task",
    description="拒绝一个待审批的视频生成任务。",
)
async def reject_video_task(_ctx: AgentCtx, task_id: str) -> str:
    """Reject a pending video generation task.

    Args:
        task_id: The task ID to reject.

    Returns:
        Text describing rejection result.

    Examples:
        reject_video_task(task_id="task_000001")
    """
    success = await _reject_task(task_id)
    if success:
        return f"已拒绝任务 {task_id}。"
    return f"拒绝任务 {task_id} 失败，请检查任务状态。"


# ---------------------------------------------------------------------------
# 查询 (TOOL)
# ---------------------------------------------------------------------------


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="get_video_by_task_id",
    description="按任务 ID 获取视频 URL。",
)
async def get_video_by_task_id(_ctx: AgentCtx, task_id: str) -> str:
    """Get the video URL for a completed video generation task.

    Args:
        task_id: The task ID returned by create_video.

    Returns:
        Video URL string, or error message if task not found or not completed.

    Examples:
        get_video_by_task_id(task_id="task_000001")
    """
    task = await get_video_task(task_id)
    if not task:
        return f"任务 {task_id} 不存在。"
    if not task.video_urls:
        return f"任务 {task_id} 暂无视频 URL。状态: {task.status.value}"
    return "\n".join(task.video_urls)


@plugin.mount_sandbox_method(
    SandboxMethodType.TOOL,
    name="list_video_tasks",
    description="分页列出所有视频生成任务。",
)
async def list_video_tasks(_ctx: AgentCtx, page: int = 1) -> str:
    """List all video generation tasks with pagination.

    Args:
        page: Page number, starting from 1. Default 1.

    Returns:
        Task list text with task IDs, prompts and statuses.

    Examples:
        list_video_tasks()
        list_video_tasks(page=2)
    """
    if page < 1:
        page = 1
    tasks_page, total_pages, total_tasks = await get_tasks_page(page)
    if not tasks_page:
        return "没有找到任何任务"
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
    description="查询指定视频任务的详细信息。",
)
async def get_video_task_info(_ctx: AgentCtx, task_id: str) -> str:
    """Get detailed info of a specific video task.

    Args:
        task_id: The task ID to query.

    Returns:
        Task detail text, or error message if not found.

    Examples:
        get_video_task_info(task_id="task_000001")
    """
    task = await get_video_task(task_id)
    if not task:
        return f"任务 {task_id} 不存在。"
    return format_task_info(task)


# ---------------------------------------------------------------------------
# 管理命令 (框架命令系统)
# ---------------------------------------------------------------------------


async def _resolve_single_pending(task_id: str, label: str) -> tuple[Optional[str], Optional[str]]:
    """命令参数解析：显式 task_id 或唯一 PENDING 任务自动选择。

    Returns: (task_id, error_message)
    """
    if task_id:
        gt = await _load_tasks()
        if not gt.get_task(task_id):
            return None, f"任务 {task_id} 不存在"
        return task_id, None
    gt = await _load_tasks()
    pending = [t for t in gt.get_all_tasks() if t.status == TaskStatus.PENDING]
    if not pending:
        return None, f"当前没有待{label}的任务"
    if len(pending) > 1:
        ids = ", ".join(t.task_id for t in pending)
        return None, f"有多个待{label}任务，请指定任务ID：{ids}"
    return pending[0].task_id, None


@plugin.mount_command(
    name="agnes_y",
    description="批准视频生成任务",
    aliases=["agnes-y", "agnes-yes"],
    permission=CommandPermission.SUPER_USER,
    usage="agnes_y [task_id]；留空时自动选择唯一的待审批任务",
    category="Agnes 视频",
    tags=["video", "agnes", "approval"],
)
async def cmd_approve(
    context: CommandExecutionContext,
    task_id: Annotated[str, Arg("要批准的任务ID，留空自动选择", positional=True)] = "",
) -> CommandResponse:
    tid, err = await _resolve_single_pending(task_id, "审批")
    if err:
        return CmdCtl.failed(err)
    if await _approve_task(tid):
        return CmdCtl.success(f"已批准任务 {tid}，开始执行视频生成")
    return CmdCtl.failed(f"批准任务 {tid} 失败，请检查任务状态")


@plugin.mount_command(
    name="agnes_n",
    description="拒绝视频生成任务",
    aliases=["agnes-no"],
    permission=CommandPermission.SUPER_USER,
    usage="agnes_n [task_id]；留空时自动选择唯一的待审批任务",
    category="Agnes 视频",
    tags=["video", "agnes", "approval"],
)
async def cmd_reject(
    context: CommandExecutionContext,
    task_id: Annotated[str, Arg("要拒绝的任务ID，留空自动选择", positional=True)] = "",
) -> CommandResponse:
    tid, err = await _resolve_single_pending(task_id, "审批")
    if err:
        return CmdCtl.failed(err)
    if await _reject_task(tid):
        return CmdCtl.success(f"已拒绝任务 {tid}")
    return CmdCtl.failed(f"拒绝任务 {tid} 失败，请检查任务状态")


@plugin.mount_command(
    name="agnes_list",
    description="分页查询视频任务列表",
    aliases=["agnes-ls"],
    permission=CommandPermission.SUPER_USER,
    usage="agnes_list [page]",
    category="Agnes 视频",
    tags=["video", "agnes"],
)
async def cmd_list(
    context: CommandExecutionContext,
    page: Annotated[int, Arg("页码", positional=True)] = 1,
) -> CommandResponse:
    if page < 1:
        page = 1
    tasks_page, total_pages, total_tasks = await get_tasks_page(page)
    if not tasks_page:
        return CmdCtl.failed("没有找到任何任务")
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
        info += f"使用 /agnes_list {page + 1} 查看下一页"
    return CmdCtl.success(info)


@plugin.mount_command(
    name="agnes_info",
    description="查询视频任务详情",
    aliases=["agnes-i"],
    permission=CommandPermission.SUPER_USER,
    usage="agnes_info <task_id>",
    category="Agnes 视频",
    tags=["video", "agnes"],
)
async def cmd_info(
    context: CommandExecutionContext,
    task_id: Annotated[str, Arg("要查询的任务ID", positional=True, greedy=True)],
) -> CommandResponse:
    task = await get_video_task(task_id)
    if not task:
        return CmdCtl.failed(f"任务 {task_id} 不存在")
    return CmdCtl.success(f"任务详情:\n\n{format_task_info(task)}")


@plugin.mount_command(
    name="agnes_help",
    description="显示 Agnes 插件使用帮助",
    aliases=["agnes-h"],
    permission=CommandPermission.SUPER_USER,
    usage="agnes_help",
    category="Agnes 视频",
    tags=["video", "agnes", "help"],
)
async def cmd_help(context: CommandExecutionContext) -> CommandResponse:
    approval_status = "开启" if config.REQUIRE_ADMIN_APPROVAL else "关闭"
    help_text = (
        f"🎬 Agnes AI 视频生成插件 v2.0.0\n\n"
        f"📋 管理员命令 (需要 SUPER_USERS 权限):\n"
        f"  /agnes_y [task_id] — 批准视频任务\n"
        f"  /agnes_n [task_id] — 拒绝视频任务\n"
        f"  /agnes_list [page] — 分页任务列表\n"
        f"  /agnes_info <task_id> — 任务详情\n"
        f"  /agnes_help — 显示此帮助\n\n"
        f"⚙️ 当前配置:\n"
        f"  审批流程: {approval_status}\n"
        f"  文本模型: {config.TEXT_MODEL}\n"
        f"  图片模型: {config.IMAGE_MODEL}\n"
        f"  视频模型: {config.VIDEO_MODEL}\n"
        f"  轮询间隔: {config.POLL_INTERVAL}s\n"
        f"  最大轮询: {config.MAX_POLL_ATTEMPTS}次\n\n"
        f"💡 Agent 调用 (对话中直接使用):\n"
        f"  create_video(prompt, mode, images/first_frame/..., seconds, size) — 创建视频\n"
        f"  get_video_by_task_id(task_id) — 获取视频 URL\n"
        f"  cancel_current_video_task() — 取消任务\n"
        f"  approve_video_task(task_id) / reject_video_task(task_id) — 审批\n"
        f"  list_video_tasks(page) — 任务列表\n"
        f"  get_video_task_info(task_id) — 任务详情"
    )
    return CmdCtl.success(help_text)


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件资源。"""
    logger.info("Agnes AI Generation 插件资源已清理。")
