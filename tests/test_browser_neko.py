"""Docker Neko 安装脚本与 browser 插件 live_view_url 入口测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from agent.commands import CommandRegistry
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from plugins.manifest import bundled_plugins_dir
from runtime.tool_registry import ToolRegistry
from scripts import browser_neko, release_package
from scripts.deploy import managed as deploy_managed
from web.adapter import ConnectionManager
from web.server import create_app
from web.settings import DEVICE_COOKIE, WebSettings


def test_browser_neko_files_are_in_deploy_whitelist():
    for rel in (
        "scripts/browser_neko.py",
        "scripts/tentacle.sh",
        "plugins/bundled/browser/manifest.yaml",
        "plugins/bundled/browser/plugin.py",
    ):
        assert deploy_managed(rel), f"not deploy.managed: {rel}"
        assert release_package.runtime_name(rel), f"not release_package.runtime_name: {rel}"
        assert release_package.package_name(rel), f"not release_package.package_name: {rel}"


def test_browser_neko_validators_and_renderers():
    assert browser_neko.validate_bind("127.0.0.1") == "127.0.0.1"
    assert browser_neko.validate_bind("0.0.0.0") == "0.0.0.0"
    with pytest.raises(ValueError):
        browser_neko.validate_bind("not-an-ip")

    assert browser_neko.validate_screen("1280x720@24") == "1280x720@24"
    with pytest.raises(ValueError):
        browser_neko.validate_screen("1280x720")

    assert browser_neko.normalize_live_view_url("https://browser.example.com/neko/") == "https://browser.example.com/neko/"
    for bad in ("javascript:alert(1)", "ftp://example.com", "https://user:pass@example.com"):
        with pytest.raises(ValueError):
            browser_neko.normalize_live_view_url(bad)

    assert browser_neko.recommend_shm_size(1980) == "512m"
    assert browser_neko.recommend_shm_size(4096) == "1g"

    sup = browser_neko.render_chromium_supervisord_conf()
    assert "--remote-debugging-port=9223" in sup
    assert "--remote-debugging-address=0.0.0.0" not in sup
    assert "--user-data-dir=/home/neko/.config/chromium" in sup
    assert "--bwsi" not in sup
    assert "chromium-unlock" not in sup
    assert "SingletonLock" in sup
    assert "[program:cdp-proxy]" in sup
    assert "/etc/neko/cdp-proxy.py" in sup

    proxy_py = browser_neko.render_cdp_proxy_script()
    compile(proxy_py, "cdp-proxy.py", "exec")
    assert "9222" in proxy_py and "9223" in proxy_py

    policies = json.loads(browser_neko.render_chromium_policies())
    assert policies["DeveloperToolsAvailability"] == 1

    cfg = browser_neko.NekoConfig(
        install_dir="/var/lib/tentacle-browser",
        bind="127.0.0.1",
        http_port=8080,
        cdp_port=9222,
        mux_port=52000,
        nat_ip="203.0.113.10",
        screen="1280x720@24",
        shm_size="512m",
        live_view_url="https://browser.example.com",
    )
    compose = browser_neko.render_docker_compose(cfg)
    assert '"127.0.0.1:8080:8080"' in compose
    assert '"127.0.0.1:9222:9222"' in compose
    assert '"52000:52000/udp"' in compose
    assert '"52000:52000/tcp"' in compose
    assert "./policies.json:/etc/chromium/policies/managed/policies.json:ro" in compose
    assert 'NEKO_SERVER_PROXY: "true"' in compose
    assert "NEKO_WEBRTC_UDPMUX: 52000" in compose
    assert "NEKO_WEBRTC_TCPMUX: 52000" in compose
    assert 'NEKO_WEBRTC_NAT1TO1: "203.0.113.10"' in compose
    assert 'shm_size: "512m"' in compose

    proxy_conf = browser_neko.render_reverse_proxy_examples(cfg)
    assert "127.0.0.1:8080" in proxy_conf
    assert "52000/udp" in proxy_conf

    # 插件配置字典转 NekoConfig 与 systemd 受控 Socket 模板校验
    from_plugin = browser_neko.config_from_plugin_dict({
        "neko_dir": "/var/lib/tentacle-browser",
        "neko_bind": "127.0.0.1",
        "neko_http_port": 18080,
        "neko_cdp_port": 19222,
        "neko_mux_port": 52100,
        "neko_nat_ip": "browser.example.com,100.64.0.1",
        "neko_screen": "1280x720@24",
        "live_view_url": "https://browser.example.com",
    })
    assert from_plugin.http_port == 18080
    assert from_plugin.cdp_port == 19222
    assert from_plugin.nat_ip == "browser.example.com,100.64.0.1"

    sock_unit = browser_neko.render_control_socket_unit("tentacle")
    assert "SocketUser=tentacle" in sock_unit
    assert "Accept=yes" in sock_unit
    svc_unit = browser_neko.render_control_service_unit(browser_neko.Path("/opt/tentacle/current/scripts/browser_neko.py"), "tentacle")
    assert "control-worker --allowed-user tentacle" in svc_unit


def test_browser_neko_cli_install_no_start_and_tentacle_config_sync(tmp_path):
    install_dir = tmp_path / "neko"
    tentacle_cfg = tmp_path / "config.yaml"
    tentacle_cfg.write_text(
        yaml.safe_dump({
            "webui": {"port": 8420},
            "plugins": {
                "items": {
                    "qqbot": {"enabled": False},
                    "browser": {
                        "enabled": True,
                        "trusted": True,
                        "config": {
                            "chrome_path": "/old/chrome",
                            "headed": True,
                            "cdp_discovery_url": "http://10.0.0.1:9222/json/version",
                        },
                    },
                }
            },
        }),
        encoding="utf-8",
    )

    rc = browser_neko.main([
        "--dir", str(install_dir),
        "install",
        "--nat-ip", "198.51.100.8",
        "--http-port", "18080",
        "--cdp-port", "19222",
        "--mux-port", "52123",
        "--live-view-url", "https://browser.example.test",
        "--tentacle-config", str(tentacle_cfg),
        "--no-start",
    ])
    assert rc == 0

    assert (install_dir / "docker-compose.yaml").is_file()
    assert (install_dir / "chromium.conf").is_file()
    assert (install_dir / "cdp-proxy.py").is_file()
    assert (install_dir / "policies.json").is_file()
    assert (install_dir / "reverse-proxy.example.conf").is_file()
    assert (install_dir / ".env").is_file()
    assert (install_dir / "profile").is_dir()

    creds_1 = browser_neko.parse_env_file(install_dir / ".env")
    assert creds_1["NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD"]
    assert creds_1["NEKO_MEMBER_MULTIUSER_USER_PASSWORD"]

    # 二次 install 默认保留既有密码，不覆盖
    rc2 = browser_neko.main([
        "--dir", str(install_dir),
        "install",
        "--tentacle-config", str(tentacle_cfg),
        "--no-start",
    ])
    assert rc2 == 0
    creds_2 = browser_neko.parse_env_file(install_dir / ".env")
    assert creds_2 == creds_1

    # Tentacle config.yaml 仅增量写入连接地址，绝不写入 Neko 密码且保留其他配置
    saved_text = tentacle_cfg.read_text(encoding="utf-8")
    assert creds_1["NEKO_MEMBER_MULTIUSER_ADMIN_PASSWORD"] not in saved_text
    assert creds_1["NEKO_MEMBER_MULTIUSER_USER_PASSWORD"] not in saved_text
    saved = yaml.safe_load(saved_text)
    assert saved["webui"]["port"] == 8420
    assert saved["plugins"]["items"]["qqbot"]["enabled"] is False
    browser_cfg = saved["plugins"]["items"]["browser"]["config"]
    assert browser_cfg["neko_cdp_port"] == 19222
    assert browser_cfg["live_view_url"] == "https://browser.example.test"
    assert "cdp_discovery_url" not in browser_cfg
    assert "chrome_path" not in browser_cfg
    assert "headed" not in browser_cfg
    assert "neko_enabled" not in browser_cfg

    # credentials & proxy-config 子命令可正常读取
    assert browser_neko.main(["--dir", str(install_dir), "credentials"]) == 0
    assert browser_neko.main(["--dir", str(install_dir), "proxy-config"]) == 0

    state = json.loads((install_dir / "neko-install.json").read_text(encoding="utf-8"))
    assert "ADMIN_PASSWORD" not in json.dumps(state)
    assert state["cdp_port"] == 19222


async def test_browser_live_view_summary_and_ext_route_behind_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("TENTACLE_HOME", str(tmp_path))
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    pm = PluginManager(
        plugins_dir=tmp_path / "user-plugins",
        bundled_dir=bundled_plugins_dir(),
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )

    await pm.startup({
        "plugins": {
            "items": {
                "browser": {
                    "enabled": True,
                    "trusted": True,
                    "config": {
                        "neko_cdp_port": 19229,
                        "live_view_url": "https://browser.example.test/neko/",
                    },
                }
            },
            "mcp": {},
        }
    })

    try:
        settings = WebSettings(str(tmp_path / "webui_settings.json"))
        settings.set_password("pw123456")
        token = settings.issue_token()
        dev_cookie = settings.create_device("test-device")
        settings.set_device_auth(True)
        settings.deny_new_devices()

        conn = ConnectionManager()
        conn.broadcast = AsyncMock()
        app = create_app(
            conn_manager=conn,
            web_adapter=SimpleNamespace(),
            agent_session=SimpleNamespace(),
            heartbeat=SimpleNamespace(),
            session_manager=SimpleNamespace(),
            chronicle=SimpleNamespace(db_path=str(tmp_path / "chronicle.db")),
            settings=settings,
            goal_manager=SimpleNamespace(),
            plugin_manager=pm,
        )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            # 未带设备 Cookie 必须 403
            assert (await client.get("/api/plugins/ext/browser/live-view")).status_code == 403

            # 仅有设备 Cookie 无 Bearer Token 必须 401
            client.cookies.set(DEVICE_COOKIE, dev_cookie)
            assert (await client.get("/api/plugins/ext/browser/live-view")).status_code == 401

            headers = {"Authorization": f"Bearer {token}"}
            ui_res = await client.get("/api/plugins/ui", headers=headers)
            assert ui_res.status_code == 200
            ui_body = ui_res.json()
            assert ui_body["browser_live_view"] == {
                "live_view_url": "https://browser.example.test/neko/",
                "cdp_configured": True,
                "mode": "neko",
            }

            live_res = await client.get("/api/plugins/ext/browser/live-view", headers=headers)
            assert live_res.status_code == 200
            live_body = live_res.json()
            assert live_body["configured"] is True
            assert live_body["live_view_url"] == "https://browser.example.test/neko/"
            assert live_body["cdp_port"] == 19229
            assert live_body["cdp_configured"] is True
            assert live_body["mode"] == "neko"
            assert "webSocketDebuggerUrl" not in live_body

            # Neko 控制路由同样必须受 DeviceAccessGate + auth_dep 保护，并拒绝非法 action
            neko_status = await client.get("/api/plugins/ext/browser/neko/status", headers=headers)
            assert neko_status.status_code == 200
            ns = neko_status.json()
            assert ns["ok"] is True
            assert "ADMIN_PASSWORD" not in json.dumps(ns)

            bad_action = await client.post(
                "/api/plugins/ext/browser/neko/action",
                headers=headers,
                json={"action": "rm -rf /"},
            )
            assert bad_action.status_code == 400

            proxy_action = await client.post(
                "/api/plugins/ext/browser/neko/action",
                headers=headers,
                json={"action": "proxy_config"},
            )
            assert proxy_action.status_code == 200
            assert "reverse_proxy" in proxy_action.json()["proxy_config"]
    finally:
        await pm.shutdown()


async def test_cdp_proxy_script_forwards_half_closed_tcp_stream():
    import asyncio

    ns: dict = {}
    exec(compile(browser_neko.render_cdp_proxy_script(), "cdp-proxy.py", "exec"), ns)

    async def echo_upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.read()
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n" + data)
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo_upstream, "127.0.0.1", 0)
    up_port = upstream.sockets[0].getsockname()[1]

    orig_open = asyncio.open_connection

    async def redirected_open(host, port, *a, **kw):
        assert (host, port) == ("127.0.0.1", 9223)
        return await orig_open("127.0.0.1", up_port, *a, **kw)

    ns["asyncio"].open_connection = redirected_open
    proxy_srv = await asyncio.start_server(ns["_handle"], "127.0.0.1", 0)
    proxy_port = proxy_srv.sockets[0].getsockname()[1]

    try:
        r, w = await orig_open("127.0.0.1", proxy_port)
        w.write(b"GET /json/version HTTP/1.1\r\n\r\n")
        await w.drain()
        w.write_eof()
        resp = await r.read()
        w.close()
        assert resp == b"HTTP/1.1 200 OK\r\n\r\nGET /json/version HTTP/1.1\r\n\r\n"
    finally:
        ns["asyncio"].open_connection = orig_open
        proxy_srv.close()
        upstream.close()
        await proxy_srv.wait_closed()
        await upstream.wait_closed()
