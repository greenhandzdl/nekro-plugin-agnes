# Agnes AI Generation Plugin

> 一个用于 NekroAgent 的 Agnes AI 生成插件，提供文本、图片和视频生成功能。

## 功能

- **文本生成**：使用 `agnes-2.0-flash` 模型生成文本，支持流式输出
- **文生图**：使用 `agnes-image-2.1-flash` 生成图片
- **图生图 / 图片编辑**：基于输入图片进行修改
- **文生视频**：使用 `agnes-video-v2.0` 创建视频
- **图生视频**：将静态图片动态化
- **多图视频 / 关键帧动画**：多张图片生成过渡动画
- **自动翻译**：非英文提示词自动翻译为英文，提高生成质量

## 安装

### 1. 安装插件包

```bash
cd nekro-plugin-agnes
pip install -e .
```

或直接从 GitHub 安装：

```bash
pip install git+https://github.com/Yacey/agnes-ai-generation-skill.git
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

### 工具列表

| 工具名 | 功能 | 关键参数 |
|--------|------|----------|
| `generate_text` | 文本生成 | `prompt`, `system`, `temperature`, `max_tokens`, `stream` |
| `generate_image` | 文生图 / 图生图 | `prompt`, `size`, `input_image_url`, `translate_prompt` |
| `create_video` | 创建视频任务 | `prompt`, `image_url`, `image_urls`, `mode`, `num_frames`, `poll` |
| `get_video` | 查询视频状态 | `task_id` |

### 示例

让 Agent 生成一张图片：

```
使用 Agnes 帮我生成一张高信息密度的未来城市图片。
```

让 Agent 生成视频：

```
使用 Agnes 把这张图片生成一段电影感视频。
```

## API 参考

详细 API 信息请参考原始 skill 文档中的 `references/api.md`。

## 许可证

MIT License. See [LICENSE](LICENSE).
