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

- NekroAgent **>= 2.4.0**（使用 `mount_command` 命令系统与 `mount_async_task` 任务系统）
- 不再依赖 nonebot

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

### 从 v1.x 升级

- `agnes-video-v2.0` 已于 2026-09-25 下线；若历史配置仍写着 v2.0 或 `agnes-2.0-flash`，插件会自动归一到 2.5 系列免费模型并打日志提示
- 旧版 `height/width/num_frames/frame_rate` 参数已被 2.5 的 `seconds/size/aspect_ratio` 取代
- 管理命令名保持不变，但底层已从 nonebot 迁移到框架命令系统

## 开发与测试

```bash
# 离线单元测试（不触网；需能 import nekro_agent 的环境）
pytest tests/

# 真实 API 冒烟（仅免费模型：文本 + 1K 图 + 4s 720P 视频）
source ~/.zshenv   # 提供 AGNES_API_KEY
python scripts/live_smoke.py            # 全量
python scripts/live_smoke.py --skip-video
```

## API 参考

详细 API 信息请参考 [Agnes 官方文档](https://agnes-ai.com/zh-Hans/docs/overview)。

## 许可证

MIT License. See [LICENSE](LICENSE).
