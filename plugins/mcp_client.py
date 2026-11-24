"""标准 MCP 客户端（stdio + Streamable HTTP），按单服务隔离连接并托管到 ToolRegistry。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from copy import deepcopy
from dataclasses import dataclass, field
import json
import logging
import os
from typing import TYPE_CHECKING, Any, Callable

from plugins.manifest import ENV_NAME_RE, PLUGIN_ID_RE
from plugins.permissions import PluginManifestError

if TYPE_CHECKING:
    from mcp import ClientSession, types


logger = logging.getLogger(__name__)


class MCPConfigError(PluginManifestError):
    """MCP 服务器配置不合法。"""


@dataclass(frozen=True)
class MCPServerConfig:
    """单个 MCP 服务器的配置结构（来自 config.yaml::plugins.mcp.<id>）。"""

    id: str
    enabled: bool = False
    trusted: bool = False
    transport: str = "stdio"  # stdio | streamable_http
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_secrets: dict[str, str] = field(default_factory=dict)  # TARGET_VAR -> HOST_ENV_NAME
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    header_secrets: dict[str, str] = field(default_factory=dict)  # HEADER -> HOST_ENV_NAME
    auth_bearer_env: str | None = None
    timeout_seconds: float = 30.0
    disabled_tools: tuple[str, ...] = ()

    def referenced_env_vars(self) -> list[str]:
        seen: list[str] = []
        for host_env in [*self.env_secrets.values(), *self.header_secrets.values()]:
            if host_env not in seen:
                seen.append(host_env)
        if self.auth_bearer_env and self.auth_bearer_env not in seen:
            seen.append(self.auth_bearer_env)
        return seen


def _normalize_secret_mapping(raw: Any, field_name: str) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, list):
        mapping = {}
        for item in raw:
            if not isinstance(item, str) or not ENV_NAME_RE.fullmatch(item.strip()):
                raise MCPConfigError(f"{field_name} 中的环境变量名无效: {item!r}")
            cleaned = item.strip()
            mapping[cleaned] = cleaned
        return mapping
    if not isinstance(raw, dict):
        raise MCPConfigError(f"{field_name} 必须为映射对象或环境变量名列表")
    mapping = {}
    for target, source in raw.items():
        if not isinstance(target, str) or not target.strip():
            raise MCPConfigError(f"{field_name} 键名不能为空")
        if not isinstance(source, str) or not ENV_NAME_RE.fullmatch(source.strip()):
            raise MCPConfigError(f"{field_name}.{target} 必须填写 .env 环境变量名，不能直接填写密钥")
        mapping[target.strip()] = source.strip()
    return mapping


def parse_mcp_config(server_id: str, raw: object) -> MCPServerConfig:
    """校验并解析 `plugins.mcp.<server_id>` 配置。"""
    if not isinstance(server_id, str) or not PLUGIN_ID_RE.fullmatch(server_id):
        raise MCPConfigError(f"MCP 服务 ID 无效: {server_id!r}")
    if not isinstance(raw, dict):
        raise MCPConfigError(f"plugins.mcp.{server_id} 必须为对象")

    transport = str(raw.get("transport") or "stdio").strip().lower()
    if transport not in {"stdio", "streamable_http"}:
        raise MCPConfigError(f"plugins.mcp.{server_id}.transport 必须为 stdio 或 streamable_http")

    enabled = bool(raw.get("enabled", False))
    trusted = bool(raw.get("trusted", False))
    timeout = float(raw.get("timeout_seconds", 30.0))
    if not 0.5 <= timeout <= 600:
        raise MCPConfigError(f"plugins.mcp.{server_id}.timeout_seconds 必须在 0.5 到 600 之间")

    disabled_raw = raw.get("disabled_tools") or []
    if not isinstance(disabled_raw, list) or not all(isinstance(x, str) for x in disabled_raw):
        raise MCPConfigError(f"plugins.mcp.{server_id}.disabled_tools 必须为字符串列表")

    command = str(raw.get("command") or "").strip()
    args_raw = raw.get("args") or []
    if not isinstance(args_raw, list) or not all(isinstance(x, (str, int, float)) for x in args_raw):
        raise MCPConfigError(f"plugins.mcp.{server_id}.args 必须为字符串数组")
    args = tuple(str(x) for x in args_raw)
    cwd = raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
        raise MCPConfigError(f"plugins.mcp.{server_id}.cwd 必须为有效路径或 null")

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise MCPConfigError(f"plugins.mcp.{server_id}.env 必须为键值对对象")
    env = {str(k): str(v) for k, v in env_raw.items()}
    env_secrets = _normalize_secret_mapping(raw.get("env_secrets"), f"plugins.mcp.{server_id}.env_secrets")

    url = str(raw.get("url") or "").strip()
    headers_raw = raw.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise MCPConfigError(f"plugins.mcp.{server_id}.headers 必须为键值对对象")
    headers = {str(k): str(v) for k, v in headers_raw.items()}
    header_secrets = _normalize_secret_mapping(raw.get("header_secrets"), f"plugins.mcp.{server_id}.header_secrets")
    auth_bearer_env = raw.get("auth_bearer_env")
    if auth_bearer_env is not None:
        if not isinstance(auth_bearer_env, str) or not auth_bearer_env.strip():
            auth_bearer_env = None
        else:
            auth_bearer_env = auth_bearer_env.strip()
            if not ENV_NAME_RE.fullmatch(auth_bearer_env):
                raise MCPConfigError(f"plugins.mcp.{server_id}.auth_bearer_env 必须为环境变量名")

    if transport == "stdio":
        if not command:
            raise MCPConfigError(f"plugins.mcp.{server_id}（stdio）需要填写 command")
    else:
        if not url.startswith(("http://", "https://")):
            raise MCPConfigError(f"plugins.mcp.{server_id}（streamable_http）需要有效的 http/https url")

    return MCPServerConfig(
        id=server_id,
        enabled=enabled,
        trusted=trusted,
        transport=transport,
        command=command,
        args=args,
        cwd=cwd.strip() if isinstance(cwd, str) else None,
        env=env,
        env_secrets=env_secrets,
        url=url,
        headers=headers,
        header_secrets=header_secrets,
        auth_bearer_env=auth_bearer_env,
        timeout_seconds=timeout,
        disabled_tools=tuple(disabled_raw),
    )


def resolve_mcp_secrets(config: MCPServerConfig) -> tuple[dict[str, str], dict[str, str], list[str], list[str]]:
    """从宿主环境按变量名解析 MCP 所需的 env / headers，仅返回缺失变量名列表。"""
    missing: list[str] = []
    all_vars = config.referenced_env_vars()
    env = dict(config.env)
    for target_var, host_var in config.env_secrets.items():
        val = os.getenv(host_var)
        if not val:
            if host_var not in missing:
                missing.append(host_var)
        else:
            env[target_var] = val

    headers = dict(config.headers)
    for header_name, host_var in config.header_secrets.items():
        val = os.getenv(host_var)
        if not val:
            if host_var not in missing:
                missing.append(host_var)
        else:
            headers[header_name] = val

    if config.auth_bearer_env:
        token = os.getenv(config.auth_bearer_env)
        if not token:
            if config.auth_bearer_env not in missing:
                missing.append(config.auth_bearer_env)
        else:
            headers["Authorization"] = f"Bearer {token}"

    return env, headers, all_vars, missing


def _normalize_tool_schema(raw_schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw_schema, dict):
        return {"type": "object", "properties": {}}
    schema = deepcopy(raw_schema)
    if schema.get("type") != "object":
        schema["type"] = "object"
    schema.setdefault("properties", {})
    return schema


def format_call_result(result: types.CallToolResult) -> str | list[dict[str, Any]] | dict[str, Any]:
    """将 MCP CallToolResult 转为 Tentacle 工具返回格式（支持纯文本与多模态图片块）。"""
    texts: list[str] = []
    blocks: list[dict[str, Any]] = []
    has_image = False

    for item in getattr(result, "content", None) or []:
        kind = getattr(item, "type", None)
        if kind == "text":
            text = str(getattr(item, "text", "") or "")
            if text:
                texts.append(text)
                blocks.append({"type": "input_text", "text": text})
        elif kind == "image":
            data = getattr(item, "data", None)
            mime = getattr(item, "mimeType", None) or "image/png"
            if data:
                has_image = True
                blocks.append({
                    "type": "input_image",
                    "image_url": f"data:{mime};base64,{data}",
                    "mime": mime,
                })
        elif kind == "resource":
            res = getattr(item, "resource", None)
            res_text = getattr(res, "text", None)
            res_uri = getattr(res, "uri", None)
            rendered = res_text if res_text is not None else f"[资源: {res_uri}]"
            texts.append(str(rendered))
            blocks.append({"type": "input_text", "text": str(rendered)})
        else:
            summary = f"[MCP 内容: {kind}]"
            texts.append(summary)
            blocks.append({"type": "input_text", "text": summary})

    structured = getattr(result, "structuredContent", None)
    if getattr(result, "isError", False):
        err_text = "\n".join(texts).strip()
        if not err_text and structured is not None:
            err_text = json.dumps(structured, ensure_ascii=False)
        return {"error": err_text or "MCP 工具返回错误"}

    if has_image:
        return blocks
    if texts:
        return "\n".join(texts)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return ""


def _redact_secrets(text: str, env_vars: list[str] | tuple[str, ...]) -> str:
    cleaned = text
    for key in env_vars:
        val = os.getenv(key)
        if val and len(val) >= 4:
            cleaned = cleaned.replace(val, "***")
    return cleaned


class MCPConnection:
    """管理单个 MCP 服务器的独立 asyncio 生命周期任务与会话调用。"""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        extra_env: dict[str, str],
        extra_headers: dict[str, str],
        transport_factory: Callable[[MCPServerConfig, dict[str, str], dict[str, str]], Any] | None = None,
    ):
        self.config = config
        self.server_id = config.id
        self._extra_env = extra_env
        self._extra_headers = extra_headers
        self._transport_factory = transport_factory
        self._session: ClientSession | None = None
        self._tools: list[types.Tool] = []
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._startup_error: Exception | None = None

    @property
    def tools(self) -> list[types.Tool]:
        return list(self._tools)

    @property
    def connected(self) -> bool:
        return self._session is not None and self._task is not None and not self._task.done()

    async def connect(self) -> list[types.Tool]:
        ready = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._startup_error = None
        self._task = asyncio.create_task(self._worker(ready, self._stop_event), name=f"mcp-{self.server_id}")
        await ready.wait()
        if self._startup_error is not None:
            err = self._startup_error
            await self.close()
            raise err
        return list(self._tools)

    async def _worker(self, ready: asyncio.Event, stop_event: asyncio.Event) -> None:
        from mcp import ClientSession

        async with AsyncExitStack() as stack:
            try:
                if self._transport_factory is not None:
                    read_stream, write_stream = await stack.enter_async_context(
                        self._transport_factory(self.config, self._extra_env, self._extra_headers)
                    )
                elif self.config.transport == "stdio":
                    from mcp.client.stdio import StdioServerParameters, stdio_client

                    err_stream = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
                    params = StdioServerParameters(
                        command=self.config.command,
                        args=list(self.config.args),
                        env=self._extra_env or None,
                        cwd=self.config.cwd,
                    )
                    read_stream, write_stream = await stack.enter_async_context(
                        stdio_client(params, errlog=err_stream)
                    )
                else:
                    import httpx2
                    from mcp.client.streamable_http import streamable_http_client

                    client = httpx2.AsyncClient(
                        headers=self._extra_headers or None,
                        timeout=self.config.timeout_seconds,
                    )
                    await stack.enter_async_context(client)
                    read_stream, write_stream = await stack.enter_async_context(
                        streamable_http_client(self.config.url, http_client=client)
                    )

                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await asyncio.wait_for(session.initialize(), timeout=self.config.timeout_seconds)
                listed = await asyncio.wait_for(session.list_tools(), timeout=self.config.timeout_seconds)
                self._session = session
                self._tools = list(getattr(listed, "tools", None) or [])
                ready.set()
                await stop_event.wait()
            except Exception as exc:
                self._startup_error = exc
                ready.set()
            finally:
                self._session = None

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None):
        if not self.connected or self._session is None:
            return {"error": f"MCP 服务 {self.server_id} 未连接或已断开"}
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments or {}),
                timeout=self.config.timeout_seconds,
            )
            return format_call_result(result)
        except Exception as exc:
            msg = _redact_secrets(f"{type(exc).__name__}: {exc}", self.config.referenced_env_vars())
            logger.error(f"MCP 工具调用失败 [{self.server_id}/{name}]: {msg}", exc_info=True)
            return {"error": f"MCP 工具调用失败: {msg}"}

    async def close(self) -> None:
        self._session = None
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
                with suppress(BaseException):
                    await self._task
            self._task = None
