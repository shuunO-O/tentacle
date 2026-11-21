#!/usr/bin/env python3
"""Tentacle 可视化浏览器（Docker + m1k1o/neko Chromium + 本机 CDP）一键安装与管理工具。

设计原则：
1. 仅使用 Python 标准库，无额外第三方依赖，便于在任意 Linux 主机通过 sudo 直接执行。
2. Chromium 远程调试端口严格绑定宿主机 127.0.0.1（绝不对外暴露）；Neko Web 入口默认仅绑定
   127.0.0.1:8080，便于挂在现有 Nginx/Caddy/Tailscale HTTPS 反代下，也支持通过 --bind 指定地址。
3. 使用 Neko v3 单端口 TCP+UDP MUX（默认 52000），只需放行一个端口即可建立 WebRTC 视频流。
4. 通过覆盖挂载 /etc/neko/supervisord/chromium.conf 直接复用官方预构建镜像，免本地构建，
   同时移除访客模式（--bwsi）并持久化 profile 目录，保留登录态。
5. Neko 凭据仅保存在运行目录 0600 权限的 .env 中，绝不写入 Tentacle 的 config.yaml 或经 WebUI 回显。
6. Linux systemd 生产环境下，通过按需拉起的 root Unix Socket（SO_PEERCRED 校验调用方 UID）
   仅向 Tentacle WebUI 暴露白名单操作（status / start / stop / restart / proxy_config），
   无需将低权限应用账号加入 docker 组，且空闲时零常驻内存开销。
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlsplit


DEFAULT_IMAGE = "ghcr.io/m1k1o/neko/chromium:latest"
DEFAULT_CONTAINER_NAME = "tentacle-browser-neko"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_HTTP_PORT = 8080
DEFAULT_CDP_PORT = 9222
DEFAULT_MUX_PORT = 52000
DEFAULT_SCREEN = "1280x720@24"
DEFAULT_INSTALL_DIR_ROOT = Path("/var/lib/tentacle-browser")
STATE_FILENAME = "neko-install.json"

CONTROL_SOCKET_PATH = Path("/run/tentacle-browser/control.sock")
CONTROL_SOCKET_UNIT = "tentacle-browser-neko.socket"
CONTROL_SERVICE_UNIT = "tentacle-browser-neko@.service"
ALLOWED_CONTROL_ACTIONS = frozenset({"status", "start", "stop", "restart", "proxy_config"})

_SCREEN_RE = re.compile(r"^(\d{3,4})x(\d{3,4})@(\d{1,2})$")
_SHM_RE = re.compile(r"^\d+[mMgG]$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{2,200}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,255}$")
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass
class NekoConfig:
    install_dir: str
    container_name: str = DEFAULT_CONTAINER_NAME
    image: str = DEFAULT_IMAGE
    bind: str = DEFAULT_BIND
    http_port: int = DEFAULT_HTTP_PORT
    cdp_port: int = DEFAULT_CDP_PORT
    mux_port: int = DEFAULT_MUX_PORT
    nat_ip: str = ""
    screen: str = DEFAULT_SCREEN
    shm_size: str = "1g"
    live_view_url: str = ""


def default_install_dir() -> Path:
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and geteuid() == 0:
        return DEFAULT_INSTALL_DIR_ROOT
    return Path.home() / ".local" / "share" / "tentacle-browser"


def validate_port(port: Any, name: str) -> int:
    try:
        num = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 端口必须是整数，收到: {port}") from exc
    if not (1 <= num <= 65535):
        raise ValueError(f"{name} 端口必须在 1-65535 之间，收到: {num}")
    return num


def validate_bind(bind: str) -> str:
    value = str(bind or "").strip()
    if not value:
        raise ValueError("监听地址不能为空")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError as exc:
        raise ValueError(f"监听地址必须是合法 IP（如 127.0.0.1 或 0.0.0.0），收到: {value}") from exc


def validate_screen(screen: str) -> str:
    value = str(screen or "").strip()
    m = _SCREEN_RE.fullmatch(value)
    if not m:
        raise ValueError(f"分辨率格式须为 WxH@FPS（如 1280x720@24），收到: {value}")
    w, h, fps = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (640 <= w <= 3840 and 480 <= h <= 2160 and 5 <= fps <= 60):
        raise ValueError(f"分辨率或帧率超出合理范围: {value}")
    return value


def validate_shm_size(shm_size: str) -> str:
    value = str(shm_size or "").strip().lower()
    if not _SHM_RE.fullmatch(value):
        raise ValueError(f"shm_size 格式无效（例如 512m、1g、2g），收到: {shm_size}")
    return value


def validate_image(image: str) -> str:
    value = str(image or "").strip()
    if not _IMAGE_RE.fullmatch(value):
        raise ValueError(f"Docker 镜像名无效: {image}")
    return value


def validate_container_name(name: str) -> str:
    value = str(name or "").strip()
    if not _CONTAINER_NAME_RE.fullmatch(value):
        raise ValueError(f"容器名称无效: {name}")
    return value


def validate_nat_ip(nat_ip: str) -> str:
    value = str(nat_ip or "").strip()
    if not value:
        return ""
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if not parts:
        return ""
    for part in parts:
        if not _HOST_RE.fullmatch(part) or any(ch.isspace() for ch in part):
            raise ValueError(f"NAT IP 或域名格式无效: {nat_ip}")
    return ",".join(parts)


def normalize_live_view_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return ""
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        raise ValueError("live_view_url 包含非法控制字符")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("live_view_url 必须是完整的 http:// 或 https:// 地址")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("live_view_url 不得内嵌用户名或密码")
    return value


def detect_host_memory_mib() -> tuple[int | None, int | None]:
    """读取 Linux 宿主机物理内存与 Swap 大小（MiB）；非 Linux 返回 (None, None)。"""
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return None, None
    total_kib: int | None = None
    swap_kib: int | None = None
    try:
        for line in meminfo.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemTotal:"):
                total_kib = int(line.split()[1])
            elif line.startswith("SwapTotal:"):
                swap_kib = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None, None
    return (
        total_kib // 1024 if total_kib is not None else None,
        swap_kib // 1024 if swap_kib is not None else None,
    )


def recommend_shm_size(total_mem_mib: int | None) -> str:
    if total_mem_mib is not None and total_mem_mib <= 2600:
        return "512m"
    return "1g"


def config_from_plugin_dict(
    plugin_cfg: dict[str, Any],
    *,
    default_dir: Path | None = None,
) -> NekoConfig:
    """从 Tentacle browser 插件配置字典构造并校验 NekoConfig。"""
    cfg = plugin_cfg if isinstance(plugin_cfg, dict) else {}
    total_mem, _ = detect_host_memory_mib()
    raw_dir = str(cfg.get("neko_dir") or "").strip()
    install_dir = Path(raw_dir).expanduser().resolve() if raw_dir else (default_dir or default_install_dir())
    if len(install_dir.parts) < 2 or install_dir == Path("/"):
        raise ValueError("neko_dir 不能使用系统根目录")

    raw_shm = str(cfg.get("neko_shm_size") or "").strip()
    shm_size = validate_shm_size(raw_shm) if raw_shm else recommend_shm_size(total_mem)
    return NekoConfig(
        install_dir=str(install_dir),
        container_name=validate_container_name(str(cfg.get("neko_container_name") or DEFAULT_CONTAINER_NAME)),
        image=validate_image(str(cfg.get("neko_image") or DEFAULT_IMAGE)),
        bind=validate_bind(str(cfg.get("neko_bind") or DEFAULT_BIND)),
        http_port=validate_port(cfg.get("neko_http_port", DEFAULT_HTTP_PORT), "neko_http_port"),
        cdp_port=validate_port(cfg.get("neko_cdp_port", DEFAULT_CDP_PORT), "neko_cdp_port"),
        mux_port=validate_port(cfg.get("neko_mux_port", DEFAULT_MUX_PORT), "neko_mux_port"),
        nat_ip=validate_nat_ip(str(cfg.get("neko_nat_ip") or "")),
        screen=validate_screen(str(cfg.get("neko_screen") or DEFAULT_SCREEN)),
        shm_size=shm_size,
        live_view_url=normalize_live_view_url(str(cfg.get("live_view_url") or "")),
    )


def render_chromium_policies() -> str:
    """生成覆盖 Neko 镜像默认策略的 policies.json，允许 DevTools / CDP 附加。"""
    policies = {
        "DeveloperToolsAvailability": 1,
        "DefaultBrowserSettingEnabled": False,
        "MetricsReportingEnabled": False,
        "PromotionalTabsEnabled": False,
    }
    return json.dumps(policies, ensure_ascii=False, indent=2) + "\n"


def render_cdp_proxy_script() -> str:
    """生成容器内轻量 TCP 转发脚本（0.0.0.0:9222 -> 127.0.0.1:9223），兼容新版 Chromium 强制回环监听。"""
    return """#!/usr/bin/env python3
import asyncio
import signal


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    upstream_w = None
    try:
        upstream_r, upstream_w = await asyncio.open_connection("127.0.0.1", 9223)
        t1 = asyncio.create_task(_pipe(client_r, upstream_w))
        t2 = asyncio.create_task(_pipe(upstream_r, client_w))
        await asyncio.gather(t1, t2, return_exceptions=True)
    except OSError:
        pass
    finally:
        for w in (upstream_w, client_w):
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass


async def main() -> None:
    server = await asyncio.start_server(_handle, "0.0.0.0", 9222, reuse_address=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    async with server:
        await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
"""


def render_chromium_supervisord_conf() -> str:
    """生成覆盖容器内 /etc/neko/supervisord/chromium.conf 的配置（开启持久化、127.0.0.1:9223 CDP 与 9222 容器转发）。"""
    return """[program:chromium]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s"
command=/bin/sh -c 'rm -f /home/neko/.config/chromium/SingletonLock /home/neko/.config/chromium/SingletonSocket /home/neko/.config/chromium/SingletonCookie && exec "$(command -v chromium || command -v google-chrome || command -v chromium-browser)" --no-sandbox --window-position=0,0 --display=%(ENV_DISPLAY)s --user-data-dir=/home/neko/.config/chromium --no-first-run --no-default-browser-check --start-maximized --disable-gpu --disable-software-rasterizer --disable-dev-shm-usage --remote-debugging-port=9223'
stopsignal=INT
autorestart=true
priority=800
user=%(ENV_USER)s
stdout_logfile=/var/log/neko/chromium.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=3
redirect_stderr=true

[program:cdp-proxy]
command=/usr/bin/python3 -u /etc/neko/cdp-proxy.py
stopsignal=TERM
autorestart=true
priority=750
user=%(ENV_USER)s
stdout_logfile=/var/log/neko/cdp-proxy.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=2
redirect_stderr=true

[program:openbox]
environment=HOME="/home/%(ENV_USER)s",USER="%(ENV_USER)s",DISPLAY="%(ENV_DISPLAY)s"
command=/usr/bin/openbox --config-file /etc/neko/openbox.xml
autorestart=true
priority=300
user=%(ENV_USER)s
stdout_logfile=/var/log/neko/openbox.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=3
redirect_stderr=true
"""


def render_docker_compose(cfg: NekoConfig) -> str:
    """生成受管 docker-compose.yaml（CDP 仅绑定 127.0.0.1，WebRTC 使用单端口 TCP+UDP MUX）。"""
    env_lines = [
        f'      NEKO_DESKTOP_SCREEN: "{cfg.screen}"',
        '      NEKO_SERVER_PROXY: "true"',
        '      NEKO_WEBRTC_ICELITE: "true"',
        f"      NEKO_WEBRTC_UDPMUX: {cfg.mux_port}",
        f"      NEKO_WEBRTC_TCPMUX: {cfg.mux_port}",
    ]
    if cfg.nat_ip:
        env_lines.append(f'      NEKO_WEBRTC_NAT1TO1: "{cfg.nat_ip}"')

    sup_target = (
        "/etc/neko/supervisord/google-chrome.conf"
        if "google-chrome" in cfg.image.lower()
        else "/etc/neko/supervisord/chromium.conf"
    )
    env_block = "\n".join(env_lines)
    return f"""# Managed by Tentacle browser_neko.py
services:
  neko:
    image: "{cfg.image}"
    container_name: "{cfg.container_name}"
    restart: unless-stopped
    shm_size: "{cfg.shm_size}"
    env_file:
      - .env
    ports:
      - "{cfg.bind}:{cfg.http_port}:8080"
      - "127.0.0.1:{cfg.cdp_port}:9222"
      - "{cfg.mux_port}:{cfg.mux_port}/udp"
      - "{cfg.mux_port}:{cfg.mux_port}/tcp"
    volumes:
      - "./profile:/home/neko/.config/chromium"
      - "./chromium.conf:{sup_target}:ro"
      - "./cdp-proxy.py:/etc/neko/cdp-proxy.py:ro"
      - "./policies.json:/etc/chromium/policies/managed/policies.json:ro"
      - "./policies.json:/etc/opt/chrome/policies/managed/policies.json:ro"
    environment:
{env_block}
"""


def render_reverse_proxy_examples(cfg: NekoConfig) -> str:
    """生成适配当前端口的 Nginx / Caddy / Tailscale Serve 反代参考配置。"""
    upstream = f"127.0.0.1:{cfg.http_port}"
    return f"""# Tentacle 可视化浏览器（Neko）HTTPS 反代参考配置
# 说明：
# 1. Neko 网页/信令默认监听 {cfg.bind}:{cfg.http_port}（Neko v3 前端走根路径，请使用独立子域名或独立端口，勿挂 /neko/ 子路径）
# 2. WebRTC 媒体通道使用单端口 TCP+UDP MUX：{cfg.mux_port}（需在云服务器安全组/防火墙放行 {cfg.mux_port}/udp 与 {cfg.mux_port}/tcp）
# 3. CDP 调试端口仅绑定 127.0.0.1:{cfg.cdp_port}，请勿对外反向代理该端口！

# ---------- 方式 1：Caddy（推荐最简；独立子域名或复用域名+:8443） ----------
# browser.your-domain {{
#     reverse_proxy {upstream}
# }}
# 或免改 DNS：
# your-domain:8443 {{
#     reverse_proxy {upstream}
# }}

# ---------- 方式 2：Tailscale Serve（私网自动 HTTPS，无需公网端口与域名） ----------
# sudo tailscale serve --bg --https=8443 http://{upstream}
# 配合在 WebUI /plugins 将 neko_nat_ip 填为该主机的 Tailscale IP（如 100.x.y.z）

# ---------- 方式 3：Nginx 独立子域名反代（例如 https://browser.your-domain） ----------
# server {{
#     listen 443 ssl http2;
#     server_name browser.your-domain;
#     # ssl_certificate ...
#     # ssl_certificate_key ...
#     location / {{
#         proxy_pass http://{upstream};
#         proxy_http_version 1.1;
#         proxy_set_header Upgrade $http_upgrade;
#         proxy_set_header Connection "upgrade";
#         proxy_set_header Host $host;
#         proxy_set_header X-Real-IP $remote_addr;
#         proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
#         proxy_set_header X-Forwarded-Proto $scheme;
#         proxy_read_timeout 86400s;
#         proxy_send_timeout 86400s;
#     }}
# }}
"""


def _atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            pass
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        values[key] = val
    return values


def ensure_env_passwords(env_path: Path, *, rotate: bool = False) -> tuple[dict[str, str], bool]:
    existing = {} if rotate else parse_env_file(env_path)
    user_pw = existing.get("NEKO_MEMBER_MULTIUSER_USER_PASSWORD", "").strip()
    admin_pw = existing.get("NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD", "").strip()
    changed = False
    if not user_pw:
        user_pw = secrets.token_urlsafe(18)
        changed = True
    if not admin_pw:
        admin_pw = secrets.token_urlsafe(24)
        changed = True
    creds = {
        "NEKO_MEMBER_MULTIUSER_USER_PASSWORD": user_pw,
        "NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD": admin_pw,
    }
    if changed or not env_path.is_file():
        content = (
            "# Managed by Tentacle browser_neko.py (chmod 0600)\n"
            f"NEKO_MEMBER_MULTIUSER_USER_PASSWORD={user_pw}\n"
            f"NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD={admin_pw}\n"
        )
        _atomic_write(env_path, content, mode=0o600)
    return creds, changed


def _write_if_changed(path: Path, text: str, mode: int = 0o644) -> bool:
    """仅当文件内容有变化时才原子替换，避免无故更换 bind-mount 文件 inode。"""
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    _atomic_write(path, text, mode=mode)
    return True


def write_install_files(cfg: NekoConfig, *, rotate_passwords: bool = False) -> tuple[dict[str, str], bool, bool]:
    root = Path(cfg.install_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile_dir = root / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    # 容器内默认 neko 用户 UID/GID 为 1000，尽量确保其可写
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and geteuid() == 0:
        try:
            os.chown(profile_dir, 1000, 1000)
        except OSError:
            pass

    creds, rotated = ensure_env_passwords(root / ".env", rotate=rotate_passwords)
    changed = rotated
    for filename, content in (
        ("policies.json", render_chromium_policies()),
        ("cdp-proxy.py", render_cdp_proxy_script()),
        ("chromium.conf", render_chromium_supervisord_conf()),
        ("docker-compose.yaml", render_docker_compose(cfg)),
        ("reverse-proxy.example.conf", render_reverse_proxy_examples(cfg)),
        (STATE_FILENAME, json.dumps(asdict(cfg), ensure_ascii=False, indent=2) + "\n"),
    ):
        if _write_if_changed(root / filename, content, mode=0o644):
            changed = True
    return creds, rotated, changed


def load_saved_config(install_dir: Path) -> NekoConfig | None:
    state_file = install_dir / STATE_FILENAME
    if not state_file.is_file():
        return None
    try:
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return NekoConfig(
            install_dir=str(raw.get("install_dir") or install_dir),
            container_name=str(raw.get("container_name") or DEFAULT_CONTAINER_NAME),
            image=str(raw.get("image") or DEFAULT_IMAGE),
            bind=str(raw.get("bind") or DEFAULT_BIND),
            http_port=int(raw.get("http_port") or DEFAULT_HTTP_PORT),
            cdp_port=int(raw.get("cdp_port") or DEFAULT_CDP_PORT),
            mux_port=int(raw.get("mux_port") or DEFAULT_MUX_PORT),
            nat_ip=str(raw.get("nat_ip") or ""),
            screen=str(raw.get("screen") or DEFAULT_SCREEN),
            shm_size=str(raw.get("shm_size") or "1g"),
            live_view_url=str(raw.get("live_view_url") or ""),
        )
    except (OSError, ValueError, TypeError):
        return None


def discover_tentacle_config_path(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidates: list[Path] = []
    env_home = os.environ.get("TENTACLE_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser() / "config" / "config.yaml")
    update_json = Path("/etc/tentacle/update.json")
    if update_json.is_file():
        try:
            host_cfg = json.loads(update_json.read_text(encoding="utf-8"))
            home_dir = str(host_cfg.get("home") or "").strip() if isinstance(host_cfg, dict) else ""
            if home_dir:
                candidates.append(Path(home_dir) / "config" / "config.yaml")
        except (OSError, ValueError):
            pass
    candidates.extend([
        Path("/home/tentacle/config/config.yaml"),
        Path("/var/lib/tentacle/config/config.yaml"),
        Path.home() / ".tentacle" / "config" / "config.yaml",
    ])
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def read_tentacle_browser_config(config_path: Path | None) -> dict[str, Any]:
    if config_path is None or not config_path.is_file():
        return {}
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return {}
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    plugins = loaded.get("plugins")
    if not isinstance(plugins, dict):
        return {}
    items = plugins.get("items")
    if not isinstance(items, dict):
        return {}
    browser_item = items.get("browser")
    if not isinstance(browser_item, dict):
        return {}
    cfg = browser_item.get("config")
    return dict(cfg) if isinstance(cfg, dict) else {}


def update_tentacle_browser_config(
    config_path: Path,
    *,
    cfg: NekoConfig,
) -> bool:
    """增量更新 Tentacle config.yaml 中的 browser 插件配置（仅写非敏感字段，绝不写密码）。"""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return False

    data: dict[str, Any] = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    plugins_sec = data.setdefault("plugins", {})
    if not isinstance(plugins_sec, dict):
        plugins_sec = {}
        data["plugins"] = plugins_sec
    items = plugins_sec.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        plugins_sec["items"] = items
    browser_item = items.setdefault("browser", {})
    if not isinstance(browser_item, dict):
        browser_item = {}
        items["browser"] = browser_item
    browser_item.setdefault("enabled", True)
    browser_item.setdefault("trusted", True)
    browser_cfg = browser_item.setdefault("config", {})
    if not isinstance(browser_cfg, dict):
        browser_cfg = {}
        browser_item["config"] = browser_cfg

    for legacy_key in (
        "neko_enabled",
        "cdp_discovery_url",
        "cdp_discovery_timeout",
        "chrome_path",
        "headed",
        "profile",
        "args",
        "skills_dir",
        "session",
    ):
        browser_cfg.pop(legacy_key, None)

    browser_cfg["neko_dir"] = cfg.install_dir
    browser_cfg["neko_image"] = cfg.image
    browser_cfg["neko_bind"] = cfg.bind
    browser_cfg["neko_http_port"] = cfg.http_port
    browser_cfg["neko_cdp_port"] = cfg.cdp_port
    browser_cfg["neko_mux_port"] = cfg.mux_port
    browser_cfg["neko_nat_ip"] = cfg.nat_ip
    browser_cfg["neko_screen"] = cfg.screen
    browser_cfg["neko_shm_size"] = cfg.shm_size
    if cfg.live_view_url:
        browser_cfg["live_view_url"] = cfg.live_view_url

    old_stat = config_path.stat() if config_path.exists() else None
    rendered = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    _atomic_write(config_path, rendered, mode=old_stat.st_mode & 0o777 if old_stat else 0o600)
    if old_stat is not None and hasattr(os, "chown"):
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and geteuid() == 0:
            try:
                os.chown(config_path, old_stat.st_uid, old_stat.st_gid)
            except OSError:
                pass
    return True


def detect_compose_command() -> list[str] | None:
    docker_bin = shutil.which("docker")
    if docker_bin:
        try:
            proc = subprocess.run(
                [docker_bin, "compose", "version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                return [docker_bin, "compose"]
        except (OSError, subprocess.SubprocessError):
            pass
    legacy = shutil.which("docker-compose")
    if legacy:
        return [legacy]
    return None


def docker_daemon_accessible() -> bool:
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False
    try:
        proc = subprocess.run([docker_bin, "info"], capture_output=True, text=True, timeout=6)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_docker_engine() -> None:
    if sys.platform != "linux":
        raise RuntimeError("--install-docker 仅支持 Linux 主机，Windows/macOS 请先安装 Docker Desktop。")
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and geteuid() != 0:
        raise RuntimeError("自动安装 Docker 需要 root 权限，请使用 sudo 运行。")
    print("[neko] 正在通过 Docker 官方安装脚本安装 Docker Engine 与 Compose 插件...")
    with tempfile.TemporaryDirectory(prefix="tentacle-docker-install-") as tmp_dir:
        script_path = Path(tmp_dir) / "get-docker.sh"
        urllib.request.urlretrieve("https://get.docker.com", script_path)
        subprocess.run(["/bin/sh", str(script_path)], check=True)
    systemctl = shutil.which("systemctl")
    if systemctl:
        subprocess.run([systemctl, "enable", "--now", "docker"], check=False)


def probe_cdp_endpoint(cdp_port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    url = f"http://127.0.0.1:{cdp_port}/json/version"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            raw = resp.read(65536)
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def wait_for_cdp(cdp_port: int, deadline_seconds: float = 35.0) -> dict[str, Any] | None:
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        info = probe_cdp_endpoint(cdp_port, timeout=1.5)
        if info and info.get("webSocketDebuggerUrl"):
            return info
        time.sleep(1.0)
    return None


def inspect_container_state(container_name: str) -> str:
    """查询容器状态（running / exited / created / missing / unavailable）。"""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return "unavailable"
    try:
        proc = subprocess.run(
            [docker_bin, "inspect", "-f", "{{.State.Status}}", container_name],
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    if proc.returncode != 0:
        err = (proc.stderr or "").lower()
        if "no such" in err:
            return "missing"
        return "unavailable"
    return (proc.stdout or "").strip().lower() or "unknown"


def _run_compose(
    compose_cmd: list[str],
    install_dir: Path,
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = [*compose_cmd, "-f", str(install_dir / "docker-compose.yaml"), *args]
    return subprocess.run(cmd, cwd=str(install_dir), check=check, text=True, capture_output=capture)


def render_control_socket_unit(socket_user: str) -> str:
    return f"""# Managed by Tentacle browser_neko.py
[Unit]
Description=Tentacle Docker Neko Browser Control Socket

[Socket]
ListenStream={CONTROL_SOCKET_PATH}
RuntimeDirectory=tentacle-browser
RuntimeDirectoryMode=0750
SocketUser={socket_user}
SocketGroup={socket_user}
SocketMode=0660
Accept=yes

[Install]
WantedBy=sockets.target
"""


def render_control_service_unit(helper_path: Path, allowed_user: str) -> str:
    return f"""# Managed by Tentacle browser_neko.py
[Unit]
Description=Tentacle Docker Neko Browser Control Worker

[Service]
Type=simple
User=root
StandardInput=socket
StandardOutput=null
StandardError=journal
ExecStart=/usr/bin/python3 -E -s {helper_path} control-worker --allowed-user {allowed_user}
"""


def detect_tentacle_app_user() -> str:
    """探测 systemd 生产环境应用账号名（默认 tentacle）。"""
    try:
        import pwd

        for cand in ("tentacle",):
            try:
                pwd.getpwnam(cand)
                return cand
            except KeyError:
                continue
    except ImportError:
        pass
    return "root"


def install_control_socket(*, allowed_user: str | None = None, helper_path: Path | None = None) -> bool:
    """在 Linux systemd 主机安装受控 Unix Socket，使低权限 WebUI 可安全启停 Neko。"""
    if sys.platform != "linux":
        return False
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid) or geteuid() != 0:
        return False
    systemctl = shutil.which("systemctl")
    if not systemctl or not Path("/etc/systemd/system").is_dir():
        return False

    user = (allowed_user or detect_tentacle_app_user()).strip() or "root"
    if helper_path is not None:
        script_path = helper_path
    else:
        stable = Path("/opt/tentacle/current/scripts/browser_neko.py")
        resolved = Path(__file__).resolve()
        script_path = stable if (stable.is_file() and stable.resolve() == resolved) else resolved
    _atomic_write(Path("/etc/systemd/system") / CONTROL_SOCKET_UNIT, render_control_socket_unit(user), mode=0o644)
    _atomic_write(Path("/etc/systemd/system") / CONTROL_SERVICE_UNIT, render_control_service_unit(script_path, user), mode=0o644)
    subprocess.run([systemctl, "daemon-reload"], check=False)
    subprocess.run([systemctl, "enable", "--now", CONTROL_SOCKET_UNIT], check=False)
    return True


def remove_control_socket() -> None:
    if sys.platform != "linux":
        return
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid) or geteuid() != 0:
        return
    systemctl = shutil.which("systemctl")
    if systemctl:
        subprocess.run([systemctl, "disable", "--now", CONTROL_SOCKET_UNIT], check=False)
    for name in (CONTROL_SOCKET_UNIT, CONTROL_SERVICE_UNIT):
        unit = Path("/etc/systemd/system") / name
        if unit.exists():
            try:
                unit.unlink()
            except OSError:
                pass
    if systemctl:
        subprocess.run([systemctl, "daemon-reload"], check=False)


def execute_local_control(
    action: str,
    *,
    plugin_cfg: dict[str, Any],
    install_dir: Path | None = None,
) -> dict[str, Any]:
    """在本机具备 Docker 权限的进程内执行白名单 Neko 控制动作（不回显密码）。"""
    act = str(action or "").strip().lower()
    if act not in ALLOWED_CONTROL_ACTIONS:
        raise ValueError(f"不支持的控制操作: {action}")

    cfg = config_from_plugin_dict(plugin_cfg, default_dir=install_dir)
    root = Path(cfg.install_dir)

    if act == "proxy_config":
        return {
            "ok": True,
            "action": act,
            "proxy_config": render_reverse_proxy_examples(cfg),
        }

    if act == "status":
        compose_cmd = detect_compose_command()
        installed = (root / "docker-compose.yaml").is_file()
        container_state = inspect_container_state(cfg.container_name)
        cdp_info = probe_cdp_endpoint(cfg.cdp_port, timeout=1.5)
        return {
            "ok": True,
            "available": compose_cmd is not None and docker_daemon_accessible(),
            "docker_available": compose_cmd is not None,
            "installed": installed,
            "container_name": cfg.container_name,
            "container_state": container_state,
            "running": container_state == "running",
            "cdp_port": cfg.cdp_port,
            "cdp_url": f"http://127.0.0.1:{cfg.cdp_port}/json/version",
            "cdp_reachable": bool(cdp_info and cdp_info.get("webSocketDebuggerUrl")),
            "browser_version": str((cdp_info or {}).get("Browser") or "").strip(),
            "http_listen": f"http://{cfg.bind}:{cfg.http_port}",
            "mux_port": cfg.mux_port,
            "nat_ip": cfg.nat_ip,
            "screen": cfg.screen,
            "install_dir": cfg.install_dir,
            "live_view_url": cfg.live_view_url,
            "proxy_config": render_reverse_proxy_examples(cfg),
        }

    compose_cmd = detect_compose_command()
    if compose_cmd is None or not docker_daemon_accessible():
        raise RuntimeError("当前环境未检测到可用 Docker 引擎，请先运行 `sudo tentacle browser install --install-docker`。")

    if act in {"start", "restart"}:
        _creds, _rotated, files_changed = write_install_files(cfg, rotate_passwords=False)
        args = ["up", "-d", "--force-recreate"] if (act == "restart" or files_changed) else ["up", "-d"]
        proc = _run_compose(compose_cmd, root, args, check=False, capture=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise RuntimeError(f"Docker Compose 执行失败: {detail or proc.returncode}")
        wait_for_cdp(cfg.cdp_port, deadline_seconds=20.0)
    elif act == "stop":
        if (root / "docker-compose.yaml").is_file():
            proc = _run_compose(compose_cmd, root, ["stop"], check=False, capture=True)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[:400]
                raise RuntimeError(f"停止 Neko 容器失败: {detail or proc.returncode}")

    result = execute_local_control("status", plugin_cfg=plugin_cfg, install_dir=root)
    result["action"] = act
    return result


def send_control_socket_request(
    action: str,
    plugin_cfg: dict[str, Any],
    *,
    socket_path: Path = CONTROL_SOCKET_PATH,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """通过受控 Unix Socket 向 root worker 发送白名单控制请求。"""
    if not hasattr(socket, "AF_UNIX"):
        raise OSError("当前操作系统不支持 Unix Socket")
    payload = json.dumps({"action": action, "config": plugin_cfg}, ensure_ascii=False) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(socket_path))
        conn.sendall(payload.encode("utf-8"))
        conn.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 512 * 1024:
                raise RuntimeError("控制代理响应过大")
    text = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not text:
        raise RuntimeError("控制代理未返回响应")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise RuntimeError("控制代理响应格式无效")
    if data.get("ok") is False:
        raise RuntimeError(str(data.get("error") or "Neko 控制操作失败"))
    return data


def control_neko_from_app(
    action: str,
    plugin_cfg: dict[str, Any],
    *,
    socket_path: Path = CONTROL_SOCKET_PATH,
) -> dict[str, Any]:
    """供 Tentacle WebUI 插件后端调用：优先走受控 Unix Socket，否则尝试本机直接控制。"""
    if socket_path.exists() and hasattr(socket, "AF_UNIX"):
        try:
            res = send_control_socket_request(action, plugin_cfg, socket_path=socket_path)
            res["control_mode"] = "systemd_socket"
            return res
        except OSError:
            pass

    if action == "proxy_config":
        res = execute_local_control("proxy_config", plugin_cfg=plugin_cfg)
        res["control_mode"] = "local"
        return res

    if detect_compose_command() is not None and docker_daemon_accessible():
        res = execute_local_control(action, plugin_cfg=plugin_cfg)
        res["control_mode"] = "local_docker"
        return res

    # 无可控通道时，对 status 返回优雅降级状态（不抛 500），对写操作抛出明确引导
    cfg = config_from_plugin_dict(plugin_cfg)
    cdp_info = probe_cdp_endpoint(cfg.cdp_port, timeout=1.5)
    hint = "请先在服务器终端执行一次 `sudo tentacle browser install --install-docker` 初始化 Docker Neko 与受控启停通道。"
    if action == "status":
        return {
            "ok": True,
            "available": False,
            "control_mode": "unavailable",
            "docker_available": detect_compose_command() is not None,
            "installed": (Path(cfg.install_dir) / "docker-compose.yaml").is_file(),
            "container_name": cfg.container_name,
            "container_state": "running" if (cdp_info and cdp_info.get("webSocketDebuggerUrl")) else "unknown",
            "running": bool(cdp_info and cdp_info.get("webSocketDebuggerUrl")),
            "cdp_port": cfg.cdp_port,
            "cdp_url": f"http://127.0.0.1:{cfg.cdp_port}/json/version",
            "cdp_reachable": bool(cdp_info and cdp_info.get("webSocketDebuggerUrl")),
            "browser_version": str((cdp_info or {}).get("Browser") or "").strip(),
            "http_listen": f"http://{cfg.bind}:{cfg.http_port}",
            "mux_port": cfg.mux_port,
            "nat_ip": cfg.nat_ip,
            "screen": cfg.screen,
            "install_dir": cfg.install_dir,
            "live_view_url": cfg.live_view_url,
            "proxy_config": render_reverse_proxy_examples(cfg),
            "setup_hint": hint,
        }
    raise RuntimeError(hint)


def _peer_uid(conn: socket.socket) -> int | None:
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return None
    raw = conn.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def run_control_worker(allowed_user: str) -> int:
    """systemd socket activation 单次请求 worker（stdin 即已接受的 Unix socket fd）。"""
    if sys.platform != "linux":
        return 1
    import pwd

    allowed_uids = {0}
    try:
        allowed_uids.add(pwd.getpwnam(allowed_user).pw_uid)
    except KeyError:
        pass

    conn = socket.fromfd(0, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        uid = _peer_uid(conn)
        if uid is None or uid not in allowed_uids:
            conn.sendall(json.dumps({"ok": False, "error": "拒绝未授权的本机进程调用"}, ensure_ascii=False).encode() + b"\n")
            return 1
        conn.settimeout(10.0)
        chunks: list[bytes] = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 128 * 1024:
                raise ValueError("请求体过大")
        req = json.loads(b"".join(chunks).decode("utf-8"))
        if not isinstance(req, dict):
            raise ValueError("请求格式无效")
        action = str(req.get("action") or "").strip().lower()
        plugin_cfg = req.get("config") if isinstance(req.get("config"), dict) else {}
        result = execute_local_control(action, plugin_cfg=plugin_cfg)
        conn.sendall((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
        return 0
    except Exception as exc:
        try:
            conn.sendall((json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass
        return 1
    finally:
        conn.close()


def cmd_install(args: argparse.Namespace) -> int:
    total_mem, total_swap = detect_host_memory_mib()
    tentacle_cfg_path = None if args.skip_tentacle_config else discover_tentacle_config_path(args.tentacle_config)
    saved_plugin_cfg = read_tentacle_browser_config(tentacle_cfg_path)

    raw_dir = args.dir or str(saved_plugin_cfg.get("neko_dir") or "").strip()
    install_dir = Path(raw_dir).expanduser().resolve() if raw_dir else default_install_dir()
    existing = load_saved_config(install_dir)

    def pick_str(cli_val: str | None, plugin_key: str, existing_val: str | None, fallback: str) -> str:
        if cli_val is not None:
            return cli_val
        p_val = str(saved_plugin_cfg.get(plugin_key) or "").strip()
        if p_val:
            return p_val
        if existing_val:
            return existing_val
        return fallback

    def pick_int(cli_val: int | None, plugin_key: str, existing_val: int | None, fallback: int) -> int:
        if cli_val is not None:
            return cli_val
        if plugin_key in saved_plugin_cfg and saved_plugin_cfg[plugin_key] not in (None, ""):
            return int(saved_plugin_cfg[plugin_key])
        if existing_val is not None:
            return existing_val
        return fallback

    nat_ip = validate_nat_ip(pick_str(args.nat_ip, "neko_nat_ip", existing.nat_ip if existing else "", ""))
    shm_raw = pick_str(args.shm_size, "neko_shm_size", existing.shm_size if existing else "", recommend_shm_size(total_mem))
    cfg = NekoConfig(
        install_dir=str(install_dir),
        container_name=validate_container_name(pick_str(args.container_name, "neko_container_name", existing.container_name if existing else "", DEFAULT_CONTAINER_NAME)),
        image=validate_image(pick_str(args.image, "neko_image", existing.image if existing else "", DEFAULT_IMAGE)),
        bind=validate_bind(pick_str(args.bind, "neko_bind", existing.bind if existing else "", DEFAULT_BIND)),
        http_port=validate_port(pick_int(args.http_port, "neko_http_port", existing.http_port if existing else None, DEFAULT_HTTP_PORT), "http-port"),
        cdp_port=validate_port(pick_int(args.cdp_port, "neko_cdp_port", existing.cdp_port if existing else None, DEFAULT_CDP_PORT), "cdp-port"),
        mux_port=validate_port(pick_int(args.mux_port, "neko_mux_port", existing.mux_port if existing else None, DEFAULT_MUX_PORT), "mux-port"),
        nat_ip=nat_ip,
        screen=validate_screen(pick_str(args.screen, "neko_screen", existing.screen if existing else "", DEFAULT_SCREEN)),
        shm_size=validate_shm_size(shm_raw),
        live_view_url=normalize_live_view_url(pick_str(args.live_view_url, "live_view_url", existing.live_view_url if existing else "", "")),
    )

    if total_mem is not None and total_mem <= 2600 and (total_swap is None or total_swap < 1024):
        print(
            f"[提示] 检测到当前主机物理内存约 {total_mem} MiB、Swap 约 {total_swap or 0} MiB。"
            "建议配置至少 2G Swap 避免重型网页触发 OOM（已自动将 shm_size 调整为 512m）。"
        )

    creds, rotated, files_changed = write_install_files(cfg, rotate_passwords=args.rotate_passwords)
    print(f"[neko] 已写入运行配置到: {install_dir}")
    print(f"[neko] 反代参考配置已生成: {install_dir / 'reverse-proxy.example.conf'}")

    if not args.skip_tentacle_config:
        if tentacle_cfg_path is not None:
            if update_tentacle_browser_config(tentacle_cfg_path, cfg=cfg):
                print(f"[neko] 已同步 Tentacle browser 插件配置: {tentacle_cfg_path}")
            else:
                print(f"[提示] 当前 Python 环境缺少 PyYAML，请手动在 WebUI /plugins 的 browser 插件中设置 neko_cdp_port = {cfg.cdp_port}")
        else:
            print(f"[提示] 未自动找到 Tentacle config.yaml，可在 WebUI /plugins 的 browser 插件中填写 neko_cdp_port = {cfg.cdp_port}")

    if not args.no_start and not args.no_control_socket:
        if install_control_socket(allowed_user=args.app_user):
            print(f"[neko] 已安装 WebUI 受控启停通道: {CONTROL_SOCKET_UNIT}（此后可直接在 /plugins 启停 Neko）")

    if args.no_start:
        print("[neko] 已指定 --no-start，跳过镜像拉取与容器启动。")
        return 0

    compose_cmd = detect_compose_command()
    if compose_cmd is None:
        if args.install_docker:
            install_docker_engine()
            compose_cmd = detect_compose_command()
        if compose_cmd is None:
            print(
                "[错误] 未检测到 docker / docker compose。\n"
                "请先安装 Docker，或重新运行并加上 --install-docker（Linux root）：\n"
                "  sudo tentacle browser install --install-docker",
                file=sys.stderr,
            )
            return 2

    print(f"[neko] 正在拉取并启动 {cfg.image} ...")
    _run_compose(compose_cmd, install_dir, ["pull"])
    up_args = ["up", "-d", "--force-recreate"] if files_changed else ["up", "-d"]
    _run_compose(compose_cmd, install_dir, up_args)

    print(f"[neko] 等待本机 CDP 端点 127.0.0.1:{cfg.cdp_port} 就绪...")
    cdp_info = wait_for_cdp(cfg.cdp_port)
    if cdp_info:
        print(f"[neko] CDP 已就绪: {cdp_info.get('Browser', 'Chromium')}")
    else:
        print("[警告] 容器已启动，但暂未探测到本机 CDP 响应，请执行 `tentacle browser logs` 排查。")

    print("\n=== 安装完成 ===")
    print(f"• Neko 本地入口 : http://{cfg.bind}:{cfg.http_port}")
    print(f"• 本机 CDP 发现 : http://127.0.0.1:{cfg.cdp_port}/json/version（仅本机回环访问）")
    print(f"• WebRTC 放行口 : {cfg.mux_port}/udp 与 {cfg.mux_port}/tcp（单端口 MUX）")
    if cfg.nat_ip:
        print(f"• WebRTC NAT IP : {cfg.nat_ip}")
    else:
        print("• WebRTC NAT IP : 未显式固定（Neko 启动时自动探测公网 IP；如仅走 Tailscale 或需填域名，可在 /plugins 填写 neko_nat_ip）")
    if rotated:
        print("• 首次生成的登录凭据（仅保存在 0600 权限 .env，可用 `tentacle browser credentials` 查看）：")
        print(f"    管理员账号 admin  / 密码: {creds['NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD']}")
        print(f"    普通账号   neko   / 密码: {creds['NEKO_MEMBER_MULTIUSER_USER_PASSWORD']}")
    else:
        print("• 登录凭据沿用已有 .env（可用 `tentacle browser credentials` 查看）")
    print(f"• HTTPS 反代示例: 查看 {install_dir / 'reverse-proxy.example.conf'} 或在 WebUI /plugins 查看")
    print("• 如修改了 Tentacle 配置，请在 WebUI /plugins 重启应用或执行 `tentacle restart` 生效。")
    return 0


def _require_installed(args: argparse.Namespace) -> tuple[Path, NekoConfig]:
    install_dir = Path(args.dir).expanduser().resolve() if args.dir else default_install_dir()
    cfg = load_saved_config(install_dir)
    if cfg is None or not (install_dir / "docker-compose.yaml").is_file():
        raise FileNotFoundError(f"未在 {install_dir} 找到已安装的 Neko 配置，请先执行 install。")
    return install_dir, cfg


def cmd_compose_lifecycle(action: str, args: argparse.Namespace) -> int:
    install_dir, cfg = _require_installed(args)
    compose_cmd = detect_compose_command()
    if compose_cmd is None:
        print("[错误] 未检测到 docker / docker compose。", file=sys.stderr)
        return 2

    if action == "start":
        _run_compose(compose_cmd, install_dir, ["up", "-d"])
        cdp_info = wait_for_cdp(cfg.cdp_port, deadline_seconds=20.0)
        print(f"[neko] 已启动（CDP: {'在线 ' + str(cdp_info.get('Browser', '')) if cdp_info else '等待中'}）")
    elif action == "stop":
        _run_compose(compose_cmd, install_dir, ["stop"])
        print("[neko] 已停止（持久化 profile 登录态已保留）。")
    elif action == "restart":
        _run_compose(compose_cmd, install_dir, ["restart"])
        cdp_info = wait_for_cdp(cfg.cdp_port, deadline_seconds=20.0)
        print(f"[neko] 已重启（CDP: {'在线 ' + str(cdp_info.get('Browser', '')) if cdp_info else '等待中'}）")
    elif action == "logs":
        tail = str(getattr(args, "tail", 100) or 100)
        extra = ["logs", "--tail", tail]
        if getattr(args, "follow", False):
            extra.append("-f")
        _run_compose(compose_cmd, install_dir, extra, check=False)
    elif action == "status":
        _run_compose(compose_cmd, install_dir, ["ps"], check=False)
        cdp_info = probe_cdp_endpoint(cfg.cdp_port, timeout=2.0)
        print(f"\n安装目录 : {install_dir}")
        print(f"Neko 监听: http://{cfg.bind}:{cfg.http_port}")
        print(f"WebRTC口 : {cfg.mux_port} (UDP+TCP MUX, NAT={cfg.nat_ip or '自动探测'})")
        print(f"本机 CDP : http://127.0.0.1:{cfg.cdp_port}/json/version -> {'在线 (' + str(cdp_info.get('Browser', 'Chromium')) + ')' if cdp_info else '未连接'}")
        if cfg.live_view_url:
            print(f"可视化URL: {cfg.live_view_url}")
    elif action == "uninstall":
        _run_compose(compose_cmd, install_dir, ["down", "--remove-orphans"], check=False)
        remove_control_socket()
        if getattr(args, "purge", False):
            shutil.rmtree(install_dir, ignore_errors=True)
            print(f"[neko] 已移除容器、受控 Socket 并清空目录: {install_dir}")
        else:
            print(f"[neko] 已停止并移除容器，登录态数据保留在: {install_dir / 'profile'}")
    return 0


def cmd_credentials(args: argparse.Namespace) -> int:
    install_dir, _cfg = _require_installed(args)
    creds = parse_env_file(install_dir / ".env")
    if not creds:
        print(f"[错误] 未找到凭据文件: {install_dir / '.env'}", file=sys.stderr)
        return 1
    print(f"管理员账号 admin / 密码: {creds.get('NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD', '(未设置)')}")
    print(f"普通账号   neko  / 密码: {creds.get('NEKO_MEMBER_MULTIUSER_USER_PASSWORD', '(未设置)')}")
    return 0


def cmd_proxy_config(args: argparse.Namespace) -> int:
    _install_dir, cfg = _require_installed(args)
    print(render_reverse_proxy_examples(cfg))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="browser_neko.py",
        description="Tentacle Docker Neko（Chromium + WebRTC + 本机 CDP）一键安装与管理工具",
    )
    parser.add_argument("--dir", default=None, help="Neko 数据与配置目录（默认 root 为 /var/lib/tentacle-browser）")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_install = sub.add_parser("install", help="一键生成配置、拉取镜像并启动 Neko 浏览器容器")
    p_install.add_argument("--image", default=None, help=f"Docker 镜像（默认 {DEFAULT_IMAGE}）")
    p_install.add_argument("--container-name", default=None, help=f"容器名称（默认 {DEFAULT_CONTAINER_NAME}）")
    p_install.add_argument("--bind", default=None, help=f"Neko Web 监听地址（默认 {DEFAULT_BIND}）")
    p_install.add_argument("--http-port", type=int, default=None, help=f"Neko Web 端口（默认 {DEFAULT_HTTP_PORT}）")
    p_install.add_argument("--cdp-port", type=int, default=None, help=f"宿主机本机回环 CDP 端口（默认 {DEFAULT_CDP_PORT}）")
    p_install.add_argument("--mux-port", type=int, default=None, help=f"WebRTC 单端口 TCP+UDP MUX（默认 {DEFAULT_MUX_PORT}）")
    p_install.add_argument("--nat-ip", default=None, help="客户端可达的服务器公网 IP / Tailscale IP / 域名（支持逗号分隔）")
    p_install.add_argument("--screen", default=None, help=f"虚拟屏幕分辨率与帧率（默认 {DEFAULT_SCREEN}）")
    p_install.add_argument("--shm-size", default=None, help="容器共享内存大小（默认 2G 机器 512m，其他 1g）")
    p_install.add_argument("--live-view-url", default=None, help="对外访问的 Neko URL（如 https://browser.example.com），写入 Tentacle browser 插件")
    p_install.add_argument("--tentacle-config", default=None, help="指定 Tentacle config.yaml 路径以自动读写插件配置")
    p_install.add_argument("--skip-tentacle-config", action="store_true", help="不自动读写 Tentacle config.yaml")
    p_install.add_argument("--install-docker", action="store_true", help="未安装 Docker 时自动通过官方脚本安装（需 Linux root）")
    p_install.add_argument("--app-user", default=None, help="允许通过受控 Unix Socket 启停 Neko 的 Tentacle 应用系统账号（默认自动探测 tentacle）")
    p_install.add_argument("--no-control-socket", action="store_true", help="不安装 systemd WebUI 受控启停 Socket")
    p_install.add_argument("--rotate-passwords", action="store_true", help="重新生成 Neko 随机登录密码")
    p_install.add_argument("--no-start", action="store_true", help="仅生成配置文件，不拉取镜像或启动容器")

    sub.add_parser("start", help="启动 Neko 浏览器容器")
    sub.add_parser("stop", help="停止 Neko 浏览器容器（释放内存，保留登录态）")
    sub.add_parser("restart", help="重启 Neko 浏览器容器")
    sub.add_parser("status", help="查看容器运行状态与本机 CDP 健康状态")
    sub.add_parser("credentials", help="查看当前生成的 Neko 登录账号密码")
    sub.add_parser("proxy-config", help="打印适配当前端口的 Nginx/Caddy/Tailscale 反代配置")

    p_logs = sub.add_parser("logs", help="查看 Neko 容器日志")
    p_logs.add_argument("--tail", type=int, default=100, help="查看末尾日志行数（默认 100）")
    p_logs.add_argument("-f", "--follow", action="store_true", help="实时跟踪日志")

    p_un = sub.add_parser("uninstall", help="停止并移除容器（默认保留登录 profile）")
    p_un.add_argument("--purge", action="store_true", help="同时删除数据目录与持久化登录态")

    p_worker = sub.add_parser("control-worker", help=argparse.SUPPRESS)
    p_worker.add_argument("--allowed-user", default="tentacle")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "install":
            return cmd_install(args)
        if args.subcommand == "credentials":
            return cmd_credentials(args)
        if args.subcommand == "proxy-config":
            return cmd_proxy_config(args)
        if args.subcommand == "control-worker":
            return run_control_worker(args.allowed_user)
        return cmd_compose_lifecycle(args.subcommand, args)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"[错误] 命令执行失败（退出码 {exc.returncode}）", file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
