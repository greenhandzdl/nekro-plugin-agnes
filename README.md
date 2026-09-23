# Agnes AI Generation Plugin

> 一个用于 NekroAgent 的 Agnes AI 生成插件，提供文本、图片和视频生成功能。

**官网**: [https://agnes-ai.com/](https://agnes-ai.com/)

## 功能

- **文本生成**：默认 `agnes-2.5-flash`（限时免费），支持流式输出、多模态输入（文本+图片）
- **文生图**：默认 `agnes-image-2.5-flash`（当前免费），档位制尺寸 `1K/2K/3K/4K` + 宽高比 `ratio`
- **图生图 / 多图合成**：基于输入图片进行修改，支持多张参考图
- **文生视频**：Agnes Video 2.5 系列异步生成（`agnes-video-2.5-flash` 限时免费，仅 720P）
- **首尾帧关键帧动画**：`keyframe` 模式用首帧/尾帧控制起止构图
- **多模态参考视频**：`reference` 模式支持图片/音频/视频参考（付费 `agnes-video-2.5` 支持视频参考）
- **自动翻译**：非英文提示词自动翻译为英文，提高生成质量
- **视频审批流程**：可选的管理员审批机制，控制 API 资源消耗
- **框架深度集成**：管理命令走 NekroAgent 命令系统（自动权限/帮助），视频轮询走框架异步任务系统，重启后自动恢复未完成任务

## 版本要求

| 项 | 要求 | 实测环境 |
|----|------|----------|
| NekroAgent | >= 2.4.0 | 2.4.0 |
| Python | >= 3.11, < 3.13（跟随框架约束） | 3.11.13 |

用到的框架能力（决定最低版本）：

- 命令系统：`plugin.mount_command` + `nekro_agent.services.command`（`CommandPermission`、`CmdCtl`、`Arg`、`CommandExecutionContext`、`CommandResponse`）
- 异步任务：`plugin.mount_async_task` + `nekro_agent.services.plugin.task`（`task`、`TaskCtl`、`AsyncTaskHandle`），视频轮询与重启恢复都走这里
- 其余：`mount_sandbox_method` / `mount_init_method` / `mount_cleanup_method` / `mount_prompt_inject_method`、`store.get/set`、`push_system`

不再依赖 nonebot。`mount_command` 在框架 2.3.0 就已存在，但本插件只在 2.4.0 上验证过；回落到 2.3.x 需自测命令注册。

**更新插件代码必须重启进程**：框架用 `import_module` 取模块，已加载过的插件会命中 `sys.modules` 缓存，热重载看不到新文件（Docker 部署即 `docker restart <container>`）。

## 安装

### 1. 安装插件包

```bash
cd nekro-plugin-agnes
pip install -e .
```

或直接从 GitHub 安装：

```bash
pip install git+https://github.com/greenhandzdl/nekro-plugin-agnes.git
```

### 2. 配置 API Key

在 NekroAgent 插件配置中设置 `API_KEY`，或设置环境变量：

```bash
export AGNES_API_KEY="your_api_key"
```

也支持以下环境变量名：`AGNES_API_TOKEN`、`APIHUB_AGNES_API_KEY`

### 3. 注册到 NekroAgent

确保插件被 NekroAgent 发现并加载。参考 NekroAgent 插件配置文档。

## 使用

安装并配置后，Agent 会自动在以下场景调用本插件：

- 要求生成文本内容时
- 要求生成图片时（"画一张..."、"生成图片..."）
- 要求生成视频时（"制作视频..."、"把图片动起来..."）
- 要求编辑图片时（"修改这张图..."、"把图片变成..."）

### Agent 工具列表

| 工具名 | 类型 | 功能 | 关键参数 |
|--------|------|------|----------|
| `generate_text` | AGENT | 文本生成（支持多模态） | `prompt`, `images`, `system`, `temperature`, `stream` |
| `generate_image` | TOOL | 文生图 / 图生图 / 多图合成 | `prompt`, `size`(1K-4K), `ratio`, `image_urls`, `translate_prompt`, `send_to_chat` |
| `create_video` | BEHAVIOR | 创建视频任务（2.5 系列） | `prompt`, `mode`, `first_frame`, `last_frame`, `images`, `audios`, `videos`, `seconds`, `size`, `aspect_ratio`, `seed`, `reason` |
| `get_video_by_task_id` | TOOL | 按 task_id 获取视频 URL | `task_id` |
| `cancel_current_video_task` | BEHAVIOR | 取消当前会话的视频任务 | 无 |
| `approve_video_task` | BEHAVIOR | 批准待审批的视频任务 | `task_id` |
| `reject_video_task` | BEHAVIOR | 拒绝待审批的视频任务 | `task_id` |
| `list_video_tasks` | TOOL | 分页查询任务列表 | `page` |
| `get_video_task_info` | TOOL | 查询任务详情 | `task_id` |

### 视频模式（Agnes Video 2.5）

| mode | 用途 | 必需媒体 | 不允许 |
|------|------|----------|--------|
| `text` | 纯文本生成视频 | 无 | 任何媒体字段 |
| `keyframe` | 首/尾帧控制构图 | `first_frame` 与 `last_frame` 至少一个 | `images/audios/videos` |
| `reference` | 素材作内容/风格/动作参考 | `images/audios/videos` 至少一类 | `first_frame/last_frame` |

- `seconds`: `"4"`–`"12"`；`size`: `720P/1080P/1K/2K`（flash 模型仅 `720P`）
- `aspect_ratio`: `16:9`（默认）`9:16` `1:1` `4:3` `3:4` `21:9`（视频不支持 `2:3`/`3:2`，图片才支持）
- 参考媒体上限：图片 ≤8（flash ≤5）、音频 ≤3、视频 ≤1（flash 不支持）
- 异步任务：创建后由框架后台协程每 `POLL_INTERVAL` 秒轮询 `GET /agnesapi?video_id=...&model_name=...`，完成/失败自动通知会话

### 管理员命令

由 NekroAgent 命令系统托管，需 SUPER_USERS 权限（`/na_help` 可列出）：

| 命令 | 功能 |
|------|------|
| `/agnes_y [task_id]` | 批准视频任务（留空自动选择唯一待审批任务） |
| `/agnes_n [task_id]` | 拒绝视频任务 |
| `/agnes_list [page]` | 分页任务列表 |
| `/agnes_info <task_id>` | 任务详情 |
| `/agnes_help` | 显示帮助 |

### 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `API_KEY` | - | Agnes AI 平台 API Key |
| `BASE_URL` | `https://apihub.agnes-ai.com` | API 基础地址 |
| `TEXT_MODEL` | `agnes-2.5-flash` | 文本生成模型（限时免费） |
| `IMAGE_MODEL` | `agnes-image-2.5-flash` | 图片生成模型（当前免费） |
| `VIDEO_MODEL` | `agnes-video-2.5-flash` | 视频生成模型（限时免费，仅 720P；付费升级 `agnes-video-2.5`） |
| `REQUIRE_ADMIN_APPROVAL` | `False` | 是否需要管理员审批视频任务 |
| `MANAGER_CHAT_KEY` | - | 接收审批请求的频道 |
| `POLL_INTERVAL` | `2` | 视频轮询间隔（秒），官方建议 1-2 秒 |
| `MAX_POLL_ATTEMPTS` | `300` | 最大轮询次数（约 10 分钟） |
| `APPROVAL_TIMEOUT` | `86400` | 审批等待超时（秒），超时自动拒绝 |
| `DISABLE_TEXT_GENERATION` | `False` | 禁用文本生成 |
| `DISPLAY_HISTORY` | `3` | Agent 上下文注入的历史记录数 |
| `MAX_HISTORY` | `99` | 每个会话最大历史记录数 |
| `ITEMS_PER_PAGE` | `5` | 任务列表每页显示数 |

### 视频状态流转

```
创建 → PENDING (需审批) / QUEUED (不需审批)
PENDING → APPROVED (审批通过) / REJECTED (拒绝/超时)
APPROVED/QUEUED → PROCESSING (API 生成中) → COMPLETED / FAILED
```

任务进入终态时会以系统消息唤醒 Agent（`notify_agent(trigger=True)`）把结果播报给用户。副作用要注意：免费档队列拥塞时失败是常态，如果提示词里要求"失败就重试"，Agent 会连续刷创建（2026-09-23 实测：6 分钟内 task_000007→task_000017 共 11 次 create，全部 503/429），把免费配额刷干。这不是插件在自循环，但重试策略要在会话层面收口。

### 从 v1.x 升级 / 回退到 v1.x

v2.0.0 是破坏性升级（`feat!`），只支持 Agnes Video 2.5 系列的请求形态。

**接口层面**

- 旧版 `height/width/num_frames/frame_rate` 由 2.5 的 `seconds/size/aspect_ratio` 取代，插件内不做参数映射
- `agnes-video-v2.0` 仍能被 `GET /v1/models` 列出（2026-09-23 实测），但它要求旧的 `ti2vid/keyframes/multi_reference` 模式值，收到 2.5 形态的请求会返回 400；插件已不再构建这类请求，配置里写着 v2.0 会被归一到 `agnes-video-2.5-flash` 并打日志
- 管理命令名保持不变，底层从 nonebot 迁移到框架命令系统（因此受上面的最低框架版本约束）

**历史数据兼容**

- 1.1.0 落盘的任务记录中 `model/mode/seconds/size/aspect_ratio` 可能是显式 `null`，而 pydantic v2 不会给显式 `null` 套用默认值，会导致整个任务表反序列化失败、插件加载中断。v2.0.1 起用 `field_validator(mode="before")` 把这些字段归一到声明的默认值
- 启动恢复时，非终态且模型不是 `agnes-video-2.5*` 的 1.x 任务会被一次性标为 `FAILED`（旧记录没有 `video_id`，无路可续轮询），不会重新提交到 API（v2.0.2）
- 历史任务/会话记录不会被删除，1.x 建的已完成任务在 2.x 里照常查阅

**回退到 1.1.0**

- `git checkout stable_20260621`（该 tag 即 1.1.0，仓库没有 `v1.1.0` tag），或恢复升级前的插件目录副本，然后重启实例（理由见「版本要求」）
- 2.x 写入的记录多出 `model/mode/seconds/size/aspect_ratio/video_id` 等字段，1.1.0 的模型未声明 `extra="forbid"`，pydantic v2 默认忽略多余字段，历史列表在 1.1.0 下仍可读；但 2.x 创建的任务在 1.1.0 中没有可用的轮询路径，只能当历史记录看
- 回退后 1.x 会重新按旧参数构建视频请求，2.x 期间被标 `FAILED` 的任务不会自动恢复

## 开发与测试

```bash
# 离线单元测试（不触网；需能 import nekro_agent 的环境）
pytest tests/

# 真实 API 冒烟（仅免费模型：文本 + 1K 图 + 4s 720P 视频）
source ~/.zshenv   # 提供 AGNES_API_KEY
python scripts/live_smoke.py            # 全量
python scripts/live_smoke.py --skip-video
```

免费档的真实限制（2026-09-23 实测）：视频创建经常返回 `503 video_queue_full`，短时间内连续调用会转成 `429 … rate limit for free users`。这两类都是提供方的容量/配额限制，不是插件报错，等队列放开后重试即可；请求参数被拒时返回的是 `400`（例如把 2.5 的请求体发给 `agnes-video-v2.0`）。

## API 参考

详细 API 信息请参考 [Agnes 官方文档](https://agnes-ai.com/zh-Hans/docs/overview)。

## 许可证

MIT License. See [LICENSE](LICENSE).
