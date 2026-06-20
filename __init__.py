"""Agnes AI 生成插件

提供文本、图片、视频生成功能，封装 Agnes 官方 API。
支持文生图、图生图、文生视频、图生视频、多图视频、关键帧动画。
"""

from .conf import plugin
from .handlers import *  # noqa: F403

__all__ = ["plugin"]
