"""统一输入与命令分流；仅使用合成会话、临时文件及模拟模型。"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from agent.session import AgentSession
from agent.commands import COMMAND_HELP, command_catalog
from agent.protocol import LoopResult, ModelConfig
from plugins.bundled.qqbot.adapter import QQAdapter
from web.server import create_app
from web.settings import WebSettings


async def test_expanded_file_body_survives_followup_and_history_reload(agent, tmp_path, monkeypatch):
    path = tmp_path / "data/files/2026-10-21/note.md"
    path.parent.mkdir(parents=True)
    path.write_text("attachment body must persist", encoding="utf-8")
    record = {"local_id": "synthetic-file", "path": path.relative_to(tmp_path).as_posix(),
              "mime": "text/plain", "filename": "note.md"}
    session = agent.session_manager.get_or_create_active_session()
    cache = tmp_path / "history.json"
    agent.session_manager.save_session = lambda: cache.write_text(json.dumps(session.history))
    agent.chronicle = SimpleNamespace(append_messages=Mock())
    agent._update_history_and_archive = AgentSession._update_history_and_archive.__get__(agent)
    await agent.run_round("read this", images=[record])
    saved = json.loads(cache.read_text())
    assert "attachment body must persist" in json.dumps(saved)
    path.unlink()
    session.history = saved
    await agent.run_round("follow up")
    messages = agent._loop.run.call_args.args[0]
    assert "attachment body must persist" in json.dumps(messages)
    assert json.dumps(messages).count("attachment body must persist") == 1


@pytest.fixture
def agent(monkeypatch):
    active = SimpleNamespace(session_id="synthetic", start_time=1, history=[])
    session = AgentSession.__new__(AgentSession)
    session.platform = "web"
    session._lock = asyncio.Lock()
    session._system_prompt = "synthetic prompt"
    session.callback = AsyncMock()
    session.heartbeat = SimpleNamespace(reset_timer=Mock())
    session.session_manager = SimpleNamespace(
        maybe_compress_daily=AsyncMock(), get_or_create_active_session=lambda: active,
        record_input_time=Mock(), context_tokens=0,
        manual_switch_session=AsyncMock(return_value="next-session"),
        manual_compress=AsyncMock(return_value="synthetic compression"),
    )
    session._loop = SimpleNamespace(
        model_config=ModelConfig('synthetic-model', 'https://model.test', provider='deepseek'),
        client=SimpleNamespace(context_cache=SimpleNamespace(note_user_input=Mock(), clear=AsyncMock())),
        run=AsyncMock(side_effect=lambda messages, context: LoopResult(
            [*messages, {"role": "assistant", "content": "synthetic answer"}], "synthetic answer", {},
        )),
    )
    session._update_history_and_archive = AsyncMock()
    session._save_rollback_state = Mock()
    session._clear_rollback_state = Mock()
    monkeypatch.setattr('agent.session.FileManager', Mock(return_value=SimpleNamespace(configured=False)))
    return session


@pytest.mark.parametrize("platform", ["web", "harmony", "qq", "future-platform"])
@pytest.mark.parametrize("text", ["/help", "/new-command", "/HELP", " /help "])
async def test_commands_on_every_platform_skip_model_media_and_history(agent, monkeypatch, platform, text):
    media = AsyncMock(side_effect=AssertionError("command must not prepare attachments"))
    monkeypatch.setattr(agent, "_prepare_user_media", media)
    result = await agent.run_round(text, platform, images=[{"local_id": "synthetic-video", "mime": "video/mp4"}])
    assert result.stop_reason == "command" and result.final_answer
    assert result.full_messages == [] and result.total_usage == {}
    agent._loop.run.assert_not_awaited()
    agent._loop.client.context_cache.note_user_input.assert_not_called()
    agent.session_manager.maybe_compress_daily.assert_not_awaited()
    agent.session_manager.record_input_time.assert_not_called()
    agent._update_history_and_archive.assert_not_awaited()
    agent._save_rollback_state.assert_not_called()
    agent._clear_rollback_state.assert_not_called()
    agent.heartbeat.reset_timer.assert_called_once()
    assert agent._pending_user_rounds == 0


@pytest.mark.parametrize("command", ["/session", "/compress", "/model use backup"])
async def test_management_commands_wait_for_round_lock_without_deadlock(agent, command):
    started = asyncio.Event()

    async def select_model(model_id):
        # 配置服务在独立任务中应用模型，需取得叙事流锁。
        async def apply():
            async with agent._lock:
                return "synthetic backup"
        started.set()
        return await asyncio.create_task(apply())

    agent.configuration_service = SimpleNamespace(select_model=select_model)
    await agent._lock.acquire()
    task = asyncio.create_task(agent.run_round(command, "harmony"))
    await asyncio.sleep(0)
    assert not task.done()
    if command == "/model use backup":
        await asyncio.wait_for(started.wait(), 2)
    agent._lock.release()
    result = await asyncio.wait_for(task, 2)
    assert result.stop_reason == "command"
    assert agent._pending_user_rounds == 0
    agent._loop.run.assert_not_awaited()


async def test_waiting_command_interrupts_heartbeat_and_cancellation_cleans_pending(agent):
    agent._heartbeat_interrupt = asyncio.Event()
    await agent._lock.acquire()
    task = asyncio.create_task(agent.run_round("/session"))
    try:
        await asyncio.sleep(0)
        assert agent._heartbeat_interrupt.is_set()
        assert agent._pending_user_rounds == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent._pending_user_rounds == 0
    finally:
        agent._lock.release()


@pytest.mark.parametrize("source", ["heartbeat", "subagent"])
async def test_system_content_is_never_parsed_as_command(agent, source):
    claim = AsyncMock(return_value=True)
    result = await agent.run_round(
        source=source, trigger_time=1,
        injection_messages=[{"role": "user", "content": "/session"}], before_run=claim,
    )
    assert result.stop_reason == "complete"
    claim.assert_awaited_once()
    agent.session_manager.manual_switch_session.assert_not_awaited()
    messages, context = agent._loop.run.call_args.args
    assert context.source == source and messages[-1]["content"] == "/session"
    assert (getattr(agent, "_heartbeat_interrupt", None)) is None


@pytest.mark.parametrize("platform", ["web", "harmony", "future-platform"])
async def test_normal_input_uses_common_context_and_preserves_rollback(agent, platform):
    result = await agent.run_round("synthetic question", platform)
    assert result.stop_reason == "complete"
    messages, context = agent._loop.run.call_args.args
    assert context.platform == platform and context.source == "user"
    assert f"[{platform}] synthetic question" in messages[-1]["content"]
    agent._save_rollback_state.assert_called_once()
    agent._clear_rollback_state.assert_not_called()


async def test_target_media_resolution_is_shared_by_user_and_system_rounds(agent, monkeypatch):
    files = SimpleNamespace(configured=True, ensure_fresh_file_ids=AsyncMock(),
                            image_block_to_data_url=AsyncMock(), resolve_remote_file_id=AsyncMock())
    monkeypatch.setattr('agent.session.FileManager', lambda: files)
    block = {'type': 'image', 'local_id': 'synthetic-image'}
    for source in ("user", "heartbeat", "subagent"):
        await agent.run_round("synthetic input", source=source)
        _, context = agent._loop.run.call_args.args
        assert context.image_resolver is files.image_block_to_data_url
        await context.image_file_resolver(block, ('deepseek_files',))
        files.resolve_remote_file_id.assert_awaited_with(
            block, ('deepseek_files',), model_config=agent._loop.model_config)
    assert files.resolve_remote_file_id.await_count == 3
    files.ensure_fresh_file_ids.assert_not_awaited()


async def test_new_backend_command_is_discoverable_without_client_changes(monkeypatch):
    monkeypatch.setitem(COMMAND_HELP, "future-command", "synthetic command")
    assert {"value": "/future-command", "description": "synthetic command"} in command_catalog()


async def test_qq_returns_command_feedback_without_calling_model(agent, monkeypatch):
    monkeypatch.setattr(QQAdapter, "_load_latest_chat", lambda self: None)
    adapter = QQAdapter(session=agent, default_to="synthetic-user")
    adapter._save_latest_chat = Mock()
    adapter._deliver_to = AsyncMock()
    adapter._api_request = AsyncMock(side_effect=AssertionError("test must not contact QQ"))
    await adapter._handle_c2c_message({
        "id": "synthetic-id", "author": {"user_openid": "synthetic-user"}, "content": "/help",
    })
    assert "/session" in adapter._deliver_to.call_args.args[0]
    assert adapter._deliver_to.call_args.args[1] == {"type": "c2c", "chat_id": "synthetic-user"}
    adapter._api_request.assert_not_awaited()
    agent.heartbeat.reset_timer.assert_called_once()
    agent._loop.run.assert_not_awaited()


@pytest.mark.parametrize("platform", ["web", "harmony", "future-platform"])
async def test_send_api_returns_generic_command_result(agent, tmp_path, platform):
    settings = WebSettings(str(tmp_path / "settings.json"))
    app = create_app(None, None, agent, None, None, SimpleNamespace(db_path=tmp_path / "unused.db"), settings, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/api/chat/send", json={"text": "/help", "platform": platform})
        assert response.status_code == 200
        assert response.json()["command"]["refresh_history"] is True
        assert "/session" in response.json()["command"]["text"]
        assert response.json()["total_tokens"] == 0
        legacy = await client.post("/api/chat/command", json={"text": "/help"})
        assert legacy.json()["text"] == response.json()["command"]["text"]
        catalog = await client.get("/api/chat/commands")
        assert catalog.json()["commands"] == command_catalog()
        settings.set_password("synthetic password")
        assert (await client.get("/api/chat/commands")).status_code == 401
    agent._loop.run.assert_not_awaited()
