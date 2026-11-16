"""
工具注册与调度模块

单例模式实现，负责：
1. 工具注册：加载器调用工具模块的 register_tools() 完成注册
2. 定义组装：为大模型生成符合 OpenAI 标准的工具定义列表
3. 执行调度：根据工具名和参数调用对应函数，返回 JSON 字符串

核心设计原则：
- execute() 永不抛出异常，所有错误转为 JSON 返回，确保主循环不崩溃
- 工具定义与实现分离，支持同步/异步两种执行模式
"""

import inspect
import json
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class ToolRegistry:
    """
    工具注册表（单例）

    存储所有已注册工具的元信息和执行函数，提供统一的查询和调度接口。
    """
    _instance = None

    def __new__(cls):
        """单例模式：确保全局只有一个注册表实例"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tools = {}
        return cls._instance

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        func: Callable,
        is_async: Optional[bool] = None,
        *,
        requires: tuple[str, ...] = (),
        source: str = "builtin",
        auto_expose: bool = False,
    ):
        """
        注册一个工具

        Args:
            name: 工具名称，必须唯一，大模型通过此名调用
            description: 工具描述，会传递给大模型帮助其理解工具用途
            parameters: JSON Schema 格式的参数定义
            func: 工具执行函数，签名为 func(args: dict) -> str
            is_async: 兼容旧调用；默认自动识别，显式声明必须与函数一致
            requires: 运行时装配所需的依赖名称
        """
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError(f"无效工具名: {name!r}")
        if name in self._tools:
            raise ValueError(f"工具重复注册: {name}")
        if not isinstance(description, str):
            raise TypeError(f"工具 {name} 的 description 必须是字符串")
        if not callable(func):
            raise TypeError(f"工具 {name} 的执行函数不可调用")
        # {} 是合法的空 JSON Schema；保留旧工具/测试的无约束定义。
        if not isinstance(parameters, dict) or (parameters and parameters.get("type") != "object"):
            raise ValueError(f"工具 {name} 的参数必须为 object schema")
        properties = parameters.get("properties", {})
        required = parameters.get("required", [])
        if not isinstance(properties, dict) or not all(isinstance(value, dict) for value in properties.values()):
            raise ValueError(f"工具 {name} 的 properties 必须是属性 schema 字典")
        if (not isinstance(required, list) or not all(isinstance(key, str) for key in required)
                or len(set(required)) != len(required) or any(key not in properties for key in required)):
            raise ValueError(f"工具 {name} 的 required 必须引用已声明的属性且不能重复")
        json.dumps(parameters, allow_nan=False)
        inspect.signature(func).bind({})
        detected_async = inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(func.__call__)
        if is_async is not None and (not isinstance(is_async, bool) or is_async != detected_async):
            raise ValueError(f"工具 {name} 的 is_async 与执行函数不一致")
        if not isinstance(requires, tuple) or not all(isinstance(key, str) and key for key in requires):
            raise ValueError(f"工具 {name} 的 requires 必须是依赖名元组")
        self._tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "func": func,
            "is_async": detected_async,
            "requires": requires,
        }
        meta = getattr(self, "_tool_meta", None)
        if meta is None:
            meta = {}
            self._tool_meta = meta
        meta[name] = {"source": source, "auto_expose": bool(auto_expose)}
        logger.info(f"工具已注册: {name}")

    def has(self, name: str) -> bool:
        """查询工具是否已注册（定时任务创建时校验 tool 动作用）"""
        return name in self._tools

    def is_builtin(self, name: str) -> bool:
        """查询工具是否由核心内置模块（非插件/MCP）注册。"""
        if name not in self._tools:
            return False
        meta = getattr(self, "_tool_meta", None) or {}
        return meta.get(name, {}).get("source", "builtin") == "builtin"

    def builtin_names(self) -> list[str]:
        """返回核心内置工具名列表（排除插件与 MCP 动态工具）。"""
        return [name for name in self._tools if self.is_builtin(name)]

    def get_definitions(self, tool_names: Optional[list[str]] = None) -> list[dict]:
        """
        获取大模型所需的工具定义列表

        将内部工具信息转换为 OpenAI Function Calling 标准格式。
        如果不指定 tool_names，则返回所有已注册工具。

        Args:
            tool_names: 需要暴露的工具名列表，None 表示全部

        Returns:
            符合 OpenAI 标准的工具定义列表
        """
        names = list(self._tools.keys()) if tool_names is None else tool_names
        unknown = [name for name in names if name not in self._tools]
        if unknown:
            raise ValueError("未注册工具: " + ", ".join(unknown))
        definitions = []
        for name in names:
            tool = self._tools[name]
            definitions.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            })
        return definitions

    def dependency_errors(self, tool_names, context: dict) -> list[str]:
        """只检查所选工具的装配依赖，不执行工具或探测外部服务。"""
        return [f"工具 {name} 缺少运行依赖: {key}"
                for name in sorted(tool_names) for key in self._tools[name]["requires"]
                if context.get(key) is None]

    async def execute(self, name: str, arguments: dict) -> str | list[dict]:
        """
        执行指定工具

        核心安全设计：捕获所有异常并转为 JSON 错误字符串返回，
        确保主循环不会因为工具故障而崩溃。

        Args:
            name: 工具名称
            arguments: 工具参数字典

        Returns:
            工具执行结果的 JSON 字符串，或错误信息的 JSON 字符串
        """
        if name not in self._tools:
            return json.dumps({"error": f"工具不存在: {name}"}, ensure_ascii=False)

        tool = self._tools[name]
        try:
            # 根据工具声明选择同步或异步调用
            if tool["is_async"]:
                result = await tool["func"](arguments)
            else:
                result = tool["func"](arguments)

            # 统一返回字符串格式；内容块数组（如图片 input_image）保持原样透传，
            # 供 Function call output 以内容块列表发送（视觉模型可读图）
            if isinstance(result, str):
                return result
            if isinstance(result, list) and all(
                isinstance(block, dict) and block.get("type") in ("image", "input_image", "text", "input_text")
                for block in result
            ):
                return result
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.error(f"工具执行异常 [{name}]: {e}", exc_info=True)
            return json.dumps({"error": f"工具执行异常: {str(e)}"}, ensure_ascii=False)

    def _meta(self) -> dict[str, dict]:
        meta = getattr(self, "_tool_meta", None)
        if meta is None:
            meta = {}
            self._tool_meta = meta
        return meta

    def unregister(self, name: str) -> bool:
        """按工具名注销工具。"""
        removed = self._tools.pop(name, None) is not None
        self._meta().pop(name, None)
        return removed

    def unregister_by_source(self, source: str) -> list[str]:
        """注销指定来源（如 plugin:<id> / mcp:<id>）的全部工具。"""
        meta = self._meta()
        targets = [name for name in list(self._tools) if meta.get(name, {}).get("source") == source]
        for name in targets:
            self._tools.pop(name, None)
            meta.pop(name, None)
        return targets

    def get_auto_exposed_names(self) -> list[str]:
        """返回自动暴露给主对话的插件/MCP 工具名列表。"""
        meta = self._meta()
        return [name for name in self._tools if meta.get(name, {}).get("auto_expose")]

    def resolve_tool_names(self, configured_names: Optional[list[str]], *, role: Optional[str] = None) -> Optional[list[str]]:
        """合并主对话显式工具列表与插件自动暴露工具。"""
        if configured_names is None:
            return None
        if role != "chat":
            return list(configured_names)
        merged = list(configured_names)
        seen = set(merged)
        for name in self.get_auto_exposed_names():
            if name not in seen:
                merged.append(name)
                seen.add(name)
        return merged


# 全局单例实例，供工具文件导入使用
registry = ToolRegistry()


def tool_result_error(result) -> str | None:
    """结构化工具失败统一判定；普通文本、内容块及嵌套业务数据不是错误标志。"""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return None
    if isinstance(result, dict):
        if result.get('error'):
            return str(result['error'])
        if result.get('status') in ('error', 'failed'):
            return str(result.get('message') or '工具执行失败')
    return None
