# Agnes AI Generation Plugin

> 一个用于 NekroAgent 的 Agnes AI 生成插件，提供文本、图片和视频生成功能。

**官网**: [https://agnes-ai.com/](https://agnes-ai.com/)

## 功能

- **文本生成**：使用 `agnes-2.0-flash` 模型生成文本，支持流式输出、多模态输入（文本+图片）
- **文生图**：使用 `agnes-image-2.1-flash` 生成图片
- **图生图 / 图片编辑**：基于输入图片进行修改，支持多图参考
- **文生视频**：使用 `agnes-video-v2.0` 创建视频
- **图生视频**：将静态图片动态化
- **多图视频 / 关键帧动画**：多张图片生成过渡动画
- **自动翻译**：非英文提示词自动翻译为英文，提高生成质量
- **视频审批流程**：可选的管理员审批机制，控制 API 资源消耗

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
| `generate_image` | TOOL | 文生图 / 图生图 | `prompt`, `size`, `image_urls`, `translate_prompt`, `send_to_chat` |
| `create_video` | BEHAVIOR | 创建视频任务 | `prompt`, `image_urls`, `mode`, `reason`, `height`, `width`, `num_frames` |
| `get_video_by_task_id` | TOOL | 按 task_id 获取视频 URL | `task_id` |
| `cancel_current_video_task` | BEHAVIOR | 取消当前会话的视频任务 | 无 |
| `approve_video_task` | BEHAVIOR | 批准待审批的视频任务 | `task_id` |
| `reject_video_task` | BEHAVIOR | 拒绝待审批的视频任务 | `task_id` |
| `list_video_tasks` | TOOL | 分页查询任务列表 | `page` |
| `get_video_task_info` | TOOL | 查询任务详情 | `task_id` |

### 管理员命令

| 命令 | 功能 | 说明 |
|------|------|------|
| `/agnes_y [task_id]` | 批准视频任务 | 需 SUPER_USERS 权限 |
| `/agnes_n [task_id]` | 拒绝视频任务 | 需 SUPER_USERS 权限 |
| `/agnes_list [page]` | 分页任务列表 | 需 SUPER_USERS 权限 |
| `/agnes_info <task_id>` | 任务详情 | 需 SUPER_USERS 权限 |
| `/agnes_help` | 显示帮助 | 需 SUPER_USERS 权限 |

### 配置项

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `API_KEY` | - | Agnes AI 平台 API Key |
| `BASE_URL` | `https://apihub.agnes-ai.com` | API 基础地址 |
| `TEXT_MODEL` | `agnes-2.0-flash` | 文本生成模型 |
| `IMAGE_MODEL` | `agnes-image-2.1-flash` | 图片生成模型 |
| `VIDEO_MODEL` | `agnes-video-v2.0` | 视频生成模型 |
| `REQUIRE_ADMIN_APPROVAL` | `False` | 是否需要管理员审批视频任务 |
| `MANAGER_CHAT_KEY` | - | 接收审批请求的频道 |
| `POLL_INTERVAL` | `10` | 视频轮询间隔（秒） |
| `MAX_POLL_ATTEMPTS` | `60` | 最大轮询次数 |
| `DISABLE_TEXT_GENERATION` | `False` | 禁用文本生成 |
| `DISPLAY_HISTORY` | `3` | Agent 上下文注入的历史记录数 |
| `MAX_HISTORY` | `99` | 每个会话最大历史记录数 |
| `ITEMS_PER_PAGE` | `5` | 任务列表每页显示数 |

### 视频状态流转

```
创建 → PENDING (需审批) / QUEUED (不需审批)
PENDING → APPROVED (审批通过) / REJECTED (审批拒绝)
APPROVED/QUEUED → PROCESSING (API 生成中) → COMPLETED / FAILED
```

### 示例

让 Agent 生成一张图片：

```
使用 Agnes 帮我生成一张高信息密度的未来城市图片。
```

让 Agent 生成视频：

```
使用 Agnes 把这张图片生成一段电影感视频。
```

Agent 调用示例（通过对话）：

```
帮我把这两张图融合成一张新图
```

```
帮我生成一段猫咪在海滩上奔跑的视频，动画风格
```

## API 参考

详细 API 信息请参考 [Agnes 官方文档](https://agnes-ai.com/)。

## 许可证

MIT License. See [LICENSE](LICENSE).
