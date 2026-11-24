"""
协议模块

定义主循环与外部通信的标准数据结构：
- ModelConfig: 单个模型配置
- Context: 请求级上下文，每次用户输入时创建
- Event: 主循环派发给适配器的事件
- LoopResult: 主循环执行结果

所有通过回调传递的数据都封装为 Event 对象，保证格式统一。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional


class EventType(Enum):
    """事件类型枚举，覆盖主循环中所有可能的状态变化"""
    THINKING = "thinking"              # 模型的思考内容（reasoning_content）- 非流式
    THINKING_DELTA = "thinking_delta"  # 增量思考内容 - 流式
    TEXT = "text"                      # 模型的文本输出 - 非流式
    TEXT_DELTA = "text_delta"          # 增量文本内容 - 流式
    TOOL_CALL_START = "tool_call_start"  # 模型发起工具调用
    TOOL_RESULT = "tool_result"        # 工具执行完成，返回结果
    FINAL_ANSWER = "final_answer"      # 模型输出结束；宿主持久化尚未完成
    ROUND_FINISHED = "round_finished"  # 宿主回合终态（命令与模型共用）
    ERROR = "error"                    # 主循环发生未预期异常
    NOTICE = "notice"                  # 宿主提示（不进入模型上下文或对话归档）
    USAGE = "usage"                    # Token 用量统计（单次调用）
    WEB_SEARCH = "web_search"          # 联网搜索状态更新（Responses API 原生 web_search）


@dataclass
class ModelConfig:
    """
    单个模型配置

    按需从 ConfigManager 获取，传递给 AgentLoop。

    Attributes:
        name: 模型名称
        base_url: API 地址
        api_key: API 密钥
        temperature: 温度参数，控制输出随机性
        reasoning_effort: 推理努力程度（o1 系列模型参数）
        stream: 是否启用流式输出
        max_iterations: 最大循环次数
        tool_names: 可用工具名列表
        user_id: DeepSeek user_id，用于模块级 KV cache 隔离
        api_style: API 风格（方言），"chat" / "responses" / "interactions" /
                  "gemini_generate"（Cloud Gemini 原生 generateContent）
        provider: 供应商怪癖（quirks）标识，同一方言内的字段差异由此区分；
                  默认通用 OpenAI 兼容行为；旧格式缺省在配置解析时保持历史兼容
        proxy: 显式 HTTP 代理；空字符串直连，None 保留环境代理行为
        auth: 认证方式；"api_key" 使用 api_key_env，"adc" 使用 Google ADC
        project: Google Cloud project ID（Interactions Cloud 路径使用）
        location: Google Cloud location，Interactions 当前使用 global
        service_tier: Gemini 消费层级；standard / priority / flex
        vision: 模型是否具备视觉能力；False 时各方言把图片块替换为占位文本
        audio / video: Gemini 音视频输入能力，分别显式启用；其他接口保留占位文本
        chat_file_id_channel: chat 方言下是否用 file_id 直引图片（DeepSeek Files API
                  支持跨 API 家族引用）；False 时回退 base64 data URL 内联
    """
    name: str
    base_url: str
    api_key: str = ""
    temperature: Optional[float] = None
    reasoning_effort: str = "high"
    stream: bool = True
    max_iterations: int = 10
    tool_names: Optional[list[str]] = None
    response_format: Optional[dict] = None
    user_id: Optional[str] = None
    api_style: str = "chat"
    provider: str = "openai_compatible"
    vision: bool = False
    audio: bool = False
    video: bool = False
    chat_file_id_channel: bool = True
    proxy: Optional[str] = None  # None 继承环境；空字符串直连；URL 显式代理
    auth: str = "api_key"
    project: Optional[str] = None
    location: str = "global"
    credentials_env: str = ""  # 环境变量保存服务账号 JSON 路径；空值使用 GOOGLE_APPLICATION_CREDENTIALS
    gcs_base_url: str = ""  # 此 Vertex 连接的私有 bucket/prefix
    max_retries: int = 3
    retry_base_seconds: float = 1.0
    service_tier: Optional[str] = None
    explicit_cache: Optional[dict] = None  # 仅 Cloud Gemini generateContent 使用
    heartbeat_service_tier: Optional[str] = "flex"
    heartbeat_timeout_seconds: float = 300.0  # 每次主模型请求总预算，含重试；不取消工具
    heartbeat_max_retries: int = 1
    interactions_stateful: bool = False  # 显式启用远端续接；完整历史始终保留在本地

    provider_id: str = ""
    model_id: str = ""
    role: str = ""
    provider_type: str = ""  # declared connection type, separate from protocol quirks





@dataclass
class Context:
    """
    请求级上下文

    每次用户输入时由适配器创建，贯穿整个主循环执行过程。

    Attributes:
        current_message: 最新用户输入
        history_messages: 历史对话（由 adapter 维护）
        system_prompt: 系统提示词
        platform: 平台标识，如 "web"、"qq"、"feishu"
        user_id: 用户标识，可选
        session_id: 会话标识，可选
        created_time: 创建时间戳
        image_resolver: 图片块 base64 解析回调（可选，异步），签名
                async (block: dict) -> str | None（返回 base64 data URL）；
                由 adapter 注入（FileManager.image_block_to_data_url），
        image_file_resolver: 按目标上传策略解析/惰性创建远端引用的回调，签名
                async (block, uploader_names) -> file_id | None
                方言层 base64 通道使用，方言保持无状态
    """
    current_message: str
    history_messages: list[dict]
    system_prompt: str
    platform: str
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    created_time: float = 0.0
    image_resolver: Optional[Callable] = None
    image_file_resolver: Optional[Callable] = None
    subagent_depth: int = 0  # 运行时赋值，限制后台结果的自动委派链；用户输入默认重新从 0 开始。
    source: str = "user"  # 宿主来源，不能从 current_message 文本推断
    interrupt_event: Any = None  # 仅心跳主模型等待阶段可取消；工具批次只在闭合后检查
    stop_event: Any = None  # 用户显式停止；取消模型等待，当前工具收尾后跳过余下工具
    round_id: Optional[str] = None
    request_id: Optional[str] = None  # 客户端关联标识，不是去重键
    situation_written: bool = False  # 仅本轮真实 situation_notes.write 成功后置为 True
    excluded_tools: tuple[str, ...] = ()  # 仅当前回合的工具限制，不修改全局注册表


@dataclass
class Event:
    """
    标准事件结构

    Attributes:
        type: 事件类型，决定适配器如何处理
        data: 事件携带的具体数据，不同事件类型结构不同
        context: 上下文信息，由适配器注入，主循环原样透传
    """
    type: EventType
    data: Any
    context: Context = None


@dataclass
class LoopResult:
    """
    主循环执行结果

    Attributes:
        full_messages: 完整对话（包含 system_prompt + 所有交互）
        final_answer: 最终回答文本
        total_usage: 累计 Token 用量（所有 API 调用的总和）
        stop_reason: complete 等模型结束原因；command 表示宿主命令已执行，未调用主模型
    """
    full_messages: list[dict]
    final_answer: str
    total_usage: dict
    stop_reason: str = "complete"
    error: Optional[str] = None
    round_status: Optional[dict] = None  # AgentSession 完成宿主处理后填写，与 HTTP/WS 共用
