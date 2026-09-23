"""任务状态机与异步任务协程的行为测试（全部离线，不触网）"""

from types import SimpleNamespace

import pytest
from nekro_agent.services.plugin.task import TaskSignal

from agnes_ai_generation import service
from agnes_ai_generation.models import ChatSessionData, GlobalTaskData, TaskStatus, VideoTask


class FakeStore:
    def __init__(self):
        self.data = {}

    async def get(self, chat_key="", store_key="", **kw):
        return self.data.get((chat_key, store_key))

    async def set(self, chat_key="", store_key="", value="", **kw):
        self.data[(chat_key, store_key)] = value


class FakeHandle:
    def __init__(self, notify_result=True):
        self.notified = []
        self.notify_result = notify_result

    def notify(self, key, data=None):
        self.notified.append((key, data))
        return self.notify_result


class FakeTaskAPI:
    def __init__(self):
        self.started = []
        self.handles = {}
        self.cancelled = []
        self.running = set()

    def get_handle(self, task_type, task_id):
        return self.handles.get(task_id)

    def is_running(self, task_type, task_id):
        return task_id in self.running

    async def start(self, task_type, task_id, chat_key, plugin, *args, **kwargs):
        self.started.append(task_id)

    async def cancel(self, task_type, task_id):
        self.cancelled.append(task_id)
        return True


class GenHandle:
    def __init__(self, wait_result=None):
        self.agent_msgs = []
        self.is_cancelled = False
        self._wait_result = wait_result

    async def wait(self, key, timeout=None):
        return self._wait_result

    async def notify_agent(self, message, trigger=True):
        self.agent_msgs.append(message)


CHAT = "platform-user_10001"


@pytest.fixture
def env(monkeypatch):
    """隔离的 service 环境：内存 store + 假 task API + 默认配置"""
    fake_store = FakeStore()
    fake_api = FakeTaskAPI()
    monkeypatch.setattr(service, "store", fake_store)
    monkeypatch.setattr(service, "task_api", fake_api)
    monkeypatch.setattr(service.config, "REQUIRE_ADMIN_APPROVAL", False)
    monkeypatch.setattr(service.config, "POLL_INTERVAL", 0)
    monkeypatch.setattr(service.config, "MAX_POLL_ATTEMPTS", 10)
    pushed = []

    async def fake_push(chat_key="", message="", **kw):
        pushed.append((chat_key, message))

    monkeypatch.setattr(service, "push_system", fake_push)
    return SimpleNamespace(store=fake_store, api=fake_api, pushed=pushed)


def _ctx():
    return SimpleNamespace(from_chat_key=CHAT, chat_key=CHAT)


async def _seed_task(env, task_id="task_000001", status=TaskStatus.QUEUED, **kw) -> VideoTask:
    gt = GlobalTaskData()
    task = VideoTask.create(task_id=task_id, chat_key=CHAT, prompt="a cat", model="agnes-video-2.5-flash", **kw)
    task.status = status
    gt.add_task(task)
    gt.task_counter = int(task_id.split("_")[1])
    await service._save_tasks(gt)
    chat = ChatSessionData(current_task_id=task_id)
    await service._save_chat(CHAT, chat)
    return task


async def _collect(gen):
    out = []
    async for ctl in gen:
        out.append(ctl)
    return out


# ---------------------------------------------------------------------------
# 创建 / 审批 / 拒绝 / 取消
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_queued_starts_runner(env):
    task = await service.create_video_task("a cat on the beach", _ctx())
    gt = await service._load_tasks()
    stored = gt.get_task(task.task_id)
    assert stored.status == TaskStatus.QUEUED
    assert env.api.started == [task.task_id]
    chat = await service._load_chat(CHAT)
    assert chat.current_task_id == task.task_id


@pytest.mark.asyncio
async def test_create_task_pending_when_approval_required(env, monkeypatch):
    monkeypatch.setattr(service.config, "REQUIRE_ADMIN_APPROVAL", True)
    task = await service.create_video_task("cat", _ctx())
    assert task.status == TaskStatus.PENDING
    assert env.api.started == [task.task_id]  # 协程仍启动，进入审批等待


@pytest.mark.asyncio
async def test_create_task_rejects_bad_params(env):
    with pytest.raises(ValueError):
        await service.create_video_task("cat", _ctx(), mode="reference", seconds="99")


@pytest.mark.asyncio
async def test_approve_wakes_waiting_handle(env):
    await _seed_task(env, status=TaskStatus.PENDING)
    handle = FakeHandle()
    env.api.handles["task_000001"] = handle
    assert await service.approve_video_task("task_000001")
    gt = await service._load_tasks()
    assert gt.get_task("task_000001").status == TaskStatus.APPROVED
    assert handle.notified == [("approval", True)]
    assert env.api.started == []  # 有活协程则不重复启动


@pytest.mark.asyncio
async def test_approve_restarts_after_restart(env):
    await _seed_task(env, status=TaskStatus.PENDING)
    assert await service.approve_video_task("task_000001")
    assert env.api.started == ["task_000001"]


@pytest.mark.asyncio
async def test_reject_marks_rejected_and_wakes(env):
    await _seed_task(env, status=TaskStatus.PENDING)
    handle = FakeHandle()
    env.api.handles["task_000001"] = handle
    assert await service.reject_video_task("task_000001")
    gt = await service._load_tasks()
    assert gt.get_task("task_000001").status == TaskStatus.REJECTED
    assert handle.notified == [("approval", False)]


@pytest.mark.asyncio
async def test_reject_processing_task_fails(env):
    await _seed_task(env, status=TaskStatus.PROCESSING)
    assert not await service.reject_video_task("task_000001")


@pytest.mark.asyncio
async def test_cancel_current_clears_chat_and_cancels(env):
    await _seed_task(env, status=TaskStatus.PROCESSING)
    env.api.handles["task_000001"] = FakeHandle()
    task = await service.cancel_current_video_task(CHAT)
    assert task.task_id == "task_000001"
    gt = await service._load_tasks()
    assert gt.get_task("task_000001").status == TaskStatus.REJECTED
    assert env.api.cancelled == ["task_000001"]
    chat = await service._load_chat(CHAT)
    assert chat.current_task_id is None


@pytest.mark.asyncio
async def test_terminal_state_not_overwritten(env):
    await _seed_task(env, status=TaskStatus.COMPLETED)
    await service.update_task_status("task_000001", TaskStatus.PROCESSING)
    gt = await service._load_tasks()
    assert gt.get_task("task_000001").status == TaskStatus.COMPLETED


# ---------------------------------------------------------------------------
# 异步任务协程
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generator_text_happy_path(env, monkeypatch):
    calls = []

    async def fake_req(client, method, path, payload=None):
        calls.append((method, path, payload))
        if method == "POST":
            return {"video_id": "vid_1", "status": "queued"}
        if len(calls) == 2:
            return {"status": "in_progress", "progress": 0.3}
        return {"status": "completed", "url": "https://cdn/agnes/v.mp4"}

    monkeypatch.setattr(service, "_req", fake_req)
    await _seed_task(env)
    handle = GenHandle()
    ctls = await _collect(service.agnes_video_task(handle, "task_000001"))

    assert ctls[-1].signal == TaskSignal.SUCCESS
    gt = await service._load_tasks()
    task = gt.get_task("task_000001")
    assert task.status == TaskStatus.COMPLETED
    assert task.video_urls == ["https://cdn/agnes/v.mp4"]
    assert task.video_id == "vid_1"
    # 提交 payload 符合 2.5 形态
    post = [c for c in calls if c[0] == "POST"][0]
    assert post[2]["mode"] == "text"
    assert post[2]["seconds"] == "5"
    # 轮询 URL 带 model_name
    get = [c for c in calls if c[0] == "GET"][0]
    assert "video_id=vid_1" in get[1] and "model_name=agnes-video-2.5-flash" in get[1]
    # 完成通知 + 历史
    assert any("视频生成完成" in m for m in handle.agent_msgs)
    chat = await service._load_chat(CHAT)
    assert len(chat.history_records) == 1
    assert chat.current_task_id is None


@pytest.mark.asyncio
async def test_generator_create_failure_marks_failed(env, monkeypatch):
    async def boom(client, method, path, payload=None):
        raise RuntimeError("HTTP 400 bad mode")

    monkeypatch.setattr(service, "_req", boom)
    await _seed_task(env)
    handle = GenHandle()
    ctls = await _collect(service.agnes_video_task(handle, "task_000001"))
    assert ctls[-1].signal == TaskSignal.FAIL
    gt = await service._load_tasks()
    assert gt.get_task("task_000001").status == TaskStatus.FAILED
    assert any("视频生成失败" in m for m in handle.agent_msgs)


@pytest.mark.asyncio
async def test_generator_api_reports_failed_status(env, monkeypatch):
    async def fake_req(client, method, path, payload=None):
        if method == "POST":
            return {"video_id": "vid_1", "status": "queued"}
        return {"status": "failed", "error": {"message": "nsfw detected"}}

    monkeypatch.setattr(service, "_req", fake_req)
    await _seed_task(env)
    handle = GenHandle()
    ctls = await _collect(service.agnes_video_task(handle, "task_000001"))
    assert ctls[-1].signal == TaskSignal.FAIL
    gt = await service._load_tasks()
    assert "nsfw detected" in gt.get_task("task_000001").error_message


@pytest.mark.asyncio
async def test_generator_timeout(env, monkeypatch):
    async def always_running(client, method, path, payload=None):
        if method == "POST":
            return {"video_id": "vid_1", "status": "queued"}
        return {"status": "in_progress", "progress": 10}

    monkeypatch.setattr(service, "_req", always_running)
    monkeypatch.setattr(service.config, "MAX_POLL_ATTEMPTS", 2)
    await _seed_task(env)
    handle = GenHandle()
    ctls = await _collect(service.agnes_video_task(handle, "task_000001"))
    assert ctls[-1].signal == TaskSignal.FAIL
    gt = await service._load_tasks()
    assert gt.get_task("task_000001").status == TaskStatus.FAILED
    assert "超时" in gt.get_task("task_000001").error_message


@pytest.mark.asyncio
async def test_generator_approval_flow_wakes_and_completes(env, monkeypatch):
    async def fake_req(client, method, path, payload=None):
        if method == "POST":
            return {"video_id": "vid_2", "status": "queued"}
        return {"status": "completed", "url": "https://cdn/agnes/x.mp4"}

    monkeypatch.setattr(service, "_req", fake_req)
    await _seed_task(env, status=TaskStatus.PENDING)

    handle = GenHandle(wait_result=True)
    ctls = await _collect(service.agnes_video_task(handle, "task_000001"))
    assert ctls[0].signal == TaskSignal.PROGRESS  # 等待管理员审批
    assert env.pushed and "视频生成申请" in env.pushed[0][1]
    assert ctls[-1].signal == TaskSignal.SUCCESS
    gt = await service._load_tasks()
    assert gt.get_task("task_000001").status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_generator_approval_rejected(env):
    await _seed_task(env, status=TaskStatus.PENDING)
    handle = GenHandle(wait_result=False)
    ctls = await _collect(service.agnes_video_task(handle, "task_000001"))
    assert ctls[-1].signal == TaskSignal.FAIL
    gt = await service._load_tasks()
    assert gt.get_task("task_000001").status == TaskStatus.REJECTED


@pytest.mark.asyncio
async def test_generator_skips_terminal_task(env, monkeypatch):
    """恢复时任务已是终态：直接 cancel 信号退出，不触碰 API。"""
    called = []

    async def spy_req(client, method, path, payload=None):
        called.append(method)
        return {}

    monkeypatch.setattr(service, "_req", spy_req)
    await _seed_task(env, status=TaskStatus.REJECTED)
    handle = GenHandle()
    ctls = await _collect(service.agnes_video_task(handle, "task_000001"))
    assert ctls[-1].signal == TaskSignal.CANCEL
    assert called == []


@pytest.mark.asyncio
async def test_generator_recovers_existing_video_id(env, monkeypatch):
    """重启恢复：任务已有 video_id 时跳过提交直接轮询。"""
    calls = []

    async def fake_req(client, method, path, payload=None):
        calls.append((method, path))
        return {"status": "completed", "url": "https://cdn/agnes/r.mp4"}

    monkeypatch.setattr(service, "_req", fake_req)
    task = await _seed_task(env, status=TaskStatus.PROCESSING)
    gt = await service._load_tasks()
    gt.update_task("task_000001", video_id="vid_resume")
    await service._save_tasks(gt)
    handle = GenHandle()
    ctls = await _collect(service.agnes_video_task(handle, "task_000001"))
    assert ctls[-1].signal == TaskSignal.SUCCESS
    assert not [c for c in calls if c[0] == "POST"]  # 未重复提交


@pytest.mark.asyncio
async def test_recover_unfinished_tasks(env):
    gt = GlobalTaskData()
    t1 = VideoTask.create(task_id="task_000001", chat_key=CHAT, prompt="a", model="agnes-video-2.5-flash")
    t1.status = TaskStatus.PROCESSING
    t2 = VideoTask.create(task_id="task_000002", chat_key=CHAT, prompt="b", model="agnes-video-2.5-flash")
    t2.status = TaskStatus.COMPLETED
    gt.add_task(t1)
    gt.add_task(t2)
    await service._save_tasks(gt)

    env.api.running = {"task_000001"}
    n = await service.recover_unfinished_tasks()
    assert n == 0  # 正在跑的不动
    env.api.running = set()
    n = await service.recover_unfinished_tasks()
    assert n == 1  # 只恢复 PROCESSING，COMPLETED 跳过
    assert env.api.started == ["task_000001"]


@pytest.mark.asyncio
async def test_load_tasks_tolerates_v1_null_fields(env):
    """1.1.0 落盘的任务记录把 model/mode 等字段写成了 null，加载不能整体失败。"""
    legacy = (
        '{"tasks":{"task_000001":{"task_id":"task_000001","chat_key":"c1","prompt":"a",'
        '"reason":null,"status":"completed","video_id":"v1","video_urls":["u"],'
        '"error_message":null,"create_time":1,"update_time":2,"model":null,"mode":null,'
        '"seconds":null,"size":null,"aspect_ratio":null}},"task_counter":1}'
    )
    env.store.data[("global", service._STORE_TASKS)] = legacy
    gt = await service._load_tasks()
    task = gt.get_task("task_000001")
    assert task.mode == "text"
    assert task.seconds == "5"
    assert task.size == "720P"
    assert task.aspect_ratio == "16:9"
    assert task.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_recover_marks_pre_migration_tasks_failed(env):
    """1.x 遗留任务没有 video_id 且其模型形态已不受支持，恢复阶段不能重新提交。"""
    gt = GlobalTaskData()
    old = VideoTask.create(task_id="task_000001", chat_key=CHAT, prompt="a", model="agnes-video-v2.0")
    old.status = TaskStatus.PROCESSING
    new = VideoTask.create(task_id="task_000002", chat_key=CHAT, prompt="b", model="agnes-video-2.5-flash")
    new.status = TaskStatus.PENDING
    gt.add_task(old)
    gt.add_task(new)
    await service._save_tasks(gt)

    n = await service.recover_unfinished_tasks()
    assert n == 1
    assert env.api.started == ["task_000002"]

    gt2 = await service._load_tasks()
    assert gt2.get_task("task_000001").status is TaskStatus.FAILED
    assert "2.5" in (gt2.get_task("task_000001").error_message or "")
