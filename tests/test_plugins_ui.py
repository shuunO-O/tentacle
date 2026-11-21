"""插件自定义沙箱 WebUI（manifest ui、ui_bundle 装配、受鉴权文档/资源路由与实时事件）测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.commands import CommandRegistry
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from plugins.manifest import parse_manifest
from plugins.permissions import PermissionDeniedError, PluginManifestError
from plugins.ui_bundle import SANDBOX_CSP, build_plugin_ui_document
from runtime.tool_registry import ToolRegistry
from web.adapter import ConnectionManager
from web.server import create_app
from web.settings import DEVICE_COOKIE, WebSettings


def _fresh_registry() -> ToolRegistry:
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    return reg


def _make_ui_plugin(
    root: Path,
    plugin_id: str = "viz",
    *,
    manifest_yaml: str | None = None,
    code: str | None = None,
    include_ui: bool = True,
) -> Path:
    pdir = root / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    if manifest_yaml is None:
        manifest_yaml = (
            f"id: {plugin_id}\n"
            "name: 可视化仪表盘\n"
            "version: '1.2.0'\n"
            "description: 交互式数据看板\n"
            "entrypoint: plugin.py:setup\n"
            "permissions:\n"
            "  - web:ui\n"
            "  - web:routes\n"
            "ui:\n"
            "  dir: ui\n"
            "  entry: index.html\n"
            "  title: 实时看板\n"
            "  placement: sidebar\n"
        )
    (pdir / "manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
    if code is not None:
        (pdir / "plugin.py").write_text(code, encoding="utf-8")
    elif "entrypoint:" in manifest_yaml:
        (pdir / "plugin.py").write_text(
            "from fastapi import APIRouter\n"
            "async def setup(ctx):\n"
            "    router = APIRouter()\n"
            "    @router.get('/metrics')\n"
            "    async def metrics():\n"
            "        return {'points': [1, 2, 3]}\n"
            "    ctx.register_router(router)\n",
            encoding="utf-8",
        )
    if include_ui:
        ui_dir = pdir / "ui"
        ui_dir.mkdir(parents=True, exist_ok=True)
        (ui_dir / "style.css").write_text(
            "body { background: url('./dot.svg'); color: var(--t-fg); }\n",
            encoding="utf-8",
        )
        (ui_dir / "dot.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><circle cx="2" cy="2" r="2"/></svg>',
            encoding="utf-8",
        )
        (ui_dir / "app.js").write_text(
            "console.log('ready'); const tricky = '</script>';\n",
            encoding="utf-8",
        )
        (ui_dir / "data.json").write_text('{"hello": "world"}', encoding="utf-8")
        (ui_dir / "index.html").write_text(
            "<!DOCTYPE html>\n"
            "<html>\n"
            "<head>\n"
            '  <meta charset="utf-8">\n'
            '  <link rel="stylesheet" href="./style.css">\n'
            "</head>\n"
            "<body>\n"
            '  <img src="./dot.svg" alt="dot">\n'
            '  <script src="./app.js"></script>\n'
            "</body>\n"
            "</html>\n",
            encoding="utf-8",
        )
    return pdir


def test_manifest_ui_requires_permission_and_validates_paths(tmp_path):
    pdir = _make_ui_plugin(
        tmp_path,
        "no_perm",
        manifest_yaml="id: no_perm\nname: NoPerm\nversion: '1.0'\nui:\n  dir: ui\n  entry: index.html\n",
    )
    with pytest.raises(PluginManifestError, match="web:ui"):
        parse_manifest(pdir)

    pdir_auto = _make_ui_plugin(
        tmp_path,
        "auto_ui",
        manifest_yaml="id: auto_ui\nname: AutoUI\nversion: '1.0'\npermissions:\n  - web:ui\n",
    )
    manifest = parse_manifest(pdir_auto)
    assert manifest.ui is not None
    assert manifest.ui.dir == "ui"
    assert manifest.ui.entry == "index.html"
    assert manifest.ui.title == "AutoUI"
    assert manifest.ui.placement == "sidebar"

    pdir_bad_ext = _make_ui_plugin(
        tmp_path,
        "bad_ext",
        manifest_yaml=(
            "id: bad_ext\nname: BadExt\nversion: '1.0'\npermissions:\n  - web:ui\n"
            "ui:\n  dir: ui\n  entry: app.js\n"
        ),
    )
    with pytest.raises(PluginManifestError, match="HTML"):
        parse_manifest(pdir_bad_ext)

    pdir_escape = _make_ui_plugin(
        tmp_path,
        "escape_ui",
        manifest_yaml=(
            "id: escape_ui\nname: Escape\nversion: '1.0'\npermissions:\n  - web:ui\n"
            "ui:\n  dir: ..\n  entry: index.html\n"
        ),
    )
    with pytest.raises(PluginManifestError):
        parse_manifest(pdir_escape)


def test_build_plugin_ui_document_inlines_assets_escapes_script_and_injects_csp(tmp_path):
    pdir = _make_ui_plugin(tmp_path, "viz")
    doc = build_plugin_ui_document(pdir / "ui", "index.html")
    assert f'<meta http-equiv="Content-Security-Policy" content="{SANDBOX_CSP}">' in doc
    assert '<style data-plugin-asset="./style.css">' in doc
    assert "data:image/svg+xml;base64," in doc
    assert '<script data-plugin-asset="./app.js">' in doc
    assert r"<\/script>" in doc
    # 不允许残留未转义的 </script> 提前截断脚本体
    assert doc.lower().count("</script>") == 1


def test_build_plugin_ui_document_blocks_asset_traversal(tmp_path):
    pdir = _make_ui_plugin(tmp_path, "escape")
    (pdir / "secret.js").write_text("SECRET=1", encoding="utf-8")
    (pdir / "ui" / "index.html").write_text(
        '<html><head></head><body><script src="../secret.js"></script></body></html>',
        encoding="utf-8",
    )
    with pytest.raises(PluginManifestError, match="越界"):
        build_plugin_ui_document(pdir / "ui", "index.html")


async def test_plugin_ui_web_endpoints_respect_auth_device_gate_and_activation(tmp_path):
    plugins_dir = tmp_path / "plugins"
    _make_ui_plugin(plugins_dir, "viz")
    _make_ui_plugin(plugins_dir, "pending_viz")

    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=_fresh_registry(),
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )
    settings = WebSettings(str(tmp_path / "webui_settings.json"))
    settings.set_password("secret-pass")
    auth_token = settings.issue_token()
    device_token = settings.create_device("test-browser")
    settings.set_device_auth(True)
    settings.deny_new_devices()

    conn = ConnectionManager()
    conn.broadcast = AsyncMock()

    try:
        states = await pm.startup({
            "plugins": {
                "items": {
                    "viz": {"enabled": True, "trusted": True},
                    "pending_viz": {"enabled": False, "trusted": False},
                }
            }
        })
        assert states["viz"].status == "active"
        assert states["viz"].ui is not None
        assert states["pending_viz"].status == "pending"
        assert states["pending_viz"].ui is None

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

        # 验证 ctx.web.emit_ui_event 推送标准 plugin.ui_event 信封
        viz_ctx = pm._active_runtimes["viz"]["ctx"]
        await viz_ctx.web.emit_ui_event("metrics.tick", {"value": 42})
        conn.broadcast.assert_awaited_once_with({
            "type": "plugin.ui_event",
            "data": {"plugin_id": "viz", "event": "metrics.tick", "payload": {"value": 42}},
        })

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            # 1. 无设备 Cookie 被外层 DeviceAccessGate 拦截
            r_closed = await client.get("/api/plugins/ui")
            assert r_closed.status_code == 403

            # 2. 有设备 Cookie 但无 Bearer Token 被 auth_dep 拦截
            client.cookies.set(DEVICE_COOKIE, device_token)
            r_unauth = await client.get("/api/plugins/ui")
            assert r_unauth.status_code == 401
            r_doc_unauth = await client.get("/api/plugins/viz/ui/document")
            assert r_doc_unauth.status_code == 401

            # 3. 完整鉴权后可拉取已激活插件的 UI 清单、文档、资源以及扩展 API
            headers = {"Authorization": f"Bearer {auth_token}"}
            r_list = await client.get("/api/plugins/ui", headers=headers)
            assert r_list.status_code == 200
            items = r_list.json()["items"]
            assert [item["plugin_id"] for item in items] == ["viz"]
            assert items[0]["title"] == "实时看板"
            assert items[0]["has_routes"] is True

            r_doc = await client.get("/api/plugins/viz/ui/document", headers=headers)
            assert r_doc.status_code == 200
            payload = r_doc.json()
            assert payload["plugin_id"] == "viz"
            assert "connect-src 'none'" in payload["html"]
            assert "data:image/svg+xml;base64," in payload["html"]

            r_asset = await client.get("/api/plugins/viz/ui/assets/data.json", headers=headers)
            assert r_asset.status_code == 200
            assert r_asset.headers.get("x-content-type-options") == "nosniff"
            assert r_asset.json() == {"hello": "world"}

            # 路径穿越被拒绝
            r_trav = await client.get("/api/plugins/viz/ui/assets/..%2Fplugin.py", headers=headers)
            assert r_trav.status_code in {400, 404}

            # 未启用插件绝不暴露 UI 文档或资源
            r_pending = await client.get("/api/plugins/pending_viz/ui/document", headers=headers)
            assert r_pending.status_code == 404

            # 扩展 API 与 UI 配合正常
            r_ext = await client.get("/api/plugins/ext/viz/metrics", headers=headers)
            assert r_ext.status_code == 200
            assert r_ext.json() == {"points": [1, 2, 3]}
    finally:
        await pm.shutdown()


async def test_emit_ui_event_requires_web_permission(tmp_path):
    plugins_dir = tmp_path / "plugins"
    _make_ui_plugin(
        plugins_dir,
        "plain",
        manifest_yaml="id: plain\nname: Plain\nversion: '1.0'\nentrypoint: plugin.py:setup\n",
        code="async def setup(ctx):\n    pass\n",
        include_ui=False,
    )
    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=_fresh_registry(),
        command_registry=CommandRegistry(),
        hook_bus=HookBus(),
    )
    try:
        await pm.startup({"plugins": {"items": {"plain": {"enabled": True, "trusted": True}}}})
        ctx = pm._active_runtimes["plain"]["ctx"]
        with pytest.raises(PermissionDeniedError):
            await ctx.web.emit_ui_event("ping", {})
    finally:
        await pm.shutdown()
