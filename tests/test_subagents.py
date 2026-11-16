"""子任务生命周期测试：临时 SQLite、模拟 DSH，不读取私人资料、不联网。"""

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.protocol import Context
from subagents.dsh import DshCleanupError
from subagents.config import SubagentConfig
from subagents.manager import SubagentManager


@pytest.fixture
def harness(tmp_path):
    config = SubagentConfig(enabled=True, db_path=tmp_path / "tasks.db",
                            workspace_root=tmp_path / "workspace", dsh_home=tmp_path / "dsh",
                            wait_timeout_seconds=3)
    project = config.workspace_root / "project"
    project.mkdir(parents=True)
    calls = []
    scripts = []
    closed = []

    class FakeRunner:
        async def run(self, workspace, session_id, message, *, save_session):
            calls.append((workspace, session_id, message))
            try:
                if session_id is None:
                    await save_session(f"session-{len(calls)}")
                script = scripts.pop(0) if scripts else None
                if script is not None:
                    return await script()
                return "end_turn", "verified result", {"context_tokens": 12}
            finally:
                closed.append(workspace)

    runner = FakeRunner()
    manager = SubagentManager(config, runner=runner)
    return SimpleNamespace(manager=manager, config=config, calls=calls, scripts=scripts,
                           closed=closed, runner=runner, project=project)


async def eventually(condition):
    async with asyncio.timeout(3):
        while not await condition():
            await asyncio.sleep(0.01)


async def test_blocking_result_is_not_notified_and_context_is_independent(harness):
    notifications = []

    async def notify(event, claim):
        if await claim():
            notifications.append(event)
            return True
        return False

    manager = harness.manager
    await manager.start(notify)
    try:
        result = await manager.create("inspect", cwd="project", background="selected background", wait=True)
        assert result["status"] == "completed"
        assert result["result"] == "verified result"
        assert notifications == []
        workspace, session_id, message = harness.calls[0]
        assert session_id is None
        assert message == "inspect\n\n相关背景：\nselected background"
        task = await manager.view(result["task_id"], include_history=True)
        assert task["history"] == []
        assert harness.closed == [workspace]
        assert task["turns"][0]["delivery"] == "delivered"
    finally:
        await manager.stop()


async def test_background_completion_notifies_once(harness):
    notifications = []

    async def notify(event, claim):
        if await claim():
            notifications.append(event)
            return True
        return False

    manager = harness.manager
    await manager.start(notify)
    try:
        task = await manager.create("background inspect", cwd="project")
        assert task["background"] is True
        async def notified():
            return len(notifications) == 1
        await eventually(notified)
        manager._wake.set()
        await asyncio.sleep(0.03)
        assert len(notifications) == 1
        assert notifications[0]["task_id"] == task["task_id"]
        assert notifications[0]["result"] == "verified result"
    finally:
        await manager.stop()


async def test_wait_timeout_leaves_task_running_and_later_notifies(harness):
    harness.config.wait_timeout_seconds = 0.1  # Only this test exercises the short foreground wait.
    release = asyncio.Event()
    notifications = []

    async def delayed():
        await release.wait()
        return "end_turn", "late result", {}

    async def notify(event, claim):
        if await claim():
            notifications.append(event)
            return True
        return False

    harness.scripts.append(delayed)
    manager = harness.manager
    await manager.start(notify)
    try:
        result = await manager.create("inspect", cwd="project", wait=True)
        assert result["wait_timed_out"] is True
        assert result["status"] == "running"
        assert harness.closed == []
        release.set()
        async def notified():
            return len(notifications) == 1
        await eventually(notified)
        assert notifications[0]["result"] == "late result"
    finally:
        await manager.stop()


async def test_followup_uses_same_acp_session_and_workspace(harness):
    manager = harness.manager
    await manager.start()
    try:
        first = await manager.create("first question", cwd="project", wait=True)
        second = await manager.followup(first["task_id"], "follow-up question", wait=True)
        assert second["status"] == "completed"
        workspace, _, _ = harness.calls[0]
        assert workspace == harness.project
        assert harness.calls[1] == (workspace, "session-1", "follow-up question")
        assert harness.closed == [workspace, workspace]
    finally:
        await manager.stop()


async def test_followup_while_running_is_serial(harness):
    release = asyncio.Event()
    entered = asyncio.Event()

    async def delayed():
        entered.set()
        await release.wait()
        return "end_turn", "first result", {}

    harness.scripts.append(delayed)
    manager = harness.manager
    await manager.start()
    try:
        first = await manager.create("first", cwd="project")
        await entered.wait()
        second = await manager.followup(first["task_id"], "second")
        assert len(harness.calls) == 1
        release.set()
        async def completed():
            task = await manager.view(first["task_id"])
            return task["status"] == "completed" and len(task["turns"]) == 2
        await eventually(completed)
        assert second["turn_id"] > first["turn_id"]
        assert harness.calls[1][2] == "second"
    finally:
        await manager.stop()


async def test_global_concurrency_limit(harness):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed():
        entered.set()
        await release.wait()
        return "end_turn", "one", {}

    harness.scripts.append(delayed)
    manager = harness.manager
    await manager.start()
    try:
        await manager.create("one", cwd="project")
        await entered.wait()
        (harness.config.workspace_root / "other").mkdir()
        two = await manager.create("two", cwd="other")
        await asyncio.sleep(0.03)
        assert len(harness.calls) == 1
        assert (await manager.view(two["task_id"]))["status"] == "queued"
        release.set()
        async def completed():
            return (await manager.view(two["task_id"]))["status"] == "completed"
        await eventually(completed)
    finally:
        await manager.stop()


async def test_cancel_queued_followups_and_running_dsh(harness):
    entered = asyncio.Event()

    async def delayed():
        entered.set()
        await asyncio.Event().wait()

    harness.scripts.append(delayed)
    manager = harness.manager
    await manager.start()
    try:
        first = await manager.create("one", cwd="project")
        await entered.wait()
        await manager.followup(first["task_id"], "two")
        await manager.cancel(first["task_id"])
        task = await manager.view(first["task_id"])
        assert task["status"] == "cancelled"
        assert all(turn["status"] == "cancelled" for turn in task["turns"])
        assert len(harness.closed) == 1
        continued = await manager.followup(first["task_id"], "explicit continue", wait=True)
        assert continued["status"] == "completed"
    finally:
        await manager.stop()


async def test_archived_task_cannot_be_reopened(harness):
    manager = harness.manager
    await manager.start()
    try:
        first = await manager.create("one", cwd="project", wait=True)
        await manager.cancel(first["task_id"], archive=True)
        with pytest.raises(ValueError, match="归档"):
            await manager.followup(first["task_id"], "reopen")
        assert (await manager.view())["tasks"] == []
        assert (await manager.view(include_archived=True))["tasks"][0]["archived"] == 1
    finally:
        await manager.stop()


async def test_restart_marks_interrupted_and_does_not_replay(harness):
    entered = asyncio.Event()

    async def delayed():
        entered.set()
        await asyncio.Event().wait()

    harness.scripts.append(delayed)
    manager = harness.manager
    await manager.start()
    task = await manager.create("one", cwd="project")
    await entered.wait()
    await manager.stop()
    restarted = SubagentManager(harness.config, runner=harness.runner)
    await restarted.start()
    try:
        stored = await restarted.view(task["task_id"], include_history=True)
        assert stored["status"] == "interrupted"
        assert stored["turns"][0]["message"] == "one"
        assert len(harness.calls) == 1
        result = await restarted.followup(task["task_id"], "continue", wait=True)
        assert result["status"] == "completed"
    finally:
        await restarted.stop()


async def test_workspace_resolves_absolute_and_relative_paths_and_rejects_escapes(harness):
    manager = harness.manager
    root = harness.config.workspace_root
    outside = root.with_name("workspace-other")
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    (root / "inside").symlink_to(harness.project, target_is_directory=True)
    (root / "file").touch()
    await manager.start()
    try:
        for cwd in ("", ".", "missing", "file", "escape", "../workspace-other", str(outside)):
            with pytest.raises(ValueError, match="cwd"):
                await manager.create("one", cwd=cwd)
        assert harness.calls == []
        assert (await manager.view())["tasks"] == []
        for cwd in ("project", str(harness.project), "inside"):
            assert (await manager.create("one", cwd=cwd, wait=True))["status"] == "completed"
        assert [workspace for workspace, _, _ in harness.calls] == [harness.project] * 3
    finally:
        await manager.stop()


async def test_overlapping_workspaces_wait_and_queued_path_is_revalidated(harness):
    harness.config.max_concurrency = 2
    subdir = harness.project / "subdir"
    subdir.mkdir()
    (harness.config.workspace_root / "other").mkdir()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed():
        entered.set()
        await release.wait()
        return "end_turn", "finished", {}

    harness.scripts.append(delayed)
    manager = harness.manager
    await manager.start()
    try:
        await manager.create("one", cwd="project")
        await entered.wait()
        two = await manager.create("two", cwd=str(subdir))
        assert (await manager.create("three", cwd="other", wait=True))["status"] == "completed"
        assert (await manager.view(two["task_id"]))["status"] == "queued"
        subdir.rmdir()
        subdir.symlink_to(harness.config.workspace_root / "other", target_is_directory=True)
        release.set()

        async def failed():
            return (await manager.view(two["task_id"]))["status"] == "failed"

        await eventually(failed)
        assert "工作区已变更" in (await manager.view(two["task_id"]))["turns"][-1]["error"]
        assert [message for _, _, message in harness.calls] == ["one", "three"]
    finally:
        await manager.stop()


async def test_turn_runtime_limit_closes_worker_and_retains_session(harness):
    harness.config.max_runtime_seconds = 0.05
    harness.config.wait_timeout_seconds = 3  # Test runtime expiry, not the separate foreground wait race.

    async def delayed():
        await asyncio.Event().wait()

    harness.scripts.append(delayed)
    manager = harness.manager
    await manager.start()
    try:
        result = await manager.create("one", cwd="project", wait=True)
        assert result["status"] == "failed"
        assert "总时限" in result["error"]
        assert len(harness.closed) == 1
        task = await manager.view(result["task_id"], include_history=True)
        assert task["turns"][0]["message"] == "one"
        resumed = await manager.followup(result["task_id"], "continue", wait=True)
        assert resumed["status"] == "completed"
        assert harness.calls[1][1] == "session-1"
    finally:
        await manager.stop()


async def test_queue_limit_and_task_turn_limit(harness):
    harness.config.max_pending_turns = 1
    harness.config.max_turns_per_task = 1
    entered = asyncio.Event()

    async def delayed():
        entered.set()
        await asyncio.Event().wait()

    harness.scripts.append(delayed)
    manager = harness.manager
    await manager.start()
    try:
        first = await manager.create("one", cwd="project")
        await entered.wait()
        with pytest.raises(ValueError, match="上限"):
            await manager.create("two", cwd="project")
        await manager.cancel(first["task_id"])
        with pytest.raises(ValueError, match="轮次数"):
            await manager.followup(first["task_id"], "again")
    finally:
        await manager.stop()


async def test_cancel_pending_notification_before_main_lock_is_acquired(harness):
    entered = asyncio.Event()
    release = asyncio.Event()
    notified = []

    async def notify(event, claim):
        entered.set()
        await release.wait()
        if await claim():
            notified.append(event)
            return True
        return False

    manager = harness.manager
    await manager.start(notify)
    try:
        task = await manager.create("one", cwd="project")
        await entered.wait()
        await manager.cancel(task["task_id"])
        release.set()
        await asyncio.sleep(0.03)
        assert notified == []
    finally:
        await manager.stop()


async def test_create_tool_passes_only_summarized_background(monkeypatch):
    import tools.subagent as tools
    from agent.loop import current_tool_context

    manager = SimpleNamespace(create=AsyncMock(return_value={"task_id": "one"}))
    monkeypatch.setattr(tools, "_manager", manager)
    context = Context("new", [], "private personality", "web", session_id="parent")
    messages = [{"role": "system", "content": "private personality"},
                {"role": "user", "content": "selected message"},
                {"role": "tool", "content": "internal tool result"}]
    token = current_tool_context.set((context, messages))
    try:
        await tools.subagent_create({"objective": "one", "cwd": "project"})
        assert manager.create.call_args.kwargs["background"] == ""
        assert manager.create.call_args.kwargs["cwd"] == "project"
        with pytest.raises(ValueError, match="概述"):
            await tools.subagent_create({"objective": "one", "include_recent_messages": 1})
        await tools.subagent_create({"objective": "one", "cwd": "project", "background": "selected technical context"})
        assert manager.create.call_args.kwargs["background"] == "selected technical context"
    finally:
        current_tool_context.reset(token)
    with pytest.raises(PermissionError):
        await tools.subagent_create({"objective": "no parent"})


async def test_model_view_is_compact_and_details_remain_available(harness, monkeypatch):
    from tools import subagent as tools

    manager = harness.manager
    monkeypatch.setattr(tools, "_manager", manager)
    await manager.start()
    try:
        created = await manager.create("synthetic objective", cwd="project", background="context " * 100, wait=True)
        task_id = created["task_id"]
        await manager.followup(task_id, "synthetic followup", wait=True)
        full = await manager.view(task_id, include_history=True)
        compact = json.loads(await tools.subagent_view({"task_id": task_id}))
        assert compact["status"] == "completed"
        assert compact["objective"] == "synthetic objective"
        assert compact["has_more_turns"] is True
        assert len(compact["turns"]) == 1
        assert compact["turns"][0]["result"] == "verified result"
        assert compact["turns"][0]["id"] == full["turns"][-1]["id"]
        assert not {"background", "history", "platform", "parent_session", "created_at", "updated_at"} & compact.keys()
        assert not {"message", "usage", "delivery", "wake_depth", "created_at", "finished_at"} & compact["turns"][0].keys()
        assert len(json.dumps(compact)) < len(json.dumps(full)) / 2

        expanded = json.loads(await tools.subagent_view({
            "task_id": task_id, "include_history": True, "include_usage": True, "limit": 2,
        }))
        assert expanded["history"] == full["history"]
        assert [turn["id"] for turn in expanded["turns"]] == [turn["id"] for turn in full["turns"]]
        assert expanded["turns"][-1]["usage"] == full["turns"][-1]["usage"]
        assert "has_more_turns" not in expanded

        listed = json.loads(await tools.subagent_view({}))["tasks"][0]
        assert listed["id"] == task_id
        assert "created_at" not in listed and "updated_at" not in listed
        # 内部查询继续保留完整记录及投递/用量信息。
        assert (await manager.view(task_id, include_history=True)) == full
    finally:
        await manager.stop()


async def test_compact_view_preserves_failure_and_cleanup_errors(harness):
    manager = harness.manager
    await manager.start()
    try:
        created = await manager.create("synthetic task", cwd="project", wait=True)
        task_id = created["task_id"]

        def fail(conn):
            conn.execute("UPDATE tasks SET status='failed',cleanup_error='cleanup failed' WHERE id=?", (task_id,))
            conn.execute("UPDATE turns SET status='failed',error='execution failed' WHERE task_id=?", (task_id,))

        await manager._db(fail)
        result = await manager.view(task_id, compact=True, limit=1)
        assert result["cleanup_error"] == "cleanup failed"
        assert result["turns"][0]["error"] == "execution failed"
        assert result["status"] == "failed"
    finally:
        await manager.stop()


async def test_model_wait_receipt_keeps_result_and_timeout_state(monkeypatch):
    from agent.loop import current_tool_context
    from tools import subagent as tools

    raw = {"id": 12, "task_id": "synthetic", "turn_id": 12, "status": "running", "result": "partial result",
           "background": True, "wait_timed_out": True, "error": "synthetic error",
           "message": "input already in context", "usage": {"total_tokens": 12}, "delivery": "waiting",
           "created_at": 123, "wake_depth": 0}
    manager = SimpleNamespace(create=AsyncMock(return_value=raw), followup=AsyncMock(return_value=raw))
    monkeypatch.setattr(tools, "_manager", manager)
    token = current_tool_context.set((Context("new", [], "", "web"), []))
    try:
        for call, args in [(tools.subagent_create, {"objective": "inspect", "wait": True}),
                           (tools.subagent_followup, {"task_id": "synthetic", "message": "continue", "wait": True})]:
            result = json.loads(await call(args))
            assert result["result"] == "partial result" and result["error"] == "synthetic error"
            assert result["background"] is True and result["wait_timed_out"] is True
            assert not {"message", "usage", "delivery", "created_at", "wake_depth", "id"} & result.keys()
        assert raw["usage"]["total_tokens"] == 12  # 投影不修改 manager 原记录。
    finally:
        current_tool_context.reset(token)


async def test_cleanup_failure_blocks_new_execution_and_queued_followups(harness):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed():
        entered.set()
        await release.wait()
        raise DshCleanupError("synthetic orphan process")

    manager = harness.manager
    harness.scripts.append(delayed)
    await manager.start()
    try:
        first = await manager.create("one", cwd="project")
        await entered.wait()
        await manager.followup(first["task_id"], "queued followup")
        release.set()

        async def failed():
            return (await manager.view(first["task_id"]))["status"] == "failed"

        await eventually(failed)
        task = await manager.view(first["task_id"])
        assert task["cleanup_error"] == "synthetic orphan process"
        assert task["turns"][1]["status"] == "failed"
        assert len(harness.calls) == 1
        with pytest.raises(RuntimeError, match="暂停新执行"):
            await manager.create("another", cwd="project")
        assert (await manager.view())["execution_blocked"] is True
    finally:
        await manager.stop()


async def test_automatic_notification_depth_is_bounded_but_user_can_continue(harness):
    harness.config.max_notification_depth = 2
    manager = harness.manager
    await manager.start()
    try:
        first = await manager.create("one", cwd="project", wait=True, wake_depth=1)
        with pytest.raises(ValueError, match="委派链"):
            await manager.followup(first["task_id"], "automatic again", wake_depth=2)
        second = await manager.followup(first["task_id"], "user request", wait=True, wake_depth=0)
        assert second["status"] == "completed"
    finally:
        await manager.stop()


async def test_restart_does_not_duplicate_uncertain_notification(harness):
    claimed = asyncio.Event()

    async def notify(event, claim):
        assert await claim()
        claimed.set()
        await asyncio.Event().wait()

    manager = harness.manager
    await manager.start(notify)
    first = await manager.create("one", cwd="project")
    await claimed.wait()
    await manager.stop()
    second_notify = AsyncMock()
    restarted = SubagentManager(harness.config, runner=harness.runner)
    await restarted.start(second_notify)
    try:
        task = await restarted.view(first["task_id"])
        assert task["turns"][0]["result"] == "verified result"
        assert task["turns"][0]["delivery"] == "delivery_unknown"
        await asyncio.sleep(0.02)
        second_notify.assert_not_awaited()
    finally:
        await restarted.stop()


async def test_legacy_tasks_remain_visible_without_forwarding_old_history(harness):
    task_id = "a" * 32
    with sqlite3.connect(harness.config.db_path) as conn:
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, objective TEXT, background TEXT, platform TEXT, parent_session TEXT,
                status TEXT, archived INTEGER DEFAULT 0, history TEXT DEFAULT '[]', progress TEXT DEFAULT '',
                cleanup_error TEXT, created_at REAL, updated_at REAL
            );
            INSERT INTO tasks VALUES (
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'old objective', '', 'web', NULL,
                'completed', 0, '[]', 'old progress', 'old cleanup error', 1, 1
            );
            PRAGMA user_version=1;
        """)
    manager = harness.manager
    await manager.start()
    try:
        for compact in (False, True):
            detail = await manager.view(task_id, compact=compact)
            assert detail["objective"] == "old objective"
            assert "progress" not in detail
            assert "progress" not in (await manager.view(compact=compact))["tasks"][0]
        with pytest.raises(ValueError, match="旧版只读"):
            await manager.followup(task_id, "continue")
        assert harness.calls == []
        assert (await manager.create("new task", cwd="project", wait=True))["status"] == "completed"
    finally:
        await manager.stop()


def test_default_coding_is_opt_in(tmp_path):
    import yaml

    raw = yaml.safe_load((Path(__file__).parent.parent / "config.defaults.yaml").read_text(encoding="utf-8"))
    assert "subagent" not in raw["roles"]
    assert not SubagentConfig.from_raw(raw["subagents"], coding=raw["coding"]).enabled
    configured = SubagentConfig.from_raw({"enabled": True, "docker_host": "legacy"}, root=tmp_path,
                                         coding={"repo_path": "legacy", "workspaces_path": "legacy"})
    assert configured.enabled and configured.workspace_root == tmp_path / "workspace"


@pytest.mark.parametrize(("raw", "coding"), [
    ({"max_concurrency": 0}, {}), ({}, {"dsh_home": "workspace/dsh"}),
    ({"wait_timeout_seconds": float("inf")}, {}), ({}, {"dsh_binary": "-bad"}),
    ({}, {"dsh_binary": "./dsh"}), ({"unexpected": True}, {}),
])
def test_invalid_configuration_rejected(raw, coding):
    with pytest.raises(ValueError):
        SubagentConfig.from_raw(raw, coding=coding)
