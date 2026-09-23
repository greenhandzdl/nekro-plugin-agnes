# Agnes AI Generation Plugin

> NekroAgent 插件：让 Agent 用 Agnes AI 生成文本、图片和视频。

**官网**: [https://agnes-ai.com/](https://agnes-ai.com/)

## 能做什么

- **文本生成** — `agnes-2.5-flash`（限时免费），支持流式输出与"文本 + 图片"多模态输入
- **图片生成与编辑** — `agnes-image-2.5-flash`（当前免费），文生图、图生图、多图合成，档位制尺寸 `1K/2K/3K/4K`
- **视频生成** — Agnes Video 2.5 系列异步任务，三种模式：纯文本、首尾帧关键帧、多模态参考（图/音/视频；`agnes-video-2.5-flash` 限时免费仅 720P，视频参考需付费档）
- **后台任务化** — 视频由框架异步任务系统轮询，完成或失败自动播报回原会话；实例重启后未完成任务自动恢复
- **配额闸门** — 可选管理员审批（`/agnes_y`、`/agnes_n`），避免 Agent 随手消耗额度
- **自动翻译** — 非英文提示词先译成英文再生成，视频侧更稳定

管理命令走 NekroAgent 命令系统（自动权限与帮助），任务走框架异步任务系统，不依赖 nonebot。

## 快速开始

```bash
pip install -e .                       # 或 pip install git+https://github.com/greenhandzdl/nekro-plugin-agnes.git
export AGNES_API_KEY="your_api_key"    # 也支持 AGNES_API_TOKEN / APIHUB_AGNES_API_KEY，或直接写进插件配置的 API_KEY
```

再把插件放进 NekroAgent 的 packages 路径并加载。**改过插件代码必须重启进程**——框架用 `import_module` 取模块，已加载的插件会命中 `sys.modules` 缓存，热重载看不到新文件（Docker 部署即 `docker restart <container>`）。

## 版本要求

| 项 | 要求 | 实测环境 |
|----|------|----------|
| NekroAgent | >= 2.4.0 | 2.4.0 |
| Python | >= 3.11, < 3.13（跟随框架约束） | 3.11.13 |

决定最低版本的是这几项框架能力：`plugin.mount_command` + `nekro_agent.services.command`、`plugin.mount_async_task` + `nekro_agent.services.plugin.task`、`plugin.store`（走框架 ORM，只能在 app 进程里用，独立脚本驱动不了任务表）。`mount_command` 在框架 2.3.0 就存在，但本插件只在 2.4.0 上验证过。

## Agent 拿到的工具

| 工具 | 类型 | 功能 | 关键参数 |
|------|------|------|----------|
| `generate_text` | AGENT | 文本生成，可带图片输入、可流式 | `prompt`, `images`, `system`, `temperature`, `stream` |
| `generate_image` | TOOL | 文生图 / 图生图 / 多图合成 | `prompt`, `size`(1K-4K), `ratio`, `image_urls`, `translate_prompt`, `send_to_chat` |
| `create_video` | BEHAVIOR | 创建视频任务（2.5 系列） | `prompt`, `mode`, `first_frame`/`last_frame`, `images`/`audios`/`videos`, `seconds`, `size`, `aspect_ratio`, `seed`, `reason` |
| `get_video_by_task_id` | TOOL | 取已完成任务的视频 URL | `task_id` |
| `cancel_current_video_task` | BEHAVIOR | 取消当前会话的视频任务 | 无 |
| `approve_video_task` / `reject_video_task` | BEHAVIOR | 批准 / 拒绝待审批任务 | `task_id` |
| `list_video_tasks` / `get_video_task_info` | TOOL | 分页任务列表 / 任务详情 | `page` / `task_id` |

### 视频模式与参数

| mode | 用途 | 必需媒体 | 不允许 |
|------|------|----------|--------|
| `text` | 纯文本生成视频 | 无 | 任何媒体字段 |
| `keyframe` | 首/尾帧控制构图 | `first_frame` 与 `last_frame` 至少一个 | `images/audios/videos` |
| `reference` | 素材作内容/风格/动作参考 | `images/audios/videos` 至少一类 | `first_frame/last_frame` |

- `seconds`：`"4"`–`"12"`；`size`：`720P/1080P/1K/2K`（flash 模型仅 `720P`）
- `aspect_ratio`：`16:9`（默认）`9:16` `1:1` `4:3` `3:4` `21:9`；视频不支持 `2:3`/`3:2`，只有图片支持
- 参考媒体上限：图片 ≤8（flash ≤5）、音频 ≤3、视频 ≤1（flash 不支持）
- 状态流转：`PENDING`（待审批）或 `QUEUED` → `APPROVED` → `PROCESSING` → `COMPLETED` / `FAILED` / `REJECTED`

### 管理员命令

需 SUPER_USERS 权限（`/na_help` 可列出全部命令）：

| 命令 | 功能 |
|------|------|
| `/agnes_y [task_id]` | 批准视频任务（留空则自动选中唯一的待审批任务） |
| `/agnes_n [task_id]` | 拒绝视频任务 |
| `/agnes_list [page]` | 分页任务列表 |
| `/agnes_info <task_id>` | 任务详情（含失败原因原文） |
| `/agnes_help` | 显示帮助 |

## 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `API_KEY` | - | Agnes AI 平台 API Key |
| `BASE_URL` | `https://apihub.agnes-ai.com` | API 基础地址 |
| `TEXT_MODEL` | `agnes-2.5-flash` | 文本模型（限时免费） |
| `IMAGE_MODEL` | `agnes-image-2.5-flash` | 图片模型（当前免费） |
| `VIDEO_MODEL` | `agnes-video-2.5-flash` | 视频模型（限时免费仅 720P；付费档 `agnes-video-2.5`） |
| `REQUIRE_ADMIN_APPROVAL` | `False` | 视频任务是否需要管理员审批 |
| `MANAGER_CHAT_KEY` | - | 接收审批请求的频道，留空则发回原会话 |
| `POLL_INTERVAL` | `2` | 视频轮询间隔（秒），官方建议 1–2 秒 |
| `MAX_POLL_ATTEMPTS` | `300` | 最大轮询次数（约 10 分钟） |
| `APPROVAL_TIMEOUT` | `86400` | 审批等待超时（秒），超时自动拒绝 |
| `DISABLE_TEXT_GENERATION` | `False` | 关闭文本生成能力 |
| `DISPLAY_HISTORY` | `3` | 注入 Agent 上下文的历史记录数 |
| `MAX_HISTORY` | `99` | 每个会话最大历史记录数 |
| `ITEMS_PER_PAGE` | `5` | 任务列表每页条数 |

插件配置里写的模型值会覆盖代码默认值。

## 从 1.x 升级 / 回退

v2.0.0 起只支持 Agnes Video 2.5 的请求形态：

- 旧的 `height/width/num_frames/frame_rate` 由 `seconds/size/aspect_ratio` 取代，插件内不做参数映射
- `agnes-video-v2.0` 仍会被 `GET /v1/models` 列出，但它要求已废弃的 `ti2vid/keyframes/multi_reference` 模式值，收到 2.5 形态的请求返回 400；配置里写着 v2.0 会被归一到 `agnes-video-2.5-flash` 并打日志
- 1.x 落盘的任务记录照常可读、不会被删除（v2.0.1 起容忍 1.x 写下的显式 `null` 字段）；非终态且模型不是 `agnes-video-2.5*` 的 1.x 任务会在启动时一次性标为 `FAILED`，不再重新提交（v2.0.2）
- 回退到 1.1.0：`git checkout stable_20260621`（该 tag 即 1.1.0，仓库没有 `v1.1.0`）后重启。1.1.0 会忽略 2.x 多写的字段、历史列表照读，但 2.x 建的任务在 1.1.0 里没有可用的轮询路径，只能当历史看

## 运行须知（免费档）

- **视频队列经常是满的**：`POST /v1/videos` 返回 `503 video_queue_full`，短时间内连续请求会转成 `429 rate limit for free users`，把文本和图片一起限流。这两类都是提供方的容量与配额，不是插件报错；参数被拒才是 `400`。队列放开的窗口是分钟尺度的，重试要拉开间隔、单发试探。
- **终态通知会唤醒 Agent**：任务失败时插件用 `notify_agent(trigger=True)` 播报结果，Agent 收到后会自己再建一个任务——**不需要提示词里写"失败就重试"**。2026-09-23 实测，一条"直接调用 create_video、不要调用其他工具"的指令在队列持续 503 的情况下被放大成 11 次创建，测试脚本退出后仍在继续。插件本身每次只提交一个请求，放大发生在会话层，所以重试策略要在会话层收口，或直接开 `REQUIRE_ADMIN_APPROVAL`。

## 开发与测试

```bash
pytest tests/                                                  # 离线单测，不触网（需能 import nekro_agent 的环境）
source ~/.zshenv && python scripts/live_smoke.py --skip-video  # 真实 API 冒烟，仅免费模型
```

## API 参考

详细 API 信息请参考 [Agnes 官方文档](https://agnes-ai.com/zh-Hans/docs/overview)。
