"""Tentacle 内置浏览器插件（封装 agent-browser CLI）。"""

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
_MAX_OUTPUT = 50000
_BLOCKED_SUBCOMMANDS = {"eval"}
_REMOTE_CONNECTION_FLAGS = {
    "--cdp", "--auto-connect", "--profile", "--headed", "--executable-path",
    "--args", "--provider", "-p", "--session", "--config", "--namespace",
    "--pin-tab", "--no-pin-tab", "--restore", "--session-name",
}


def _discover_cdp_url(discovery_url: str, timeout: float) -> str:
    """每次读取浏览器端点，按发现地址改写 host/port，保留 WebSocket 路径。"""
    endpoint = urlsplit(discovery_url)
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
    ):
        raise ValueError("cdp_discovery_url 必须是无用户名/密码的 HTTP(S) 发现地址")
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


def _run_cli(cmd: list[str], env: dict, timeout: float, remote: bool) -> subprocess.CompletedProcess[str]:
    """远程模式隔离 CLI 的用户/项目配置，避免残留 provider/profile 影响连接。"""
    if not remote:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
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


def _skills_dir(config: dict) -> str:
    explicit = (config.get("skills_dir") or "").strip()
    if explicit:
        return explicit
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
    """同步执行一条 agent-browser 命令并返回文本输出或截图内部标记。"""
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

    discovery_url = (cfg.get("cdp_discovery_url") or "").strip()
    remote = bool(discovery_url)
    if remote and (
        words[0].startswith("-")
        or words[0].lower() in {"connect", "install", "upgrade"}
        or any(word.split("=", 1)[0] in _REMOTE_CONNECTION_FLAGS for word in words)
    ):
        return "错误：远程模式的连接与会话由 browser 插件配置管理，不能通过命令覆盖或安装本地浏览器。"

    allow_eval = bool(cfg.get("allow_eval", False))
    reason = _blocked_reason(command, allow_eval)
    if reason:
        logger.warning(f"[安全审查] 拒绝执行: {command}")
        return reason

    executable = _find_executable(cfg)
    session = (cfg.get("session") or "tentacle").strip()
    max_output = cfg.get("max_output", 15000)
    timeout = float(cfg.get("timeout", 60) or 60)
    allowed_domains = cfg.get("allowed_domains", []) or []
    extra_args = cfg.get("args", []) or []
    chrome_path = os.path.expanduser((cfg.get("chrome_path") or "").strip())
    headed = bool(cfg.get("headed", False))
    profile = os.path.expanduser((cfg.get("profile") or "").strip())

    started = time.monotonic()
    cdp_url = ""
    if remote:
        try:
            discovery_timeout = float(cfg.get("cdp_discovery_timeout", 8) or 8)
            if discovery_timeout <= 0 or timeout <= 0:
                raise ValueError("浏览器超时必须大于零")
            cdp_url = _discover_cdp_url(discovery_url, min(timeout, discovery_timeout))
        except Exception as exc:
            logger.warning(f"[browser] 远程浏览器发现失败: {exc}")
            return "错误：远程浏览器不可用，请确认 PC、Chrome、Tailscale 和来源白名单；未启动本地浏览器。"

    cmd = [executable, "--session", session, "--max-output", str(max_output)]
    if remote:
        cmd += ["--cdp", cdp_url, "--pin-tab"]
    else:
        if headed:
            cmd.append("--headed")
        if profile:
            cmd += ["--profile", profile]
        for a in extra_args:
            cmd += ["--args", str(a)]
    if allowed_domains:
        cmd += ["--allowed-domains", ",".join(str(d) for d in allowed_domains)]
    cmd += words

    env = os.environ.copy()
    if remote:
        env = {key: value for key, value in env.items() if not key.startswith("AGENT_BROWSER_")}
    elif chrome_path:
        env["AGENT_BROWSER_EXECUTABLE_PATH"] = chrome_path
    skills_dir = _skills_dir(cfg)
    if os.path.isdir(skills_dir):
        env["AGENT_BROWSER_SKILLS_DIR"] = skills_dir

    logger.info(f"[browser] 执行: {' '.join(cmd)} (timeout={timeout}s)")

    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return "[状态: 超时] 浏览器发现耗尽命令时限，未执行操作。"
        proc = _run_cli(cmd, env, remaining, remote)
    except FileNotFoundError:
        install_hint = (
            "  npm i -g agent-browser（远程模式无需安装 Chrome）\n"
            if remote else "  npm i -g agent-browser && agent-browser install --with-deps\n"
        )
        return (
            "错误：未找到 agent-browser 命令。请先安装：\n"
            + install_hint
            + "或在 WebUI /plugins 中为 browser 插件指定 executable 二进制路径。"
        )
    except subprocess.TimeoutExpired:
        return (
            f"[状态: 超时] 命令未在 {int(timeout)}s 内完成，已终止。"
            "浏览器 daemon 仍在运行，可重试该命令或先 snapshot 查看当前状态。"
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
    """注册 browser 插件工具（`p_browser__browser`）。"""
    plugin_cfg = ctx.config

    async def browser_tool(args: dict) -> str | list[dict]:
        return await execute_browser(args, plugin_cfg, upload_image_fn=ctx.files.upload_image)

    ctx.register_tool(
        name="browser",
        description=(
            "执行 agent-browser 子命令操作真实浏览器（访问网页、表单、点击等）；调用 p_browser__browser(\"skills get core\") 获取完整手册。"
            "eval（页面 JS）默认禁用；远程模式自动发现 CDP 地址，勿自行 connect 或覆盖连接参数；标签页关闭时用 tab new 新建。"
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
