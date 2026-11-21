"""内置 browser 插件：纯 Docker Neko 本机 CDP 发现、安全审查与 PluginManager 集成测试。"""

import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import urllib.error
from unittest.mock import MagicMock

import pytest

from agent.commands import CommandRegistry
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from runtime.tool_registry import ToolRegistry


def _load_browser_plugin_module():
    path = Path(__file__).resolve().parents[1] / "plugins" / "bundled" / "browser" / "plugin.py"
    spec = importlib.util.spec_from_file_location("test_bundled_browser_plugin", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


browser_plugin = _load_browser_plugin_module()
DISCOVERY = "http://127.0.0.1:9222/json/version"


@pytest.fixture
def config():
    return {
        "executable": "/fake/agent-browser",
        "neko_cdp_port": 9222,
        "timeout": 20,
        "max_output": 15000,
        "allow_eval": False,
        "allowed_domains": [],
    }


def mock_discovery(monkeypatch, payload):
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = payload
    opener = MagicMock()
    opener.open.return_value = response
    build = MagicMock(return_value=opener)
    monkeypatch.setattr(browser_plugin.urllib.request, "build_opener", build)
    return build, opener, response


@pytest.mark.parametrize(
    "endpoint, websocket, expected",
    [
        (DISCOVERY, "ws://127.0.0.1:5555/devtools/browser/abc", "ws://127.0.0.1:9222/devtools/browser/abc"),
        (DISCOVERY, "ws://localhost:9222/devtools/browser/abc?q=1", "ws://127.0.0.1:9222/devtools/browser/abc?q=1"),
        ("http://127.0.0.1:19222/json/version", "ws://0.0.0.0:9222/devtools/browser/xyz",
         "ws://127.0.0.1:19222/devtools/browser/xyz"),
    ],
)
def test_discovery_rewrites_authority_and_preserves_path(monkeypatch, endpoint, websocket, expected):
    build, opener, response = mock_discovery(monkeypatch, json.dumps({"webSocketDebuggerUrl": websocket}).encode())
    assert browser_plugin._discover_cdp_url(endpoint, 5) == expected
    opener.open.assert_called_once_with(endpoint, timeout=5)
    response.read.assert_called_once_with(65537)
    assert build.call_args.args[0].proxies == {}


@pytest.mark.parametrize(
    "payload",
    [b"invalid-json", b"[]", b"{}", b'{"webSocketDebuggerUrl": 123}',
     b'{"webSocketDebuggerUrl": "http://localhost/devtools/browser/a"}',
     b'{"webSocketDebuggerUrl": "ws://localhost"}', b"x" * 65537],
    ids=['invalid-json', 'non-object', 'missing-url', 'non-string-url',
         'wrong-scheme', 'missing-path', 'oversized-response'],
)
def test_invalid_discovery_does_not_execute_cli(monkeypatch, config, payload):
    mock_discovery(monkeypatch, payload)
    run = MagicMock()
    monkeypatch.setattr(browser_plugin.subprocess, "run", run)
    result = browser_plugin.run_browser_command({"command": "snapshot"}, config)
    assert "Docker Neko 浏览器 CDP" in result
    run.assert_not_called()


@pytest.mark.parametrize("bad_port", [0, -1, 70000, "not-a-port"])
def test_invalid_cdp_port_is_rejected(monkeypatch, config, bad_port):
    config["neko_cdp_port"] = bad_port
    opener = MagicMock()
    run = MagicMock()
    monkeypatch.setattr(browser_plugin.urllib.request, "build_opener", opener)
    monkeypatch.setattr(browser_plugin.subprocess, "run", run)
    assert "Docker Neko 浏览器 CDP" in browser_plugin.run_browser_command({"command": "snapshot"}, config)
    opener.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("endpoint", ["ws://127.0.0.1:9222/a", "http://192.0.2.10:9222/json/version", "http://u:p@127.0.0.1:9222/a"])
def test_non_loopback_discovery_endpoint_is_rejected(endpoint):
    with pytest.raises(ValueError):
        browser_plugin._discover_cdp_url(endpoint, 5)


def test_offline_neko_does_not_execute_cli(monkeypatch, config):
    _, opener, _ = mock_discovery(monkeypatch, b"{}")
    opener.open.side_effect = urllib.error.URLError("connection refused")
    run = MagicMock()
    monkeypatch.setattr(browser_plugin.subprocess, "run", run)
    result = browser_plugin.run_browser_command({"command": "snapshot"}, config)
    assert "Docker Neko 浏览器 CDP" in result
    assert "tentacle browser start" in result
    run.assert_not_called()


def test_each_command_rediscovers_and_isolates_cli_launch(monkeypatch, config):
    _, _, response = mock_discovery(monkeypatch, b"")
    response.read.side_effect = [
        b'{"webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/before"}',
        b'{"webSocketDebuggerUrl": "ws://localhost:9222/devtools/browser/after"}',
    ]
    monkeypatch.setenv("AGENT_BROWSER_PROVIDER", "browserbase")
    monkeypatch.setenv("AGENT_BROWSER_CONFIG", "/fake/stale-config.json")
    monkeypatch.setenv("AGENT_BROWSER_PROFILE", "/fake/stale-profile")
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", "/fake/stale-chrome")
    calls = []

    def run(cmd, **kwargs):
        isolated_config = Path(cmd[cmd.index("--config") + 1])
        assert json.loads(isolated_config.read_text()) == {}
        assert "--pin-tab" in cmd
        assert not {"--headed", "--profile", "--args"}.intersection(cmd)
        assert not any(key.startswith("AGENT_BROWSER_") for key in kwargs["env"])
        calls.append((cmd, isolated_config))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(browser_plugin.subprocess, "run", run)
    assert browser_plugin.run_browser_command({"command": "open about:blank"}, config) == "ok"
    assert browser_plugin.run_browser_command({"command": "snapshot"}, config) == "ok"
    assert calls[0][0][calls[0][0].index("--cdp") + 1].endswith("/before")
    assert calls[1][0][calls[1][0].index("--cdp") + 1].endswith("/after")
    assert all(not path.exists() for _, path in calls)


@pytest.mark.parametrize(
    "command",
    ["connect 9222", "install", "upgrade", "--cdp 9222 snapshot", "snapshot --cdp=9222",
     "snapshot --no-pin-tab", "snapshot --session other", "snapshot --provider browserbase",
     "eval 'document.title'"],
)
def test_connection_overrides_and_eval_are_rejected(monkeypatch, config, command):
    discover = MagicMock()
    run = MagicMock()
    monkeypatch.setattr(browser_plugin, "_discover_cdp_url", discover)
    monkeypatch.setattr(browser_plugin.subprocess, "run", run)
    result = browser_plugin.run_browser_command({"command": command}, config)
    assert result.startswith(("错误", "[安全审查]"))
    discover.assert_not_called()
    run.assert_not_called()


def test_discovery_consumes_shared_timeout_budget(monkeypatch, config):
    config["timeout"] = 10
    discover = MagicMock(return_value="ws://127.0.0.1:9222/devtools/browser/abc")
    monkeypatch.setattr(browser_plugin, "_discover_cdp_url", discover)
    monkeypatch.setattr(browser_plugin.time, "monotonic", MagicMock(side_effect=[100, 107.5]))
    run = MagicMock(return_value=SimpleNamespace(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(browser_plugin.subprocess, "run", run)
    assert browser_plugin.run_browser_command({"command": "snapshot"}, config) == "ok"
    discover.assert_called_once_with(DISCOVERY, 8.0)
    assert run.call_args.kwargs["timeout"] == 2.5


def test_cli_failure_is_not_retried(monkeypatch, config):
    monkeypatch.setattr(browser_plugin, "_discover_cdp_url", lambda *_: "ws://127.0.0.1:9222/devtools/browser/a")
    run = MagicMock(return_value=SimpleNamespace(returncode=1, stdout="", stderr="connection closed"))
    monkeypatch.setattr(browser_plugin.subprocess, "run", run)
    assert "connection closed" in browser_plugin.run_browser_command({"command": "click @e1"}, config)
    run.assert_called_once()
    assert "--cdp" in run.call_args.args[0]


def test_missing_cli_message_does_not_request_chrome_install(monkeypatch, config):
    monkeypatch.setattr(browser_plugin, "_discover_cdp_url", lambda *_: "ws://127.0.0.1:9222/devtools/browser/a")
    monkeypatch.setattr(browser_plugin.subprocess, "run", MagicMock(side_effect=FileNotFoundError))
    result = browser_plugin.run_browser_command({"command": "snapshot"}, config)
    assert "无需安装本地 Chrome" in result
    assert "--with-deps" not in result


def test_timeout_is_reported_without_retry(monkeypatch, config):
    monkeypatch.setattr(browser_plugin, "_discover_cdp_url", lambda *_: "ws://127.0.0.1:9222/devtools/browser/a")
    run = MagicMock(side_effect=subprocess.TimeoutExpired("agent-browser", 20))
    monkeypatch.setattr(browser_plugin.subprocess, "run", run)
    assert "超时" in browser_plugin.run_browser_command({"command": "snapshot"}, config)
    run.assert_called_once()


async def test_bundled_browser_plugin_loads_via_plugin_manager(tmp_path):
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    bundled = Path(__file__).resolve().parents[1] / "plugins" / "bundled"
    pm = PluginManager(
        plugins_dir=tmp_path / "user-plugins",
        bundled_dir=bundled,
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )
    try:
        states = await pm.startup({
            "plugins": {"items": {"browser": {"enabled": True, "trusted": True, "config": {"neko_cdp_port": 9222}}}, "mcp": {}}
        })
        assert states["browser"].status == "active"
        assert states["browser"].bundled is True
        assert states["browser"].manifest.prompt_file is None
        assert states["browser"].has_prompt is False
        assert "prompts:append" not in states["browser"].manifest.permissions
        for legacy in ("chrome_path", "headed", "profile", "args", "cdp_discovery_url", "cdp_discovery_timeout", "neko_enabled"):
            assert legacy not in states["browser"].manifest.config_schema
        assert reg.has("p_browser__browser")
        assert reg.resolve_tool_names(["send_message"], role="chat") == ["send_message", "p_browser__browser"]
    finally:
        await pm.shutdown()
    assert not reg.has("p_browser__browser")
