"""视频生成工具 — 对齐 tongyi_wanx 架构

方法类型:
- BEHAVIOR: 创建/取消/审批/拒绝（Agent 触发，返回描述性文本，不触发再次调用）
- TOOL: 查询/列表/详情（返回结构化结果，Agent 可继续处理）
- on_command: 管理员命令（/agnes_y, /agnes_n, /agnes_list, /agnes_info）

状态流转:
  创建 → PENDING (需审批) / QUEUED (不需审批)
  PENDING → APPROVED (审批通过) / REJECTED (审批拒绝)
  APPROVED/QUEUED → PROCESSING (API 生成中) → COMPLETED / FAILED
"""

import time
from typing import List, Optional

from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.services.plugin.base import SandboxMethodType
from nekro_agent.tools.common_util import limited_text_output
from nonebot import on_command
from nonebot.adapters import Bot, Message
from nonebot.adapters.onebot.v11 import MessageEvent, PrivateMessageEvent, GroupMessageEvent
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

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
# 权限 + 工具
# ---------------------------------------------------------------------------


def _get_super_users() -> set[str]:
    """获取 SUPER_USERS 配置"""
    from nekro_agent.core.config import config as global_config
    return {str(uid) for uid in getattr(global_config, "SUPER_USERS", [])}


def _get_user_id(event: MessageEvent) -> str:
    """从 nonebot2 事件获取 user_id"""
    return str(event.user_id)


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

    from .service import _load_chat, _load_tasks

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
                f"- Model: {task.model}\n"
                f"- Size: {task.width}x{task.height}\n"
                f"- Frames: {task.num_frames} @ {task.frame_rate}fps\n"
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

    # 插件使用说明（Agent 通过这段文字学习如何调用）
    background_info = (
        f"[Agnes Video Generation Plugin]\n"
        f"Model: {model} | Approval: {'required' if require_approval else 'not required'} | State: {'idle' if is_idle else 'busy'}\n\n"
        f"## Available Functions:\n"
        f"1. create_video(prompt, image_urls=[...], mode=..., reason=...) -> str\n"
        f"   Create a video generation task. Returns task_id and status.\n"
        f"   - prompt: Video description (English works best, Chinese auto-translated)\n"
        f"   - image_urls: Single ['url'] or multiple ['url1','url2'] for image-to-video\n"
        f"   - mode: 'ti2vid' or 'keyframes' (optional)\n"
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
    description="创建视频任务。支持文生视频、图生视频、多图视频和关键帧动画。",
)
async def create_video(
    _ctx: AgentCtx,
    prompt: str,
    reason: str = "",
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
    """Create a video generation task.

    Creates a video generation task with Agnes AI. The task will be queued for processing.
    If admin approval is required, the task will wait for approval before starting.
    If not, the task will start immediately.

    Args:
        prompt: Video description. Include subject, action, scene, style, camera, lighting.
            English works best; Chinese will be auto-translated.
            Example: "A cinematic shot of a cat walking on the beach at sunset"
        reason: Why the user wants this video. Shown during approval. Optional.
        image_urls: Input images for image-to-video. Single: ['url']. Multiple: ['url1','url2'].
            Supports HTTP(S) URL or Data URI (base64). Optional (omit for text-to-video).
            Data URI example: ["data:image/png;base64,iVBORw0KGgo..."]
        mode: Generation mode. 'ti2vid' (image-to-video) or 'keyframes' (keyframe animation).
            Optional.
        height: Video height. Default 768.
        width: Video width. Default 1152.
        num_frames: Frame count, must satisfy 8n+1 and <= 441. Default 121. Use 81 for quick test.
        frame_rate: Frame rate 1-60. Default 24.
        num_inference_steps: Inference steps. Optional (model default if not set).
        seed: Random seed for reproducibility. Optional.
        negative_prompt: Negative prompt. Optional.
        translate_prompt: Auto-translate non-English prompts. Default True.

    Returns:
        Text describing task creation result, including task_id and status.
        On failure, returns error message.

    Examples:
        Text-to-video:
        create_video(prompt="A cat walking on the beach at sunset")

        Image-to-video:
        create_video(prompt="Animate subtle camera movement",
                     image_urls=["https://example.com/image.png"])

        Keyframe animation:
        create_video(prompt="Smooth transition between keyframes",
                     image_urls=["https://a.png", "https://b.png"],
                     mode="keyframes")

        With approval reason:
        create_video(prompt="Funny cat video", reason="User wants a birthday gift")
    """
    if not _ctx.chat_key:
        return "无法创建视频任务：未获取到聊天会话信息。"

    # 检查进行中任务
    from .service import _load_chat, _load_tasks
    chat_data = await _load_chat(_ctx.chat_key)
    if chat_data.current_task_id:
        gt = await _load_tasks()
        existing = gt.get_task(chat_data.current_task_id)
        if existing and existing.status in (TaskStatus.QUEUED, TaskStatus.PENDING, TaskStatus.APPROVED, TaskStatus.PROCESSING):
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
            neg=negative_prompt, imgs=image_urls, mode=mode,
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
# 按 task_id 获取视频 URL (TOOL)
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
        get_video_by_task_id(task_id="task_123456")
    """
    task = await get_video_task(task_id)
    if not task:
        return f"任务 {task_id} 不存在。"
    if not task.video_urls:
        return f"任务 {task_id} 暂无视频 URL。状态: {task.status.value}"
    return "\n".join(task.video_urls)


# ---------------------------------------------------------------------------
# 管理命令 (on_command)
# ---------------------------------------------------------------------------


@on_command("agnes_y", aliases={"agnes-y"}, priority=5, block=True).handle()
async def handle_approve(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    """批准视频生成任务"""
    user_id = _get_user_id(event)
    if user_id not in _get_super_users():
        await matcher.finish()
        return

    cmd_content = str(arg).strip() if arg else ""
    from .service import _load_tasks
    gt = await _load_tasks()
    pending = [t for t in gt.get_all_tasks() if t.status == TaskStatus.PENDING]

    if not cmd_content:
        if not pending:
            await matcher.finish(message="当前没有待审批的任务")
            return
        if len(pending) == 1:
            task_id = pending[0].task_id
        else:
            ids = ", ".join(t.task_id for t in pending)
            await matcher.finish(message=f"有多个待审批任务，请指定任务ID：{ids}")
            return
    else:
        task_id = cmd_content

    task = gt.get_task(task_id)
    if not task:
        await matcher.finish(message=f"任务 {task_id} 不存在")
        return

    success = await approve_video_task(task_id)
    if success:
        await matcher.finish(message=f"已批准任务 {task_id}，开始执行视频生成")
    else:
        await matcher.finish(message=f"批准任务 {task_id} 失败，请检查任务状态")


@on_command("agnes_n", aliases={"agnes-n"}, priority=5, block=True).handle()
async def handle_reject(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    """拒绝视频生成任务"""
    user_id = _get_user_id(event)
    if user_id not in _get_super_users():
        await matcher.finish()
        return

    cmd_content = str(arg).strip() if arg else ""
    from .service import _load_tasks
    gt = await _load_tasks()
    pending = [t for t in gt.get_all_tasks() if t.status == TaskStatus.PENDING]

    if not cmd_content:
        if not pending:
            await matcher.finish(message="当前没有待审批的任务")
            return
        if len(pending) == 1:
            task_id = pending[0].task_id
        else:
            ids = ", ".join(t.task_id for t in pending)
            await matcher.finish(message=f"有多个待审批任务，请指定任务ID：{ids}")
            return
    else:
        task_id = cmd_content

    task = gt.get_task(task_id)
    if not task:
        await matcher.finish(message=f"任务 {task_id} 不存在")
        return

    success = await reject_video_task(task_id)
    if success:
        await matcher.finish(message=f"已拒绝任务 {task_id}")
    else:
        await matcher.finish(message=f"拒绝任务 {task_id} 失败，请检查任务状态")


@on_command("agnes_list", aliases={"agnes-list", "agnes-ls", "agnes_ls"}, priority=5, block=True).handle()
async def handle_list(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    """查询视频任务列表"""
    user_id = _get_user_id(event)
    if user_id not in _get_super_users():
        await matcher.finish()
        return

    try:
        page = 1
        if arg:
            page = int(str(arg).strip())
            if page < 1:
                page = 1
    except ValueError:
        await matcher.finish(message="页码必须是一个正整数")
        return

    tasks_page, total_pages, total_tasks = await get_tasks_page(page)
    if not tasks_page:
        await matcher.finish(message="没有找到任何任务")
        return

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
        info += f"使用 /agnes-list {page + 1} 查看下一页"

    await matcher.finish(message=info)


@on_command("agnes_info", aliases={"agnes-info", "agnes-i", "agnes_i"}, priority=5, block=True).handle()
async def handle_info(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    """查询任务详情"""
    user_id = _get_user_id(event)
    if user_id not in _get_super_users():
        await matcher.finish()
        return

    cmd_content = str(arg).strip() if arg else ""
    if not cmd_content:
        await matcher.finish(message="请指定要查询的任务ID")
        return

    task = await get_video_task(cmd_content)
    if not task:
        await matcher.finish(message=f"任务 {cmd_content} 不存在")
        return

    await matcher.finish(message=f"任务详情:\n\n{format_task_info(task)}")


@on_command("agnes_help", aliases={"agnes-h", "agnes_h"}, priority=5, block=True).handle()
async def handle_help(matcher: Matcher, event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    """显示插件使用帮助"""
    user_id = _get_user_id(event)
    if user_id not in _get_super_users():
        await matcher.finish()
        return

    approval_status = "开启" if config.REQUIRE_ADMIN_APPROVAL else "关闭"
    help_text = (
        f"🎬 Agnes AI 视频生成插件 v1.1.0\n\n"
        f"📋 管理员命令 (需要 SUPER_USERS 权限):\n"
        f"  /agnes_y [task_id] — 批准视频任务\n"
        f"  /agnes_n [task_id] — 拒绝视频任务\n"
        f"  /agnes_list [page] — 分页任务列表\n"
        f"  /agnes_info <task_id> — 任务详情\n"
        f"  /agnes_help — 显示此帮助\n\n"
        f"⚙️ 当前配置:\n"
        f"  审批流程: {approval_status}\n"
        f"  视频模型: {config.VIDEO_MODEL}\n"
        f"  轮询间隔: {config.POLL_INTERVAL}s\n"
        f"  最大轮询: {config.MAX_POLL_ATTEMPTS}次\n\n"
        f"💡 Agent 调用 (对话中直接使用):\n"
        f"  create_video(prompt, ...) — 创建视频\n"
        f"  get_video_by_task_id(task_id) — 获取视频 URL\n"
        f"  cancel_current_video_task() — 取消任务\n"
        f"  approve_video_task(task_id) — 批准任务\n"
        f"  reject_video_task(task_id) — 拒绝任务\n"
        f"  list_video_tasks(page) — 任务列表\n"
        f"  get_video_task_info(task_id) — 任务详情\n\n"
        f"⚠️ 如果命令无响应，请将 QQ 号添加到 nekro-agent 配置的 SUPER_USERS 列表中。"
    )
    await matcher.finish(message=help_text)


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------


@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件资源。"""
    logger.info("Agnes AI Generation 插件资源已清理。")
