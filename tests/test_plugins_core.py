"""Phase 1 插件核心引擎测试：Manifest、权限门控、工具/命令/Skills/Prompt 与故障降级。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.commands import CommandRegistry, command_catalog, command_registry, parse_command, run_command
from agent.prompt import SystemPromptBuilder
from plugins.manager import PluginManager
from plugins.manifest import parse_manifest, qualify_tool_name
from plugins.permissions import PermissionDeniedError, PluginManifestError
from runtime.tool_registry import ToolRegistry
from tools import skills as skills_tool


def _fresh_registry() -> ToolRegistry:
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    return reg


def _write_plugin(
    root: Path,
    plugin_id: str,
    manifest_yaml: str,
    files: dict[str, str] | None = None,
) -> Path:
    pdir = root / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
    for rel, content in (files or {}).items():
        target = pdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return pdir


def test_qualify_tool_name_enforces_length_and_charset():
    short = qualify_tool_name("p", "weather", "get_forecast")
    assert short == "p_weather__get_forecast"

    long_raw = "very.long.tool.name:" + ("x" * 80)
    qualified = qualify_tool_name("mcp", "my-long-server-id", long_raw)
    assert len(qualified) <= 64
    assert qualified.startswith("mcp_my-long-server-id__")


def test_manifest_validation_rejects_bad_declarations_and_traversal(tmp_path):
    p_dir = _write_plugin(
        tmp_path,
        "good-id",
        "id: other-id\nname: Bad\nversion: 1.0\n",
    )
    with pytest.raises(PluginManifestError, match="不一致"):
        parse_manifest(p_dir)

    p_bad_perm = _write_plugin(
        tmp_path,
        "bad-perm",
        "id: bad-perm\nname: Bad\nversion: 1.0\npermissions:\n  - admin:godmode\n",
    )
    with pytest.raises(PluginManifestError, match="未知插件权限"):
        parse_manifest(p_bad_perm)

    p_trav = _write_plugin(
        tmp_path,
        "bad-entry",
        "id: bad-entry\nname: Bad\nversion: 1.0\nentrypoint: ../outside.py:setup\n",
    )
    (tmp_path / "outside.py").write_text("def setup(ctx): pass\n", encoding="utf-8")
    with pytest.raises(PluginManifestError, match="越界"):
        parse_manifest(p_trav)

    p_unpermitted_skill = _write_plugin(
        tmp_path,
        "no-skill-perm",
        "id: no-skill-perm\nname: Bad\nversion: 1.0\nskills_dir: skills\n",
        {"skills/demo/SKILL.md": "---\nname: demo\ndescription: hi\n---\n"},
    )
    with pytest.raises(PluginManifestError, match="skills:provide"):
        parse_manifest(p_unpermitted_skill)


async def test_pending_and_disabled_plugins_never_execute_code(tmp_path):
    plugins_dir = tmp_path / "plugins"
    marker = tmp_path / "executed.txt"
    _write_plugin(
        plugins_dir,
        "demo",
        "id: demo\nname: Demo\nversion: '1.0'\nentrypoint: plugin.py:setup\npermissions:\n  - tools:register\n",
        {"plugin.py": f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\ndef setup(ctx):\n    pass\n"},
    )
    reg = _fresh_registry()
    cmd_reg = CommandRegistry()
    pm = PluginManager(plugins_dir=plugins_dir, data_dir=tmp_path / "data", tool_registry=reg, command_registry=cmd_reg)
    try:
        states = await pm.startup({"plugins": {"items": {}, "mcp": {}}})
        assert states["demo"].status == "pending"
        assert not marker.exists()
        assert reg._tools == {}

        states = await pm.startup({"plugins": {"items": {"demo": {"enabled": False, "trusted": True}}}})
        assert states["demo"].status == "disabled"
        assert not marker.exists()
    finally:
        await pm.shutdown()


async def test_plugin_isolation_degrades_single_failure_and_preserves_healthy(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    monkeypatch.setenv("DEMO_SECRET_KEY", "super-secret-token-value")

    _write_plugin(
        plugins_dir,
        "broken",
        "id: broken\nname: Broken\nversion: '1.0'\nentrypoint: plugin.py:setup\n"
        "requires_env:\n  - DEMO_SECRET_KEY\n"
        "permissions:\n  - tools:register\n  - commands:register\n",
        {"plugin.py": (
            "def setup(ctx):\n"
            "    ctx.register_tool('partial', 'desc', {'type': 'object'}, lambda args: 'ok')\n"
            "    ctx.register_command('partialcmd', 'desc', lambda args, s: 'ok')\n"
            "    raise RuntimeError('boom with ' + ctx.get_env('DEMO_SECRET_KEY'))\n"
        )},
    )
    _write_plugin(
        plugins_dir,
        "healthy",
        "id: healthy\nname: Healthy\nversion: '1.0'\nentrypoint: plugin.py:setup\n"
        "permissions:\n  - tools:register\n  - commands:register\n  - fs:plugin_data\n"
        "config_schema:\n  greeting:\n    type: string\n    label: 问候语\n    default: hello\n",
        {"plugin.py": (
            "state = {}\n"
            "async def setup(ctx):\n"
            "    p = ctx.data_path('cache.txt')\n"
            "    p.write_text(ctx.config['greeting'], encoding='utf-8')\n"
            "    ctx.register_tool('greet', 'greet user', {'type': 'object'}, lambda args: p.read_text(encoding='utf-8'))\n"
            "    ctx.register_tool('secret_admin', 'blocked', {'type': 'object'}, lambda args: 'no')\n"
            "    ctx.register_command('ping', 'ping pong', lambda args, s: f'pong:{args}')\n"
            "async def teardown(ctx):\n"
            "    state['torn_down'] = True\n"
        )},
    )

    reg = _fresh_registry()
    cmd_reg = CommandRegistry()
    pm = PluginManager(plugins_dir=plugins_dir, data_dir=tmp_path / "data", tool_registry=reg, command_registry=cmd_reg)
    try:
        states = await pm.startup({
            "plugins": {
                "items": {
                    "broken": {"enabled": True, "trusted": True},
                    "healthy": {"enabled": True, "trusted": True, "disabled_tools": ["secret_admin"]},
                }
            }
        })
        assert states["broken"].status == "degraded"
        assert "super-secret-token-value" not in (states["broken"].error or "")
        assert "***" in (states["broken"].error or "")
        assert not reg.has("p_broken__partial")
        assert cmd_reg.get("partialcmd") is None

        assert states["healthy"].status == "active"
        assert reg.has("p_healthy__greet")
        assert not reg.has("p_healthy__secret_admin")
        assert len(states["healthy"].tool_records) == 2
        assert reg.resolve_tool_names(["send_message"], role="chat") == ["send_message", "p_healthy__greet"]
        assert reg.resolve_tool_names([], role="compress") == []
        assert reg.resolve_tool_names(None, role="chat") is None
        assert await reg.execute("p_healthy__greet", {}) == "hello"

        # 验证命令已集成到传入的 CommandRegistry，并禁止覆盖内置命令
        with pytest.raises(ValueError, match="内置命令"):
            cmd_reg.register("help", "bad", lambda args, s: "no")
        assert cmd_reg.get("ping") is not None
    finally:
        await pm.shutdown()
    assert reg._tools == {}
    assert cmd_reg.catalog() == []


async def test_missing_required_env_marks_plugin_degraded_without_running_setup(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_PLUGIN_TOKEN", raising=False)
    plugins_dir = tmp_path / "plugins"
    marker = tmp_path / "should_not_run.txt"
    _write_plugin(
        plugins_dir,
        "needenv",
        "id: needenv\nname: NeedEnv\nversion: '1.0'\nentrypoint: plugin.py:setup\nrequires_env:\n  - MISSING_PLUGIN_TOKEN\n",
        {"plugin.py": f"from pathlib import Path\ndef setup(ctx):\n    Path({str(marker)!r}).write_text('ran')\n"},
    )
    pm = PluginManager(plugins_dir=plugins_dir, data_dir=tmp_path / "data", tool_registry=_fresh_registry(), command_registry=CommandRegistry())
    try:
        states = await pm.startup({"plugins": {"items": {"needenv": {"enabled": True, "trusted": True}}}})
        assert states["needenv"].status == "degraded"
        assert states["needenv"].missing_env == ["MISSING_PLUGIN_TOKEN"]
        assert not marker.exists()
    finally:
        await pm.shutdown()


async def test_permission_gates_env_and_data_dir_and_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HOST_MASTER_KEY", "top-secret-host-key")
    monkeypatch.setenv("PLUGIN_OWN_KEY", "plugin-secret")
    plugins_dir = tmp_path / "plugins"
    _write_plugin(
        plugins_dir,
        "guarded",
        "id: guarded\nname: Guarded\nversion: '1.0'\nentrypoint: plugin.py:setup\nrequires_env:\n  - PLUGIN_OWN_KEY\n",
        {"plugin.py": (
            "def setup(ctx):\n"
            "    ctx.data_path('a.txt')\n"
        )},
    )
    _write_plugin(
        plugins_dir,
        "envguard",
        "id: envguard\nname: EnvGuard\nversion: '1.0'\nentrypoint: plugin.py:setup\nrequires_env:\n  - PLUGIN_OWN_KEY\n",
        {"plugin.py": (
            "def setup(ctx):\n"
            "    assert ctx.get_env('PLUGIN_OWN_KEY') == 'plugin-secret'\n"
            "    ctx.get_env('HOST_MASTER_KEY')\n"
        )},
    )
    with pytest.raises(PermissionDeniedError, match="tools:register"):
        from plugins.context import PluginContext
        fake_manifest = parse_manifest(_write_plugin(
            tmp_path / "permcheck",
            "onlycmd",
            "id: onlycmd\nname: OnlyCmd\nversion: '1.0'\npermissions:\n  - commands:register\n",
        ))
        PluginContext(
            manifest=fake_manifest,
            plugin_dir=tmp_path / "permcheck" / "onlycmd",
            data_dir=tmp_path / "data" / "onlycmd",
            config={},
            disabled_tools=set(),
            tool_registry=_fresh_registry(),
            command_registry=CommandRegistry(),
        ).register_tool("x", "d", {"type": "object"}, lambda a: a)

    pm = PluginManager(plugins_dir=plugins_dir, data_dir=tmp_path / "data", tool_registry=_fresh_registry(), command_registry=CommandRegistry())
    try:
        states = await pm.startup({
            "plugins": {
                "items": {
                    "guarded": {"enabled": True, "trusted": True},
                    "envguard": {"enabled": True, "trusted": True},
                }
            }
        })
        assert states["guarded"].status == "degraded"
        assert "fs:plugin_data" in (states["guarded"].error or "")
        assert states["envguard"].status == "degraded"
        assert "HOST_MASTER_KEY" in (states["envguard"].error or "")
    finally:
        await pm.shutdown()


async def test_plugin_skills_prompts_and_global_commands_integration(tmp_path, monkeypatch):
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    for name in ("soul", "master", "memory", "guild", "situation"):
        (prompt_dir / f"{name}.md").write_text(f"# {name}\nbase-{name}\n", encoding="utf-8")
    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    monkeypatch.setattr(skills_tool, "_skills_dir", lambda: user_skills)

    plugins_dir = tmp_path / "plugins"
    _write_plugin(
        plugins_dir,
        "notes",
        "id: notes\nname: Notes\nversion: '1.0'\nentrypoint: plugin.py:setup\n"
        "permissions:\n  - skills:provide\n  - prompts:append\n  - commands:register\n",
        {
            "plugin.py": "def setup(ctx):\n    ctx.register_command('note', 'save quick note', lambda args, s: f'saved:{args}')\n",
            "prompts/guild.md": "## Notes 插件\n使用 /note 命令快速记录灵感。\n",
            "skills/quick-note/SKILL.md": "---\nname: quick-note\ncategory: notes\ndescription: 快速记录灵感\n---\n# Quick Note\nBody\n",
            "skills/quick-note/references/tips.md": "tip-content\n",
        },
    )

    builder = SystemPromptBuilder(
        prompt_dir=str(prompt_dir),
        skills_dir=str(user_skills),
        preview_dir=str(tmp_path / "tmp"),
    )
    reg = _fresh_registry()
    pm = PluginManager(plugins_dir=plugins_dir, data_dir=tmp_path / "data", tool_registry=reg, command_registry=command_registry)
    pm.bind_extensions(builder)
    try:
        states = await pm.startup({"plugins": {"items": {"notes": {"enabled": True, "trusted": True}}}})
        assert states["notes"].status == "active"

        prompt = builder.build(force=True)
        assert "使用 /note 命令快速记录灵感" in prompt
        assert "quick-note: 快速记录灵感" in prompt

        listing = skills_tool.skills_list({})
        assert "quick-note (notes): 快速记录灵感" in listing
        detail = skills_tool.skill_view({"name": "quick-note"})
        assert "# Quick Note" in detail
        assert "references/tips.md" in detail
        assert skills_tool.skill_view({"name": "quick-note", "file_path": "references/tips.md"}) == "tip-content\n"

        parsed = parse_command("/note Hello")
        assert parsed == "note Hello"
        assert await run_command(parsed, SimpleNamespace()) == "saved:Hello"
        assert {"value": "/note", "description": "save quick note"} in command_catalog()
        help_text = await run_command("help", SimpleNamespace())
        assert "/note" in help_text
    finally:
        await pm.shutdown()

    assert command_registry.get("note") is None
    assert skills_tool.skills_list({}) == "当前没有可用的 skills。"
    assert "使用 /note 命令快速记录灵感" not in builder.build(force=True)


async def test_docs_plugin_example_template_validates_and_runs(tmp_path):
    import json
    import shutil
    from plugins.hooks import HookBus

    example_src = Path(__file__).resolve().parents[1] / "docs" / "plugin-example"
    plugins_dir = tmp_path / "plugins"
    shutil.copytree(example_src, plugins_dir / "example-helper")

    reg = _fresh_registry()
    cmd_reg = CommandRegistry()
    bus = HookBus()
    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=reg,
        command_registry=cmd_reg,
        hook_bus=bus,
    )
    try:
        states = await pm.startup({"plugins": {"items": {"example-helper": {"enabled": True, "trusted": True}}}})
        st = states["example-helper"]
        assert st.status == "active"
        assert reg.has("p_example-helper__echo")
        res = json.loads(await reg.execute("p_example-helper__echo", {"message": "hi"}))
        assert res == {"reply": "[example] hi", "count": 1}
        assert cmd_reg.get("example") is not None
        assert st.skills == ["example-helper"]
        assert st.has_prompt is True
        assert bus.has_hooks("after_round")
    finally:
        await pm.shutdown()
