"""数据模型定义 — 对齐 Agnes Video 2.5 系列接口"""

import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from .conf import config


class TaskStatus(str, Enum):
    """视频任务状态

    流转:
    创建 → PENDING (需审批) / QUEUED (不需审批)
    PENDING → APPROVED (审批通过) / REJECTED (审批拒绝/超时)
    APPROVED → PROCESSING (API 开始生成)
    QUEUED → PROCESSING → COMPLETED (生成完成) / FAILED (生成失败)
    """

    PENDING = "pending"       # 等待管理员审批
    APPROVED = "approved"     # 已批准，准备提交 API
    REJECTED = "rejected"     # 已拒绝（等同 CANCELED）
    QUEUED = "queued"         # 在队列中等待处理（API 初始状态）
    PROCESSING = "processing"  # 正在生成
    COMPLETED = "completed"   # 已完成
    FAILED = "failed"         # 失败

    @classmethod
    def from_api(cls, status: str) -> "TaskStatus":
        """从 API 返回的状态字符串转换（忽略 internal_status/internal_progress）"""
        mapping = {
            "queued": cls.QUEUED,
            "pending": cls.QUEUED,
            "in_progress": cls.PROCESSING,
            "processing": cls.PROCESSING,
            "completed": cls.COMPLETED,
            "succeeded": cls.COMPLETED,
            "failed": cls.FAILED,
        }
        return mapping.get((status or "").lower(), cls.PROCESSING)


# ---------------------------------------------------------------------------
# 视频任务
# ---------------------------------------------------------------------------


class VideoTask(BaseModel):
    """视频生成任务（agnes-video-2.5 / agnes-video-2.5-flash）"""

    task_id: str
    chat_key: str
    prompt: str
    reason: Optional[str] = None
    status: TaskStatus = TaskStatus.PENDING
    video_id: Optional[str] = None      # API 返回的 video_id，用于轮询
    video_urls: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    progress: int = 0
    create_time: int = 0
    update_time: int = 0
    model: str = ""
    mode: str = "text"                  # text | keyframe | reference
    seconds: str = "5"                  # "4"-"12"
    size: str = "720P"                  # 720P | 1080P | 1K | 2K（flash 仅 720P）
    aspect_ratio: str = "16:9"
    seed: Optional[int] = None
    first_frame: Optional[str] = None   # keyframe 模式首帧
    last_frame: Optional[str] = None    # keyframe 模式尾帧
    image_urls: Optional[List[str]] = None   # reference 模式参考图片
    audio_urls: Optional[List[str]] = None   # reference 模式参考音频
    video_refs: Optional[List[Dict[str, Any]]] = None  # reference 模式参考视频对象

    @field_validator("model", "mode", "seconds", "size", "aspect_ratio", mode="before")
    @classmethod
    def _null_to_default(cls, value: Any, info: ValidationInfo) -> Any:
        """1.1.0 落盘的任务记录里这些字段显式为 null，而 pydantic 不会给显式 None 套默认值。"""
        return cls.model_fields[info.field_name].default if value is None else value

    @classmethod
    def create(
        cls,
        task_id: str,
        chat_key: str,
        prompt: str,
        reason: Optional[str] = None,
        model: str = "",
        mode: str = "text",
        seconds: str = "5",
        size: str = "720P",
        aspect_ratio: str = "16:9",
        seed: Optional[int] = None,
        first_frame: Optional[str] = None,
        last_frame: Optional[str] = None,
        image_urls: Optional[List[str]] = None,
        audio_urls: Optional[List[str]] = None,
        video_refs: Optional[List[Dict[str, Any]]] = None,
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
            mode=mode,
            seconds=seconds,
            size=size,
            aspect_ratio=aspect_ratio,
            seed=seed,
            first_frame=first_frame,
            last_frame=last_frame,
            image_urls=image_urls,
            audio_urls=audio_urls,
            video_refs=video_refs,
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
