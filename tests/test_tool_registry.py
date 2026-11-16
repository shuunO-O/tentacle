"""
ToolRegistry 测试

覆盖：注册、get_definitions 过滤、execute 同步/异步、
execute 永不抛异常（工具不存在 / 函数抛异常均转 JSON 错误）。
ToolRegistry 是单例，测试用独立实例避免污染全局 registry。
"""

import pytest


def _fresh_registry():
    """绕过单例 new 一个干净实例。"""
    from runtime.tool_registry import ToolRegistry

    return ToolRegistry()


def test_register_and_get_definitions():
    reg = _fresh_registry()
    reg.register(
        name="t_hello",
        description="测试工具",
        parameters={"type": "object", "properties": {}},
        func=lambda args: "ok",
    )
    defs = reg.get_definitions(["t_hello"])
    assert len(defs) == 1
    assert defs[0]["function"]["name"] == "t_hello"
    assert defs[0]["type"] == "function"
    with pytest.raises(ValueError, match="not_registered"):
        reg.get_definitions(["t_hello", "not_registered"])


def test_get_definitions_none_returns_all():
    reg = _fresh_registry()
    reg.register(name="a", description="", parameters={}, func=lambda a: "")
    reg.register(name="b", description="", parameters={}, func=lambda a: "")
    assert len(reg.get_definitions()) == 2


async def test_execute_sync_func():
    import json

    reg = _fresh_registry()
    reg.register(name="t_sync", description="", parameters={}, func=lambda args: json.dumps(args))

    result = await reg.execute("t_sync", {"x": 1})
    assert json.loads(result) == {"x": 1}


async def test_execute_async_func():
    import json

    async def double(args):
        return json.dumps({"v": args["x"] * 2})

    reg = _fresh_registry()
    reg.register(name="t_async", description="", parameters={}, func=double, is_async=True)

    result = await reg.execute("t_async", {"x": 21})
    assert json.loads(result) == {"v": 42}


async def test_execute_unknown_tool_returns_json_error():
    import json

    reg = _fresh_registry()
    result = await reg.execute("nope", {})
    assert "error" in json.loads(result)


async def test_execute_exception_returns_json_error():
    """核心安全设计：函数抛异常不冒泡，转 JSON 错误返回。"""
    import json

    def boom(args):
        raise RuntimeError("炸了")

    reg = _fresh_registry()
    reg.register(name="t_boom", description="", parameters={}, func=boom)

    result = await reg.execute("t_boom", {})
    data = json.loads(result)
    assert "error" in data
    assert "炸了" in data["error"]


async def test_execute_non_str_result_serialized():
    import json

    reg = _fresh_registry()
    reg.register(name="t_dict", description="", parameters={}, func=lambda args: {"status": "ok"})

    result = await reg.execute("t_dict", {})
    assert json.loads(result) == {"status": "ok"}


def test_empty_selection_does_not_expose_all_tools():
    reg = _fresh_registry()
    reg.register("a", "", {}, lambda args: "ok")
    assert reg.get_definitions([]) == []


def test_duplicate_registration_preserves_original():
    reg = _fresh_registry()
    original = lambda args: "original"
    reg.register("a", "", {}, original)
    with pytest.raises(ValueError, match="重复"):
        reg.register("a", "", {}, lambda args: "replacement")
    assert reg._tools["a"]["func"] is original


@pytest.mark.parametrize("changes", [
    {"name": "bad name"}, {"func": None}, {"func": lambda a, b: "bad signature"},
    {"parameters": {"type": "array"}}, {"parameters": {"type": "object", "properties": []}},
    {"parameters": {"type": "object", "required": ["missing"]}},
    {"parameters": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x", "x"]}},
    {"requires": ["builder"]}, {"is_async": True},
])
def test_invalid_registration_is_rejected_before_insertion(changes):
    reg = _fresh_registry()
    args = dict(name="test", description="", parameters={}, func=lambda args: "ok")
    args.update(changes)
    with pytest.raises((TypeError, ValueError)):
        reg.register(**args)
    assert not reg._tools


async def test_async_mode_is_inferred_and_images_pass_through():
    reg = _fresh_registry()
    blocks = [{"type": "image", "data": "synthetic"}]

    async def screenshot(args):
        return blocks

    reg.register("screenshot", "", {}, screenshot)
    assert await reg.execute("screenshot", {}) is blocks


async def test_cancellation_is_not_swallowed():
    import asyncio

    reg = _fresh_registry()

    async def cancelled(args):
        raise asyncio.CancelledError

    reg.register("cancelled", "", {}, cancelled)
    with pytest.raises(asyncio.CancelledError):
        await reg.execute("cancelled", {})


def test_builtin_filters_exclude_plugin_and_mcp_tools():
    reg = _fresh_registry()
    reg.register("send_message", "", {}, lambda args: "ok")
    reg.register("p_browser__browser", "", {}, lambda args: "ok", source="plugin:browser", auto_expose=True)
    reg.register("mcp_demo__ping", "", {}, lambda args: "ok", source="mcp:demo", auto_expose=True)

    assert reg.has("p_browser__browser")
    assert reg.has("mcp_demo__ping")
    assert reg.is_builtin("send_message")
    assert not reg.is_builtin("p_browser__browser")
    assert not reg.is_builtin("mcp_demo__ping")
    assert not reg.is_builtin("missing")
    assert reg.builtin_names() == ["send_message"]
