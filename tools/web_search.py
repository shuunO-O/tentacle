"""厂商无关的网页搜索工具。"""
from agent.web_sources import execute_web_tool


async def web_search(args: dict) -> str:
    return await execute_web_tool("search", args)


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="web_search",
        description="搜索公开网页，返回标题、URL、发布日期与摘录。queries 用 2–3 个简短词，objective 描述完整问题与时间要求。"
                    "需全文用 web_fetch，引用保留来源链接；勿提交密钥或私人资料。",
        parameters={"type": "object", "properties": {
            "queries": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
            "objective": {"type": "string", "description": "完整检索目标，可包含时间范围和来源偏好"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "description": "默认 5"},
            "mode": {"type": "string", "enum": ["fast", "turbo", "basic", "advanced"],
                     "description": "默认 fast；advanced 用于较复杂检索，费用较高"},
        }, "required": ["queries"]},
        func=web_search, is_async=True,
    )
