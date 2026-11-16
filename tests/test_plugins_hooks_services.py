"""Phase 3 插件生命周期钩子（HookBus）与宿主能力服务权限门控测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.commands import CommandRegistry
from agent.loop import AgentLoop
from agent.prompt import SystemPromptBuilder
from agent.protocol import Context, LoopResult
from plugins.hooks import HookBus
from plugins.manager import PluginManager
from plugins.permissions import PermissionDeniedError
from runtime.goals import GoalManager
from runtime.heartbeat import Heartbeat
from runtime.tool_registry import ToolRegistry


def _fresh_registry() -> ToolRegistry:
    reg = object.__new__(ToolRegistry)
    reg._tools = {}
    return reg


def _write_plugin(root, plugin_id: str, manifest_yaml: str, code: str) -> None:
    pdir = root / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
    (pdir / "plugin.py").write_text(code, encoding="utf-8")


async def test_hook_bus_respects_observe_vs_mutate_and_isolates_errors():
    bus = HookBus()
    observed: list[str] = []

    def boom(_payload):
        raise RuntimeError("hook boom")

    bus.register("before_round", boom, source="plugin:bad", can_mutate=True)
    bus.register(
        "before_round",
        lambda p: (observed.append(p["round_id"]), {"extra_context": "unauthorized"})[1],
        source="plugin:reader",
        can_mutate=False,
    )
    bus.register(
        "before_round",
        lambda p: {"extra_context": "authorized context"},
        source="plugin:writer",
        can_mutate=True,
    )

    notes = await bus.emit_before_round({"round_id": "r1"})
    assert observed == ["r1"]
    assert notes == ["authorized context"]


async def test_agent_loop_before_and_after_tool_hooks_keep_pairing_and_checkpoint():
    bus = HookBus()
    called_args: list[dict] = []

    async def fake_exec(name, args):
        called_args.append({"name": name, "args": args})
        return json.dumps({"ok": True, "val": args.get("x")})

    bus.register(
        "before_tool_call",
        lambda p: (
            {"block": True, "reason": "blocked by policy"}
            if p["tool_name"] == "danger_tool"
            else {"arguments": {"x": p["arguments"]["x"] * 2}}
        ),
        source="plugin:guard",
        can_mutate=True,
    )
    bus.register(
        "after_tool_result",
        lambda p: {"result": p["result"] + "|post"} if p["tool_name"] == "math_tool" else None,
        source="plugin:guard",
        can_mutate=True,
    )

    loop = object.__new__(AgentLoop)
    loop.hook_bus = bus
    loop.tool_executor = SimpleNamespace(execute=fake_exec)
    events = []
    checkpoints = []

    async def cb(ev):
        events.append(ev)

    async def cp(msgs):
        checkpoints.append(list(msgs))

    loop.callback = cb

    from openai.types.chat import ChatCompletionMessage
    from openai.types.chat.chat_completion_message_tool_call import (
        ChatCompletionMessageFunctionToolCall,
        Function,
    )

    msg = ChatCompletionMessage(
        role="assistant",
        content="",
        tool_calls=[
            ChatCompletionMessageFunctionToolCall(
                id="c1",
                type="function",
                function=Function(name="math_tool", arguments='{"x": 5}'),
            ),
            ChatCompletionMessageFunctionToolCall(
                id="c2",
                type="function",
                function=Function(name="danger_tool", arguments='{"x": 1}'),
            ),
        ],
    )
    full_messages = [{"role": "user", "content": "hi"}]
    ctx = Context(
        current_message="hi",
        history_messages=[],
        system_prompt="",
        platform="web",
        session_id="s1",
        round_id="r1",
    )

    await AgentLoop._run_tool_batch(loop, msg, full_messages, ctx, cp)
    assert called_args == [{"name": "math_tool", "args": {"x": 10}}]
    tool_msgs = [m for m in full_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "c1"
    assert tool_msgs[0]["content"].endswith("|post")
    assert tool_msgs[1]["tool_call_id"] == "c2"
    assert "blocked by policy" in tool_msgs[1]["content"]
    assert len(checkpoints) == 1


async def test_heartbeat_before_hook_skip_and_briefing(tmp_path):
    bus = HookBus()
    captured = {}

    class FakeSession:
        _pending_user_rounds = 0

        async def run_round(self, **kwargs):
            captured["injection"] = kwargs["injection_messages"]
            return LoopResult([], "done", {}, stop_reason="complete")

    hb = Heartbeat(
        FakeSession(),
        interval_minutes=30,
        settings={
            "enabled": True,
            "message": "心跳",
            "include_in_progress_tasks": False,
            "session_switch": {"enabled": False, "window_start": "01:00", "window_end": "07:00"},
        },
        state_file=tmp_path / "hb.json",
    )
    hb.hook_bus = bus

    bus.register("before_heartbeat", lambda p: {"briefing": "Unread alert x1"}, source="plugin:brief", can_mutate=True)
    res = await hb._execute_beat(1700000000.0, None, content=None, request_id="r1", before_run=None)
    assert res.stop_reason == "complete"
    assert "[插件心跳简报]\nUnread alert x1" in captured["injection"][0]["content"]

    bus.register("before_heartbeat", lambda p: {"skip": True}, source="plugin:quiet", can_mutate=True)
    res2 = await hb._execute_beat(1700000100.0, None, content=None, request_id="r2", before_run=None)
    assert res2.stop_reason == "skipped"


async def test_gated_host_services_and_end_to_end_plugin_hooks(tmp_path):
    plugins_dir = tmp_path / "plugins"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")

    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    for s in ("soul", "master", "memory", "guild", "situation"):
        (prompt_dir / f"{s}.md").write_text("", encoding="utf-8")
    builder = SystemPromptBuilder(
        prompt_dir=str(prompt_dir),
        skills_dir=str(tmp_path / "skills"),
        preview_dir=str(tmp_path / "tmp"),
    )
    gm = GoalManager(str(tmp_path / "chronicle.db"))

    class FakeMemoryStore:
        def __init__(self):
            self.saved = []

        async def search(self, query, **_kw):
            return {"results": [{"query": query}]}

        async def save_memory(self, *args, **kwargs):
            self.saved.append((args, kwargs))
            return {"ok": True}

    class FakeScheduler:
        async def list_active_schedules(self):
            return [{"id": 1, "name": "demo"}]

        async def get_schedule_detail(self, schedule_id, **_kw):
            return {"id": schedule_id}

        async def update(self, operations):
            return {"applied": len(operations)}

    mem = FakeMemoryStore()
    sched = FakeScheduler()
    bus = HookBus()

    _write_plugin(
        plugins_dir,
        "readonly",
        "id: readonly\nname: Readonly\nversion: '1.0'\nentrypoint: plugin.py:setup\n"
        "permissions:\n  - fs:workspace_read\n  - goals:read\n  - hooks:observe\n",
        (
            "refs = {}\n"
            "def setup(ctx):\n"
            "    refs['ctx'] = ctx\n"
            "    ctx.on('after_round', lambda p: refs.setdefault('rounds', []).append(p['round_id']))\n"
        ),
    )
    _write_plugin(
        plugins_dir,
        "fullpower",
        "id: fullpower\nname: Full\nversion: '1.0'\nentrypoint: plugin.py:setup\n"
        "permissions:\n"
        "  - fs:workspace_read\n  - fs:workspace_write\n"
        "  - memory:read\n  - memory:write\n"
        "  - goals:read\n  - goals:write\n"
        "  - scheduler:read\n  - scheduler:write\n"
        "  - situation:write\n",
        (
            "async def setup(ctx):\n"
            "    ctx.files.write_text('out/hello.txt', 'world')\n"
            "    assert ctx.files.read_text('out/hello.txt') == 'world'\n"
            "    assert ctx.files.file_marker('out/hello.txt') == '[file:out/hello.txt]'\n"
            "    await ctx.goals.update([{'op': 'create_goal', 'description': 'Ship plugins', 'priority': 'high'}])\n"
            "    goals = await ctx.goals.list_active()\n"
            "    assert goals[0]['description'] == 'Ship plugins'\n"
            "    assert (await ctx.memory.search('abc'))['results'][0]['query'] == 'abc'\n"
            "    await ctx.memory.save_memory('fact')\n"
            "    assert len(await ctx.scheduler.list_active()) == 1\n"
            "    assert (await ctx.scheduler.update([{'op': 'create'}]))['applied'] == 1\n"
            "    ctx.situation.write_situation('plugin active')\n"
            "    assert ctx.situation.read_situation() == 'plugin active'\n"
        ),
    )

    pm = PluginManager(
        plugins_dir=plugins_dir,
        data_dir=tmp_path / "data",
        tool_registry=_fresh_registry(),
        command_registry=CommandRegistry(),
        hook_bus=bus,
        services={
            "workspace": workspace,
            "goal_manager": gm,
            "memory_store": mem,
            "scheduler": sched,
            "builder": builder,
        },
    )
    try:
        states = await pm.startup({
            "plugins": {
                "items": {
                    "readonly": {"enabled": True, "trusted": True},
                    "fullpower": {"enabled": True, "trusted": True},
                }
            }
        })
        assert states["readonly"].status == "active"
        assert states["fullpower"].status == "active"
        assert len(mem.saved) == 1

        # 校验只读插件尝试越权操作全部被拦截
        ro_ctx = pm._active_runtimes["readonly"]["ctx"]
        with pytest.raises(PermissionDeniedError, match="fs:workspace_write"):
            ro_ctx.files.write_text("x.txt", "no")
        with pytest.raises(PermissionError):
            ro_ctx.files.read_text(".env")
        with pytest.raises(PermissionDeniedError, match="goals:write"):
            await ro_ctx.goals.update([{"op": "create_goal", "title": "x"}])
        with pytest.raises(PermissionDeniedError, match="memory:read"):
            await ro_ctx.memory.search("q")
        with pytest.raises(PermissionDeniedError, match="scheduler:read"):
            await ro_ctx.scheduler.list_active()
        with pytest.raises(PermissionDeniedError, match="situation:write"):
            ro_ctx.situation.write_situation("x")

        assert bus.has_hooks("after_round")
    finally:
        await pm.shutdown()

    assert not bus.has_hooks("after_round")


async def test_session_before_after_switch_and_daily_compress_hooks(tmp_path, monkeypatch):
    from datetime import timedelta
    from unittest.mock import AsyncMock
    import asyncio
    from agent.session import AgentSession
    from agent.session_manager import SessionManager
    from agent.compressor import Compressor
    from agent.protocol import ModelConfig
    from runtime.timezone import local_datetime

    prev_sm = SessionManager._instance
    SessionManager._instance = None
    bus = HookBus()
    recorded: list[tuple[str, dict]] = []
    bus.register(
        "before_round",
        lambda p: (recorded.append(("before_round", p)), {"extra_context": "ctx-from-plugin"})[1],
        source="plugin:t",
        can_mutate=True,
    )
    bus.register("after_round", lambda p: recorded.append(("after_round", p)), source="plugin:t")
    bus.register("on_session_switch", lambda p: recorded.append(("on_session_switch", p)), source="plugin:t")
    bus.register("on_daily_compress", lambda p: recorded.append(("on_daily_compress", p)), source="plugin:t")

    monkeypatch.setattr(Compressor, "_load_system_prompt", lambda self: "compressor prompt")
    monkeypatch.setattr("agent.session.FileManager", lambda: SimpleNamespace(configured=False))
    try:
        sm = SessionManager(
            SimpleNamespace(_raw_config={
                "providers": {"p": {"type": "openai"}},
                "models": {"m": {"provider": "p", "name": "gpt-4o-mini", "context_window": 100000}},
                "roles": {"chat": {"model": "m"}},
            }),
            db_path=str(tmp_path / "chronicle.db"),
            cache_path=str(tmp_path / "active.json"),
            compression_state_path=str(tmp_path / "compression.json"),
        )
        sm.hook_bus = bus
        monkeypatch.setattr(sm, "_assemble_summary_message", lambda: "summary")

        yesterday = local_datetime().date() - timedelta(days=1)
        sm._last_compressed_date = yesterday - timedelta(days=1)
        monkeypatch.setattr(sm.chronicle, "get_messages_by_time_range", lambda s, e: [{"role": "user", "content": "hi"}])
        sm.compressor.compress_date = AsyncMock(return_value=True)
        assert await sm.maybe_compress_daily() is True
        assert any(kind == "on_daily_compress" and p["last_compressed_date"] == yesterday.isoformat()
                   for kind, p in recorded)

        prompt_dir = tmp_path / "prompt"
        prompt_dir.mkdir()
        for s in ("soul", "master", "memory", "guild", "situation"):
            (prompt_dir / f"{s}.md").write_text("", encoding="utf-8")
        builder = SystemPromptBuilder(str(prompt_dir), str(tmp_path / "skills"), str(tmp_path / "preview"))

        captured_messages = []

        async def fake_loop_run(full_messages, ctx):
            captured_messages.extend(full_messages)
            return LoopResult([*full_messages, {"role": "assistant", "content": "ok"}], "ok", {}, stop_reason="complete")

        agent = AgentSession.__new__(AgentSession)
        agent._lock = asyncio.Lock()
        agent._pending_user_rounds = 0
        agent.role = "chat"
        agent.platform = "web"
        agent.callback = AsyncMock()
        agent.chronicle = sm.chronicle
        agent.session_manager = sm
        agent._system_prompt_builder = builder
        agent._system_prompt = builder.build()
        agent._rollback_state_path = tmp_path / "rollback.json"
        agent.manager = SimpleNamespace(get_model=lambda role: None)
        agent._loop = SimpleNamespace(
            run=fake_loop_run,
            client=SimpleNamespace(context_cache=None),
            model_config=ModelConfig("synthetic", "https://model.test", provider="deepseek", tool_names=[]),
        )
        agent._ensure_loop = lambda: agent._loop
        agent.hook_bus = bus
        agent.heartbeat = None

        res = await agent.run_round("hello")
        assert res.stop_reason == "complete"
        assert "[插件上下文]\nctx-from-plugin" in captured_messages[-1]["content"]
        assert any(kind == "after_round" and p["final_answer"] == "ok" for kind, p in recorded)

        new_sid = await agent.switch_session()
        assert any(kind == "on_session_switch" and p["new_session_id"] == new_sid for kind, p in recorded)
    finally:
        SessionManager._instance = prev_sm
