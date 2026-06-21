"""Agnes AI 生成插件

提供文本、图片、视频生成功能，封装 Agnes 官方 API。
支持文生图、图生图、文生视频、图生视频、多图视频、关键帧动画。
"""

from .conf import plugin
from .service import (  # noqa: F401
    approve_video_task,
    reject_video_task,
    cancel_current_video_task,
    get_video_task,
)
from .handlers_text import *  # noqa: F403
from .handlers_image import *  # noqa: F403
from .handlers_video import *  # noqa: F403

__all__ = [
    "plugin",
    "generate_text",
    "generate_image",
    "create_video",
    "get_video",
    "cancel_current_video_task",
    "approve_video_task",
    "reject_video_task",
    "get_video_by_task_id",
    "list_video_tasks",
    "get_video_task_info",
    "create_video_task",
    "process_video_task",
    "update_task_status",
    "format_task_info",
    "get_tasks_page",
    "extract_video_urls",
    "validate_video_args",
    "prepare_generation_prompt",
]
