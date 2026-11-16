"""Phase 2 MCP 客户端测试：真实 stdio 子进程、环境变量隔离、内存通道全链路、失败降级与热重连。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.message import SessionMessage
import pytest

from agent.commands import CommandRegistry
from plugins.manager import PluginManager
from plugins.mcp_client import MCPConfigError, parse_mcp_config, resolve_mcp_secrets
from runtime.tool_registry import ToolRegistry


def _fresh_registry() -> ToolRegistry:
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    return reg


def test_parse_mcp_config_rejects_inline_secrets_and_invalid_transport():
    with pytest.raises(MCPConfigError, match="环境变量名"):
        parse_mcp_config("bad", {
            "transport": "stdio",
            "command": "python",
            "env_secrets": {"API_KEY": "sk-not-an-env-var"},
        })

    with pytest.raises(MCPConfigError, match="stdio 或 streamable_http"):
        parse_mcp_config("bad2", {"transport": "sse", "url": "https://example.com"})

    cfg = parse_mcp_config("remote", {
        "enabled": True,
        "transport": "streamable_http",
        "url": "https://mcp.example.invalid/mcp",
        "auth_bearer_env": "REMOTE_MCP_TOKEN",
        "header_secrets": {"X-Org": "REMOTE_ORG_ID"},
        "headers": {"X-Static": "1"},
    })
    assert cfg.referenced_env_vars() == ["REMOTE_ORG_ID", "REMOTE_MCP_TOKEN"]


def test_resolve_mcp_secrets_reports_missing_and_builds_headers(monkeypatch):
    monkeypatch.setenv("REMOTE_MCP_TOKEN", "bearer-xyz-123")
    monkeypatch.delenv("REMOTE_ORG_ID", raising=False)
    cfg = parse_mcp_config("remote", {
        "enabled": True,
        "transport": "streamable_http",
        "url": "https://mcp.example.invalid/mcp",
        "auth_bearer_env": "REMOTE_MCP_TOKEN",
        "header_secrets": {"X-Org": "REMOTE_ORG_ID"},
        "headers": {"X-Client": "tentacle"},
    })
    _, headers, all_vars, missing = resolve_mcp_secrets(cfg)
    assert headers == {"X-Client": "tentacle", "Authorization": "Bearer bearer-xyz-123"}
    assert all_vars == ["REMOTE_ORG_ID", "REMOTE_MCP_TOKEN"]
    assert missing == ["REMOTE_ORG_ID"]


async def test_real_stdio_mcp_server_isolates_host_secrets_and_degrades_failed_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_MASTER_API_KEY", "host-secret-must-not-leak")
    monkeypatch.setenv("MCP_DEMO_SECRET", "allowed-mcp-secret")

    server_script = tmp_path / "echo_mcp_server.py"
    server_script.write_text(
        """
import asyncio
import json
import os
from mcp import types
from mcp.server.lowlevel import Server
import mcp.server.stdio

async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=[
        types.Tool(
            name="inspect_env",
            description="Inspect visible env keys",
            inputSchema={"type": "object", "properties": {"msg": {"type": "string"}}},
        ),
        types.Tool(
            name="blocked_tool",
            description="Should be disabled",
            inputSchema={"type": "object", "properties": {}},
        ),
    ])

async def on_call_tool(ctx, params):
    args = params.arguments or {}
    payload = {
        "msg": args.get("msg", ""),
        "has_host_secret": "HOST_MASTER_API_KEY" in os.environ,
        "passed_secret": os.environ.get("CHILD_API_KEY"),
    }
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))])

app = Server("demo-stdio", on_list_tools=on_list_tools, on_call_tool=on_call_tool)

async def main():
    async with mcp.server.stdio.stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())

asyncio.run(main())
""".strip(),
        encoding="utf-8",
    )

    reg = _fresh_registry()
    pm = PluginManager(
        plugins_dir=tmp_path / "plugins",
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
    )
    raw_cfg = {
        "plugins": {
            "items": {},
            "mcp": {
                "broken_srv": {
                    "enabled": True,
                    "trusted": True,
                    "transport": "stdio",
                    "command": "nonexistent-mcp-binary-xyz-99999",
                    "timeout_seconds": 3,
                },
                "demo_srv": {
                    "enabled": True,
                    "trusted": True,
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(server_script)],
                    "env_secrets": {"CHILD_API_KEY": "MCP_DEMO_SECRET"},
                    "disabled_tools": ["blocked_tool"],
                    "timeout_seconds": 15,
                },
            },
        }
    }

    try:
        await pm.startup(raw_cfg)
        assert pm.mcp_states["broken_srv"].status == "degraded"
        assert pm.mcp_states["broken_srv"].error
        assert pm.mcp_states["demo_srv"].status == "active"

        assert reg.has("mcp_demo_srv__inspect_env")
        assert not reg.has("mcp_demo_srv__blocked_tool")
        assert len(pm.mcp_states["demo_srv"].tool_records) == 2

        raw_out = await reg.execute("mcp_demo_srv__inspect_env", {"msg": "hello"})
        parsed = json.loads(raw_out)
        assert parsed["msg"] == "hello"
        assert parsed["has_host_secret"] is False
        assert parsed["passed_secret"] == "allowed-mcp-secret"
    finally:
        await pm.shutdown()
    assert reg._tools == {}


async def test_mcp_multimodal_error_handling_and_runtime_reconnect(tmp_path):
    version_box = {"v": 1}

    @contextlib.asynccontextmanager
    async def in_memory_factory(cfg, extra_env, extra_headers):
        current_v = version_box["v"]

        async def on_list_tools(ctx, params):
            tools = [
                types.Tool(
                    name="paint",
                    description="Returns image + text",
                    inputSchema={"type": "object", "properties": {}},
                ),
                types.Tool(
                    name="fail_tool",
                    description="Always errors",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ]
            if current_v >= 2:
                tools.append(types.Tool(
                    name="extra_v2",
                    description="Added after reconnect",
                    inputSchema={"type": "object", "properties": {}},
                ))
            return types.ListToolsResult(tools=tools)

        async def on_call_tool(ctx, params):
            if params.name == "fail_tool":
                raise ValueError("synthetic server error")
            return types.CallToolResult(content=[
                types.TextContent(type="text", text=f"painted-v{current_v}"),
                types.ImageContent(type="image", data="YWJj", mimeType="image/png"),
            ])

        server = Server(f"mem-{cfg.id}", on_list_tools=on_list_tools, on_call_tool=on_call_tool)

        c2s_send, c2s_recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)
        s2c_send, s2c_recv = anyio.create_memory_object_stream[SessionMessage | Exception](16)

        task = asyncio.create_task(
            server.run(c2s_recv, s2c_send, server.create_initialization_options())
        )
        try:
            yield s2c_recv, c2s_send
        finally:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            await c2s_send.aclose()
            await s2c_send.aclose()

    reg = _fresh_registry()
    pm = PluginManager(
        plugins_dir=tmp_path / "plugins",
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        mcp_transport_factory=in_memory_factory,
    )
    cfg = {
        "plugins": {
            "items": {},
            "mcp": {
                "canvas": {
                    "enabled": True,
                    "trusted": True,
                    "transport": "streamable_http",
                    "url": "https://mock.local/mcp",
                }
            },
        }
    }
    try:
        await pm.startup(cfg)
        assert reg.has("mcp_canvas__paint")
        assert not reg.has("mcp_canvas__extra_v2")

        res = await reg.execute("mcp_canvas__paint", {})
        assert isinstance(res, list)
        assert res[0] == {"type": "input_text", "text": "painted-v1"}
        assert res[1]["type"] == "input_image"
        assert res[1]["image_url"] == "data:image/png;base64,YWJj"

        err_res = json.loads(await reg.execute("mcp_canvas__fail_tool", {}))
        assert "synthetic server error" in err_res["error"]

        # 触发热重连加载新版本工具
        version_box["v"] = 2
        state = await pm.reconnect_mcp("canvas", cfg)
        assert state is not None and state.status == "active"
        assert reg.has("mcp_canvas__extra_v2")
    finally:
        await pm.shutdown()
    assert reg._tools == {}
