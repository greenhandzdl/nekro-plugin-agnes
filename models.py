"""数据模型定义"""

import time
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .conf import config


class TaskStatus(str, Enum):
    """视频任务状态"""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def from_api(cls, status: str) -> "TaskStatus":
        """从 API 返回的状态字符串转换，未知状态映射为 QUEUED。"""
        try:
            return cls(status.lower())
        except ValueError:
            return cls.QUEUED


class VideoTask(BaseModel):
    """视频生成任务"""

    task_id: str
    chat_key: str
    prompt: str
    translated_prompt: Optional[str] = None
    status: TaskStatus
    video_urls: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None
    create_time: int
    update_time: int
    model: str
    height: int
    width: int
    num_frames: int
    frame_rate: float
    image_url: Optional[str] = None
    image_urls: Optional[List[str]] = None
    mode: Optional[str] = None

    @classmethod
    def create(
        cls,
        task_id: str,
        chat_key: str,
        prompt: str,
        translated_prompt: Optional[str],
        model: str,
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
            translated_prompt=translated_prompt,
            status=TaskStatus.QUEUED,
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


class TaskStoreData(BaseModel):
    """全局任务存储"""

    tasks: Dict[str, VideoTask] = Field(default_factory=dict)
    task_counter: int = 0

    def get_next_task_id(self) -> str:
        """获取下一个任务 ID"""
        self.task_counter += 1
        return f"task_{self.task_counter:06d}"

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
