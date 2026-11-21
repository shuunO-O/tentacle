"""Tentacle 内置浏览器插件（纯 Docker Neko + agent-browser CLI）。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit, urlunsplit


logger = logging.getLogger(__name__)

_DEFAULT_SKILLS_DIR = Path.home() / ".local" / "share" / "agent-browser" / "skills"
_DEFAULT_SESSION = "tentacle"
_DEFAULT_NEKO_CDP_PORT = 9222
_CDP_DISCOVERY_TIMEOUT = 8.0
_MAX_OUTPUT = 50000
_BLOCKED_SUBCOMMANDS = {"eval"}
_CONNECTION_OVERRIDE_FLAGS = {
    "--cdp", "--auto-connect", "--profile", "--headed", "--executable-path",
    "--args", "--provider", "-p", "--session", "--config", "--namespace",
    "--pin-tab", "--no-pin-tab", "--restore", "--session-name",
}


def normalize_live_view_url(raw_url: str) -> str:
    """校验可视化浏览器地址（如 Neko），仅允许不含凭据的 http(s) URL。"""
    candidate = str(raw_url or "").strip()
    if not candidate:
        return ""
    if any(ch in candidate for ch in ("\r", "\n", "\x00")):
        raise ValueError("live_view_url 包含非法控制字符")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("live_view_url 必须是无用户名/密码的 http:// 或 https:// 地址")
    return candidate


def _neko_cdp_port(cfg: dict) -> int:
    raw = cfg.get("neko_cdp_port", _DEFAULT_NEKO_CDP_PORT)
    try:
        port = int(raw if raw not in (None, "") else _DEFAULT_NEKO_CDP_PORT)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"neko_cdp_port 必须为 1-65535 的整数: {raw!r}") from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"neko_cdp_port 超出范围: {port}")
    return port


def _neko_cdp_discovery_url(cfg: dict) -> str:
    return f"http://127.0.0.1:{_neko_cdp_port(cfg)}/json/version"


def _probe_cdp_info(discovery_url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """轻量探测本机 Neko CDP /json/version 可达性与浏览器版本，绝不返回 webSocketDebuggerUrl。"""
    if not discovery_url:
        return False, ""
    try:
        endpoint = urlsplit(discovery_url)
        if (
            endpoint.scheme != "http"
            or endpoint.hostname != "127.0.0.1"
            or endpoint.username is not None
            or endpoint.password is not None
        ):
            return False, ""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(discovery_url, timeout=timeout) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            return False, ""
        data = json.loads(payload)
        if not isinstance(data, dict) or not data.get("webSocketDebuggerUrl"):
            return False, ""
        return True, str(data.get("Browser") or "").strip()
    except Exception:
        return False, ""


def _load_browser_neko_module():
    from scripts import browser_neko

    return browser_neko


def _latest_browser_config(fallback_cfg: dict) -> dict:
    """合并已生效配置与用户 config.yaml 中最新保存的 browser 插件覆盖项（不含默认值，避免覆盖运行时注入配置）。"""
    merged = dict(fallback_cfg or {})
    try:
        from config.files import parse_config
        from config.store import ConfigStore

        store = ConfigStore()
        if store.path.is_file():
            raw = parse_config(store.path.read_text(encoding="utf-8"))
            item = ((raw.get("plugins") or {}).get("items") or {}).get("browser")
            if isinstance(item, dict) and isinstance(item.get("config"), dict):
                merged.update(item["config"])
    except Exception:
        pass
    return merged


def _discover_cdp_url(discovery_url: str, timeout: float) -> str:
    """每次读取本机 Neko Chromium CDP 端点，按 127.0.0.1:<port> 改写 authority，保留 WebSocket 路径。"""
    endpoint = urlsplit(discovery_url)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname != "127.0.0.1"
        or endpoint.username is not None
        or endpoint.password is not None
    ):
        raise ValueError("Neko CDP 发现地址必须位于本机 127.0.0.1")
    endpoint.port
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(discovery_url, timeout=timeout) as response:
        payload = response.read(65537)
    if len(payload) > 65536:
        raise ValueError("浏览器发现响应超过 64 KiB")
    data = json.loads(payload)
    ws_url = data.get("webSocketDebuggerUrl") if isinstance(data, dict) else None
    if not isinstance(ws_url, str) or not ws_url:
        raise ValueError("浏览器发现响应缺少 webSocketDebuggerUrl")
    ws = urlsplit(ws_url)
    if ws.scheme not in {"ws", "wss"} or not ws.hostname or not ws.path:
        raise ValueError("webSocketDebuggerUrl 不是有效的 WebSocket 地址")
    return urlunsplit((ws.scheme, endpoint.netloc, ws.path, ws.query, ws.fragment))


def _run_cli(cmd: list[str], env: dict, timeout: float) -> subprocess.CompletedProcess[str]:
    """隔离 agent-browser 的用户/项目配置，避免残留 provider/profile 影响 Neko CDP 连接。"""
    with tempfile.TemporaryDirectory(prefix="tentacle-browser-") as directory:
        config_path = Path(directory) / "agent-browser.json"
        config_path.write_text("{}", encoding="utf-8")
        return subprocess.run(
            [cmd[0], "--config", str(config_path), *cmd[1:]],
            capture_output=True, text=True, timeout=timeout, env=env,
        )


def _find_executable(config: dict) -> str:
    explicit = (config.get("executable") or "").strip()
    if explicit:
        return explicit

    found = shutil.which("agent-browser")
    if found:
        return found

    for cand in (
        Path.home() / ".local" / "bin" / "agent-browser",
        Path.home() / ".npm-global" / "bin" / "agent-browser",
    ):
        if cand.exists():
            return str(cand)
    return "agent-browser"


def _skills_dir() -> str:
    env = os.environ.get("AGENT_BROWSER_SKILLS_DIR", "").strip()
    if env:
        return env
    return str(_DEFAULT_SKILLS_DIR)


def _blocked_reason(command: str, allow_eval: bool) -> str | None:
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.strip().split()
    if not words:
        return None
    sub = words[0].lower()
    if sub in _BLOCKED_SUBCOMMANDS and not allow_eval:
        return (
            f"[安全审查] 子命令 '{sub}' 已被禁用（可在插件 browser 配置开启 allow_eval=true）。"
            "执行页面 JS 有风险，请改用其他方式或改为手动操作。"
        )
    return None


def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    tail = text[-int(limit * 0.4):]
    return f"{head}\n...[输出过长，已截断，共 {len(text)} 字符]...\n{tail}"


def run_browser_command(args: dict, cfg: dict | None = None) -> str | dict:
    """同步执行一条 agent-browser 命令（直连本机 Docker Neko CDP）并返回文本输出或截图内部标记。"""
    command = (args.get("command") or "").strip()
    if not command:
        return "错误：command 不能为空。例如 'open https://example.com'、'snapshot'。"

    screenshot_as_image = bool(args.get("screenshot_as_image", False))
    cfg = cfg if isinstance(cfg, dict) else {}

    try:
        words = shlex.split(command)
    except ValueError as exc:
        return f"错误：命令格式无法解析：{exc}"
    if not words:
        return "错误：command 不能为空。"

    if (
        words[0].startswith("-")
        or words[0].lower() in {"connect", "install", "upgrade"}
        or any(word.split("=", 1)[0] in _CONNECTION_OVERRIDE_FLAGS for word in words)
    ):
        return "错误：浏览器连接与会话由 Docker Neko 托管配置管理，不能通过命令覆盖连接参数或安装本地浏览器。"

    allow_eval = bool(cfg.get("allow_eval", False))
    reason = _blocked_reason(command, allow_eval)
    if reason:
        logger.warning(f"[安全审查] 拒绝执行: {command}")
        return reason

    executable = _find_executable(cfg)
    max_output = cfg.get("max_output", 15000)
    timeout = float(cfg.get("timeout", 60) or 60)
    allowed_domains = cfg.get("allowed_domains", []) or []

    started = time.monotonic()
    try:
        if timeout <= 0:
            raise ValueError("浏览器超时必须大于零")
        cdp_port = _neko_cdp_port(cfg)
        discovery_url = f"http://127.0.0.1:{cdp_port}/json/version"
        cdp_url = _discover_cdp_url(discovery_url, min(timeout, _CDP_DISCOVERY_TIMEOUT))
    except Exception as exc:
        logger.warning(f"[browser] Neko CDP 发现失败: {exc}")
        port_hint = cfg.get("neko_cdp_port", _DEFAULT_NEKO_CDP_PORT)
        return (
            f"错误：Docker Neko 浏览器 CDP（127.0.0.1:{port_hint}）不可用，"
            "请在 WebUI /plugins 的 browser 插件页点击「同步并启动」或执行 tentacle browser start。"
        )

    cmd = [
        executable,
        "--session", _DEFAULT_SESSION,
        "--max-output", str(max_output),
        "--cdp", cdp_url,
        "--pin-tab",
    ]
    if allowed_domains:
        cmd += ["--allowed-domains", ",".join(str(d) for d in allowed_domains)]
    cmd += words

    env = {key: value for key, value in os.environ.items() if not key.startswith("AGENT_BROWSER_")}
    skills_dir = _skills_dir()
    if os.path.isdir(skills_dir):
        env["AGENT_BROWSER_SKILLS_DIR"] = skills_dir

    logger.info(f"[browser] 执行: {' '.join(cmd)} (timeout={timeout}s)")

    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return "[状态: 超时] 浏览器发现耗尽命令时限，未执行操作。"
        proc = _run_cli(cmd, env, remaining)
    except FileNotFoundError:
        return (
            "错误：未找到 agent-browser 命令。请先安装：\n"
            "  npm i -g agent-browser（无需安装本地 Chrome，直接使用 Docker Neko）\n"
            "或在 WebUI /plugins 中为 browser 插件指定 executable 二进制路径。"
        )
    except subprocess.TimeoutExpired:
        return (
            f"[状态: 超时] 命令未在 {int(timeout)}s 内完成，已终止。"
            "Docker Neko 浏览器仍在运行，可重试该命令或先 snapshot 查看当前状态。"
        )

    output = proc.stdout or ""
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        detail = stderr or output.strip() or "无输出"
        result = f"[退出码: {proc.returncode}]\n{_truncate(detail)}"
        logger.warning(f"[browser] 命令失败: {' '.join(cmd)} -> {result[:300]}")
        return result

    result = _truncate(output.strip()) or "[无输出]"
    logger.info(f"[browser] 完成: {command} -> {len(result)} 字符")

    if screenshot_as_image and command.split()[0] == "screenshot":
        return {"__browser_shot__": result}

    return result


async def _screenshot_to_image_blocks(output: str, upload_image_fn) -> list[dict]:
    m = re.search(r"Screenshot saved to (.+)", output)
    if not m:
        m = re.search(r"([^\s]+\.png)", output)
    if not m:
        raise ValueError("无法从输出中解析截图路径")
    path = m.group(1).strip()

    with open(path, "rb") as f:
        data = f.read()

    record = await upload_image_fn(data, filename=Path(path).name)
    block = {"type": "image", "local_id": record["local_id"], "mime": record["mime"]}
    if record.get("file_id"):
        block["file_id"] = record["file_id"]
    return [block]


async def execute_browser(args: dict, cfg: dict | None = None, upload_image_fn=None) -> str | list[dict]:
    result = await asyncio.to_thread(run_browser_command, args, cfg)
    if isinstance(result, dict) and "__browser_shot__" in result:
        if upload_image_fn is None:
            return f"{result['__browser_shot__']}\n[截图转图像内容块失败: 缺少文件上传服务]"
        try:
            return await _screenshot_to_image_blocks(result["__browser_shot__"], upload_image_fn)
        except Exception as exc:
            logger.warning(f"[browser] screenshot_as_image 转换失败: {exc}")
            return f"{result['__browser_shot__']}\n[截图转图像内容块失败: {exc}]"
    return result


def setup(ctx) -> None:
    """注册 browser 插件工具（`p_browser__browser`）与 Docker Neko 状态/控制路由。"""
    plugin_cfg = ctx.config

    if ctx.has_permission("web:routes"):
        from fastapi import APIRouter, Body, HTTPException

        router = APIRouter()

        @router.get("/live-view")
        async def get_live_view_status():
            cfg_now = _latest_browser_config(plugin_cfg)
            raw_live = str(cfg_now.get("live_view_url") or "").strip()
            live_url = ""
            url_error: str | None = None
            if raw_live:
                try:
                    live_url = normalize_live_view_url(raw_live)
                except ValueError as exc:
                    url_error = str(exc)

            cdp_reachable = False
            browser_version = ""
            cdp_port = _DEFAULT_NEKO_CDP_PORT
            try:
                cdp_port = _neko_cdp_port(cfg_now)
                cdp_url = f"http://127.0.0.1:{cdp_port}/json/version"
                cdp_reachable, browser_version = await asyncio.to_thread(_probe_cdp_info, cdp_url, 2.0)
            except ValueError:
                pass

            return {
                "configured": bool(live_url),
                "live_view_url": live_url,
                "url_error": url_error,
                "cdp_port": cdp_port,
                "cdp_configured": True,
                "cdp_reachable": cdp_reachable,
                "browser_version": browser_version,
                "mode": "neko",
            }

        @router.get("/neko/status")
        async def get_neko_status():
            cfg_now = _latest_browser_config(plugin_cfg)
            neko_mod = _load_browser_neko_module()
            try:
                return await asyncio.to_thread(neko_mod.control_neko_from_app, "status", cfg_now)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        @router.post("/neko/action")
        async def run_neko_action(body: dict = Body(...)):
            act = str((body or {}).get("action") or "").strip().lower()
            if act not in {"start", "stop", "restart", "proxy_config"}:
                raise HTTPException(status_code=400, detail=f"不支持的 Neko 操作: {act}")
            cfg_now = _latest_browser_config(plugin_cfg)
            neko_mod = _load_browser_neko_module()
            try:
                return await asyncio.to_thread(neko_mod.control_neko_from_app, act, cfg_now)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        ctx.register_router(router)

    async def browser_tool(args: dict) -> str | list[dict]:
        return await execute_browser(args, _latest_browser_config(plugin_cfg), upload_image_fn=ctx.files.upload_image)

    ctx.register_tool(
        name="browser",
        description=(
            "执行 agent-browser 子命令操作本机 Docker Neko 浏览器（访问网页、表单、点击等）；调用 p_browser__browser(\"skills get core\") 获取完整手册。"
            "eval（页面 JS）默认禁用；自动连接 Neko 本机 CDP，勿自行 connect 或覆盖连接参数；标签页关闭时用 tab new 新建。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "完整的 agent-browser 子命令字符串，如 'open https://example.com'、'snapshot'、'click @e3'、'fill @e1 \"hello\"'、'screenshot /tmp/x.png'。",
                },
                "screenshot_as_image": {
                    "type": "boolean",
                    "description": "当 command 是 screenshot 时设为 true，截图自动保存为图片附件，按当前模型接口返回可直接查看的图像。",
                },
            },
            "required": ["command"],
        },
        func=browser_tool,
        is_async=True,
    )
