"""工具发现及启动校验：合成模块与离线装配，不启动服务或调用外部 API。"""

import importlib
import sys
from uuid import uuid4

import pytest

from runtime.tool_loader import ToolLoader, ToolStartupError
from runtime.tool_registry import ToolRegistry, registry as global_registry


def fresh_registry():
    registry = object.__new__(ToolRegistry)
    registry._tools = {}
    return registry


@pytest.fixture
def package(tmp_path, monkeypatch):
    name = "synthetic_tools_" + uuid4().hex
    directory = tmp_path / name
    directory.mkdir()
    (directory / "__init__.py").write_text("")
    monkeypatch.syspath_prepend(str(tmp_path))

    def make(files):
        for path, content in files.items():
            (directory / path).write_text(content)
        importlib.invalidate_caches()
        return name

    yield make
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            del sys.modules[key]


def declaration(name):
    return f'''def register_tools(registry):
    registry.register({name!r}, "synthetic", {{"type": "object"}}, lambda args: args)
'''


async def test_discovers_sorted_modules_multiple_tools_and_cached_modules(package):
    name = package({
        "z.py": declaration("z"),
        "a.py": declaration("a") + '    registry.register("alias", "", {}, lambda args: "alias")\n',
        "_helper.py": 'raise RuntimeError("must not import helper")',
    })
    for _ in range(2):
        reg = fresh_registry()
        loader = ToolLoader(reg, name)
        loader.load()
        loader.load()  # 同一加载器重复调用不再注册。
        assert list(reg._tools) == ["a", "alias", "z"]
        assert await reg.execute("alias", {}) == "alias"


@pytest.mark.parametrize("bad_module,reason", [
    ('raise RuntimeError("synthetic import failure")', "synthetic import failure"),
    ("pass", "register_tools"),
    (declaration("a"), "重复注册"),
    ('def register_tools(registry):\n    registry.register("bad", "", {"type":"array"}, lambda args: "")',
     "object schema"),
])
def test_load_failure_reports_module_and_rolls_back_partial_registration(package, bad_module, reason):
    name = package({"a.py": declaration("a"), "z_bad.py": bad_module})
    reg = fresh_registry()
    reg.register("existing", "", {}, lambda args: "kept")
    loader = ToolLoader(reg, name)
    with pytest.raises(ToolStartupError, match=reason) as error:
        loader.load()
    assert name + ".z_bad" in str(error.value)
    assert list(reg._tools) == ["existing"]
    assert not loader.modules


def test_config_errors_are_aggregated_and_do_not_include_model_credentials():
    loader = ToolLoader(fresh_registry())
    raw = {
        "providers": {"p": {"type": "openai"}},
        "models": {"m": {"provider": "p", "name": "gpt-4o-mini"}},
        "roles": {
            "chat": {"model": "m", "api_key": "synthetic-private-key", "tools": {"enabled": ["missing", "missing", 12]}},
            "compress": {"tools": {"enabled": "not-a-list"}},
            "broken": {"tools": []},
        },
    }
    with pytest.raises(ToolStartupError) as error:
        loader.validate_config(raw)
    message = str(error.value)
    assert all(text in message for text in ["roles.chat", "roles.compress", "roles.broken", "重复", "未注册"])
    assert "synthetic-private-key" not in message


def test_separate_executor_namespace_and_disabled_tools():
    reg = fresh_registry()
    reg.register("main_only", "", {}, lambda args: "")
    loader = ToolLoader(reg)
    raw = {
        "providers": {"p": {"type": "openai"}},
        "models": {"m": {"provider": "p", "name": "gpt-4o-mini"}},
        "roles": {
            "chat": {"model": "m", "tools": {"enabled": ["main_only"]}},
            "worker": {"tools": {"enabled": ["sandbox_only"]}},
            "compress": {"tools": {"enabled": []}},
            "embedding": {},
            "null": {"tools": {"enabled": None}},
        },
    }
    assert loader.validate_config(raw, role_tools={"worker": {"sandbox_only"}}) == {"main_only"}
    raw["roles"]["worker"]["tools"]["enabled"] = ["main_only"]
    with pytest.raises(ToolStartupError, match="roles.worker"):
        loader.validate_config(raw, role_tools={"worker": {"sandbox_only"}})


def test_missing_dependency_prevents_binding_and_execution(package):
    name = package({"state.py": '''state = None
def bind(value):
    global state
    state = value
TOOL_BINDINGS = {"service": bind}
def execute(args):
    raise AssertionError("startup must never execute a tool")
def register_tools(registry):
    registry.register("dependent", "", {}, execute, requires=("service",))
'''})
    loader = ToolLoader(fresh_registry(), name)
    loader.load()
    module = loader.modules[name + ".state"]
    with pytest.raises(ToolStartupError, match="dependent.*service"):
        loader.bind({"service": None}, {"dependent"})
    assert module.state is None
    service = object()
    loader.bind({"service": service}, {"dependent"})
    assert module.state is service
    loader.unbind()
    assert module.state is None
    # 未启用的工具不要求创建运行依赖；保留明确的未装配状态。
    loader.bind({}, set())
    assert module.state is None


def test_dependency_value_without_binding_is_rejected(package):
    name = package({"tool.py": 'def register_tools(registry):\n'
                    '    registry.register("dependent", "", {}, lambda args: "", requires=("service",))'})
    loader = ToolLoader(fresh_registry(), name)
    loader.load()
    with pytest.raises(ToolStartupError, match="缺少依赖绑定声明"):
        loader.bind({"service": object()}, {"dependent"})


def test_builtin_tools_load_and_bind_without_application_start():
    pytest.importorskip("termios")
    from subagents.config import LEGACY_TOOLS

    reg = fresh_registry()
    loader = ToolLoader(reg)
    loader.load()
    assert reg.has("file_edit") and reg.has("self_rollback")
    assert reg.has("web_search") and reg.has("web_fetch")
    assert reg.has("terminal_run") and reg.has("terminal_interact")
    assert reg.has("skills_list") and reg.has("skill_view")
    raw = {
        "providers": {"p": {"type": "openai"}},
        "models": {"m": {"provider": "p", "name": "gpt-4o-mini"}},
        "roles": {
            "chat": {"model": "m", "tools": {"enabled": list(reg._tools)}},
            "subagent": {"tools": {"enabled": sorted(LEGACY_TOOLS)}},
        },
    }
    selected = loader.validate_config(raw, role_tools={
        "subagent": LEGACY_TOOLS,
    })
    assert selected == set(reg._tools)
    context = {"builder": object(), "session_manager": object(), "subagent_manager": object(), "maintenance": object()}
    try:
        loader.bind(context, selected)
        assert loader.modules["tools.keep_in_mind"]._builder is context["builder"]
        assert loader.modules["tools.maintenance"]._controller is context["maintenance"]
    finally:
        loader.unbind()


def test_normal_tool_import_has_no_registration_side_effect():
    from tools import web_search

    before = dict(global_registry._tools)
    importlib.reload(web_search)
    assert global_registry._tools == before


def test_validate_config_and_config_tools_exclude_plugin_and_mcp_tools():
    reg = fresh_registry()
    reg.register("send_message", "", {}, lambda args: "ok")
    reg.register("file_read", "", {}, lambda args: "ok")
    reg.register("p_browser__browser", "", {}, lambda args: "ok", source="plugin:browser", auto_expose=True)
    reg.register("mcp_demo__ping", "", {}, lambda args: "ok", source="mcp:demo", auto_expose=True)

    loader = ToolLoader(reg)
    layered = {
        "config_version": 2,
        "providers": {"p": {"type": "openai"}},
        "models": {"m": {"provider": "p", "name": "gpt-4o-mini"}},
        "roles": {
            "chat": {"model": "m", "tools": {"enabled": ["send_message"]}},
            "subagent": {"model": None, "tools": {"enabled": ["terminal_run"]}},
        },
    }
    tools_map = loader.configuration_tools(layered, role_tools={"subagent": {"terminal_run", "file_read"}})
    assert tools_map["chat"] == ["file_read", "send_message"]
    assert tools_map["subagent"] == ["file_read", "terminal_run"]

    assert loader.validate_config(layered, role_tools={"subagent": {"terminal_run"}}) == {"send_message"}

    layered["roles"]["chat"]["tools"]["enabled"] = ["send_message", "p_browser__browser", "mcp_demo__ping"]
    with pytest.raises(ToolStartupError) as err:
        loader.validate_config(layered, role_tools={"subagent": {"terminal_run"}})
    msg = str(err.value)
    assert "p_browser__browser" in msg and "mcp_demo__ping" in msg and "/plugins" in msg
