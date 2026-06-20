"""数据模型定义"""

import time
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .conf import config


class TaskStatus(str, Enum):
    """视频任务状态"""

    QUEUED = "queued"       # API 返回的初始状态
    PENDING = "pending"     # 等待管理员审批
    APPROVED = "approved"   # 已批准，准备执行
    REJECTED = "rejected"   # 已拒绝
    PROCESSING = "processing"  # 执行中
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"       # 失败
    CANCELED = "canceled"   # 已取消

    @classmethod
    def from_api(cls, status: str) -> "TaskStatus":
        """从 API 返回的状态字符串转换，未知状态映射为 PROCESSING。"""
        try:
            return cls(status.lower())
        except ValueError:
            return cls.PROCESSING


# ---------------------------------------------------------------------------
# 视频任务
# ---------------------------------------------------------------------------


class VideoTask(BaseModel):
    """视频生成任务"""

    task_id: str
    chat_key: str
    prompt: str
    reason: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    video_urls: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    create_time: int = 0
    update_time: int = 0
    model: str = ""
    height: int = 768
    width: int = 1152
    num_frames: int = 121
    frame_rate: float = 24
    image_url: Optional[str] = None
    image_urls: Optional[List[str]] = None
    mode: Optional[str] = None

    @classmethod
    def create(
        cls,
        task_id: str,
        chat_key: str,
        prompt: str,
        reason: Optional[str] = None,
        model: str = "",
        height: int = 768,
        width: int = 1152,
        num_frames: int = 121,
        frame_rate: float = 24,
        image_url: Optional[str] = None,
        image_urls: Optional[List[str]] = None,
        mode: Optional[str] = None,
    ) -> "VideoTask":
        """创建一个新的视频任务"""
        now = int(time.time())
        return cls(
            task_id=task_id,
            chat_key=chat_key,
            prompt=prompt,
            reason=reason,
            status=TaskStatus.PENDING,
            create_time=now,
            update_time=now,
            model=model,
            height=height,
            width=width,
            num_frames=num_frames,
            frame_rate=frame_rate,
            image_url=image_url,
            image_urls=image_urls,
            mode=mode,
        )


# ---------------------------------------------------------------------------
# 历史记录
# ---------------------------------------------------------------------------


class HistoryRecord(BaseModel):
    """视频生成历史记录"""

    prompt: str
    video_urls: List[str] = Field(default_factory=list)
    task_id: str = ""
    create_time: int = 0

    @classmethod
    def create(cls, prompt: str, video_urls: List[str], task_id: str = "") -> "HistoryRecord":
        return cls(
            prompt=prompt,
            video_urls=video_urls,
            task_id=task_id,
            create_time=int(time.time()),
        )


# ---------------------------------------------------------------------------
# 聊天会话数据
# ---------------------------------------------------------------------------


class ChatSessionData(BaseModel):
    """聊天会话数据"""

    current_task_id: Optional[str] = None
    history_records: List[HistoryRecord] = Field(default_factory=list)

    def add_history(self, prompt: str, video_urls: List[str], task_id: str = "") -> HistoryRecord:
        """添加历史记录"""
        record = HistoryRecord.create(prompt, video_urls, task_id)
        self.history_records.append(record)
        if len(self.history_records) > config.MAX_HISTORY:
            self.history_records = self.history_records[-config.MAX_HISTORY:]
        return record


# ---------------------------------------------------------------------------
# 全局任务管理
# ---------------------------------------------------------------------------


class GlobalTaskData(BaseModel):
    """全局任务管理数据"""

    tasks: Dict[str, VideoTask] = Field(default_factory=dict)
    task_counter: int = 0

    def add_task(self, task: VideoTask) -> None:
        """添加任务"""
        self.tasks[task.task_id] = task

    def get_task(self, task_id: str) -> Optional[VideoTask]:
        """获取任务"""
        return self.tasks.get(task_id)

    def update_task(self, task_id: str, **kwargs) -> bool:
        """更新任务字段"""
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        task.update_time = int(time.time())
        for key, value in kwargs.items():
            if hasattr(task, key):
                setattr(task, key, value)
        return True

    def get_next_task_id(self) -> str:
        """获取下一个任务 ID"""
        self.task_counter += 1
        return f"task_{self.task_counter:06d}"

    def get_all_tasks(self) -> List[VideoTask]:
        """获取所有任务"""
        return list(self.tasks.values())

    def get_tasks_page(self, page: int, items_per_page: int) -> List[VideoTask]:
        """获取指定页的任务"""
        all_tasks = sorted(self.tasks.values(), key=lambda x: x.create_time, reverse=True)
        start = (page - 1) * items_per_page
        end = start + items_per_page
        return all_tasks[start:end]

    def get_total_pages(self, items_per_page: int) -> int:
        """获取总页数"""
        total = len(self.tasks)
        return (total + items_per_page - 1) // items_per_page
