"""Agnes AI 生成插件

提供文本、图片、视频生成功能，封装 Agnes 官方 API（2.5 系列）。
支持文生图、图生图、多图合成、文生视频、首尾帧关键帧动画、多模态参考视频。
"""

from .conf import plugin
from .handlers_text import *  # noqa: F403
from .handlers_image import *  # noqa: F403
from .handlers_video import *  # noqa: F403

__all__ = [
    "plugin",
    "generate_text",
    "generate_image",
    "create_video",
    "cancel_current_video_task",
    "approve_video_task",
    "reject_video_task",
    "get_video_by_task_id",
    "list_video_tasks",
    "get_video_task_info",
    "create_video_task",
    "recover_unfinished_tasks",
    "build_video_payload",
    "derive_mode",
    "validate_video_args",
    "prepare_generation_prompt",
]
