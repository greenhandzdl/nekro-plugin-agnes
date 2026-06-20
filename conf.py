from nekro_agent.services.plugin.base import ConfigBase, NekroPlugin
from pydantic import Field

plugin = NekroPlugin(
    name="Agnes AI Generation",
    module_name="agnes_ai_generation",
    description="通过 Agnes AI API 进行文本、图片和视频生成",
    version="1.1.0",
    author="greenhandzdl",
    url="https://github.com/greenhandzdl/nekro-plugin-agnes",
)


@plugin.mount_config()
class AgnesConfig(ConfigBase):
    """Agnes AI 插件配置"""

    API_KEY: str = Field(
        default="",
        title="Agnes API Key",
        description="Agnes AI 平台的 API Key，留空则从环境变量读取",
    )
    BASE_URL: str = Field(
        default="https://apihub.agnes-ai.com",
        title="API 基础地址",
        description="Agnes API 的基础 URL",
    )
    TIMEOUT: int = Field(
        default=120,
        title="请求超时时间",
        description="API 请求的超时时间（秒）",
    )
    TEXT_MODEL: str = Field(
        default="agnes-2.0-flash",
        title="文本模型",
        description="文本生成使用的模型名称",
    )
    IMAGE_MODEL: str = Field(
        default="agnes-image-2.1-flash",
        title="图片模型",
        description="图片生成/编辑使用的模型名称",
    )
    VIDEO_MODEL: str = Field(
        default="agnes-video-v2.0",
        title="视频模型",
        description="视频生成使用的模型名称",
    )
    POLL_INTERVAL: int = Field(
        default=10,
        title="轮询间隔",
        description="视频任务状态轮询间隔（秒）",
    )
    MAX_POLL_ATTEMPTS: int = Field(
        default=60,
        title="最大轮询次数",
        description="视频任务状态查询的最大次数，默认 60 次（约 10 分钟）",
    )
    DISABLE_TEXT_GENERATION: bool = Field(
        default=False,
        title="禁用文本生成",
        description="开启后 generate_text 将始终返回不可用提示",
    )
    # --- 视频管理 ---
    REQUIRE_ADMIN_APPROVAL: bool = Field(
        default=False,
        title="是否需要管理员审批",
        description="视频生成是否需要管理员审批，关闭后直接开始生成（可能增加 API 消耗）",
    )
    MANAGER_CHAT_KEY: str = Field(
        default="",
        title="管理频道",
        description="接收视频审批请求的频道，留空则使用发起请求的频道",
    )
    DISPLAY_HISTORY: int = Field(
        default=3,
        title="显示历史数量",
        description="在 Agent 上下文中注入的最近历史记录数量",
    )
    MAX_HISTORY: int = Field(
        default=99,
        title="最大历史记录",
        description="每个会话保留的最大历史记录条数",
    )
    ITEMS_PER_PAGE: int = Field(
        default=5,
        title="每页显示任务数",
        description="管理员查询任务列表时每页显示的任务数量",
    )


# 获取配置
config: AgnesConfig = plugin.get_config(AgnesConfig)
# 获取插件存储
store = plugin.store
