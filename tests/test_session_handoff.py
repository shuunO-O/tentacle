"""Synthetic model-switch histories and temporary caches; no live conversation."""

from copy import deepcopy
from dataclasses import replace
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.compressor import Compressor
from agent.protocol import ModelConfig
from agent.handoff import compatible_history, dialogue_history, model_identity
from agent.session_manager import SessionManager


def config(**kw):
    return ModelConfig('gemini-test', 'https://model.test', provider='google', api_style='interactions',
                       api_key='synthetic-key', **kw)


def history():
    return [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Question'},
                                   {'type': 'image', 'local_id': 'local-1', 'file_id': 'gs://old', 'mime': 'image/png'}]},
        {'role': 'assistant', 'content': 'Tool narration', 'tool_calls': [
            {'id': 'a', 'function': {'name': 'send_message', 'arguments': '{"text":"Delivered"}'}},
            {'id': 'b', 'function': {'name': 'send_message', 'arguments': '{"text":"Failed"}'}},
            {'id': 'c', 'function': {'name': 'send_message', 'arguments': '{"text":"Unconfirmed"}'}},
        ], 'gemini_steps': [{'type': 'thought', 'signature': 'signed'}]},
        {'role': 'tool', 'tool_call_id': 'a', 'content': '{"status":"sent"}'},
        {'role': 'tool', 'tool_call_id': 'b', 'content': '{"error":"failed"}'},
        {'role': 'assistant', 'content': 'Delivered', 'reasoning_content': 'private reasoning',
         'gemini_interaction': {'id': 'old-chain'}},
        {'role': 'user', 'content': 'Again'},
        {'role': 'assistant', 'content': 'Delivered'},
    ]


@pytest.mark.parametrize('changes,expected', [
    ({'temperature': 0.3, 'reasoning_effort': 'low'}, True),
    ({'name': 'gemini-another'}, True),
    ({'name': 'unverified-model'}, False),
    ({'api_style': 'gemini_generate'}, False),
    ({'base_url': 'https://other.test'}, False),
    ({'api_key': 'different-account'}, False),
    ({'provider_id': 'different-connection'}, False),
    ({'provider': 'openai', 'api_style': 'responses'}, False),
])
def test_compatibility_is_whole_session_and_conservative(changes, expected):
    cfg = config()
    assert compatible_history(model_identity(cfg), model_identity(replace(cfg, **changes))) is expected
    assert not compatible_history(None, model_identity(cfg))
    assert cfg.api_key not in json.dumps(model_identity(cfg))


def test_same_vendor_other_model_is_not_implicitly_compatible():
    cfg = ModelConfig('gpt-a', 'https://model.test', provider='openai', api_style='responses')
    assert not compatible_history(model_identity(cfg), model_identity(replace(cfg, name='gpt-b')))


def test_dialogue_preserves_attachments_confirmed_sends_and_repeated_rounds():
    original = history()
    saved = deepcopy(original)
    rebuilt = dialogue_history(original)
    assert original == saved
    assert rebuilt == [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'Question'},
                                   {'type': 'image', 'local_id': 'local-1', 'mime': 'image/png'}]},
        {'role': 'assistant', 'content': 'Delivered'},
        {'role': 'user', 'content': 'Again'},
        {'role': 'assistant', 'content': 'Delivered'},
    ]


def test_deepseek_switch_preserves_dialogue_roles_without_extra_wrapping(sessions):
    cfg = ModelConfig('deepseek-test', 'https://ds.test', provider='deepseek')
    sessions.apply_model_config(cfg)
    rebuilt = sessions.current_session.history
    assert rebuilt == dialogue_history(history())
    assert rebuilt[0]['content'][1]['local_id'] == 'local-1'
    assert [msg['role'] for msg in rebuilt] == ['user', 'assistant', 'user', 'assistant']
    encoded = json.dumps(rebuilt)
    for removed in ('private reasoning', 'signature', 'tool_calls', 'gs://old', 'Unconfirmed', 'Failed'):
        assert removed not in encoded


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(Compressor, '_load_system_prompt', lambda self: 'Synthetic')
    monkeypatch.setattr(SessionManager, '_assemble_summary_message', lambda self: 'Synthetic summary')
    manager = SimpleNamespace(_raw_config={'models': {'chat': {}}})
    sm = SessionManager(manager, db_path=str(tmp_path / 'chronicle.db'),
                        cache_path=str(tmp_path / 'active_session.json'),
                        compression_state_path=str(tmp_path / 'compression.json'))
    session = sm.get_or_create_active_session()
    session.history = history()
    sm.save_session()
    return sm


def test_unknown_legacy_cache_rebuilds_once_and_restart_is_stable(sessions):
    original = deepcopy(sessions.current_session.history)
    sessions.apply_model_config(config())
    assert sessions.current_session.history == dialogue_history(original)
    snapshot = next((sessions.cache_path.parent / 'session_handoffs').glob('*.json'))
    assert json.loads(snapshot.read_text())['history'] == original
    sessions._history_model = None
    sessions._load_cache()
    sessions.apply_model_config(config())
    assert len(list(snapshot.parent.glob('*.json'))) == 1


def test_gemini_model_change_keeps_signatures_but_discards_chain(sessions):
    sessions._history_model = model_identity(config())
    sessions.current_session.history = history()
    sessions._context_tokens = 123
    sessions.apply_model_config(replace(config(), name='gemini-other'))
    assert sessions.current_session.history[1]['gemini_steps'][0]['signature'] == 'signed'
    assert 'gemini_interaction' not in sessions.current_session.history[4]
    assert sessions.context_tokens == 0


def test_interactions_to_generate_import_is_persisted_at_switch(sessions):
    sessions._history_model = model_identity(config())
    sessions.current_session.history = [
        {'role': 'user', 'content': 'Question'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}],
         'gemini_steps': [{'type': 'thought', 'signature': 'old-ciphertext'},
                          {'type': 'function_call', 'id': 'call', 'name': 'lookup', 'arguments': {}}]},
        {'role': 'tool', 'tool_call_id': 'call', 'content': '{"value":1}'},
        {'role': 'assistant', 'content': 'Answer', 'gemini_steps': [
            {'type': 'model_output', 'content': [{'type': 'text', 'text': 'Answer'}]}]},
    ]
    sessions.apply_model_config(replace(config(), api_style='gemini_generate'))
    saved = json.loads(sessions.cache_path.read_text())
    assert saved['history'][2]['role'] == 'tool'
    assert saved['history'][1]['gemini_content']['parts'][0]['thoughtSignature'] == 'skip_thought_signature_validator'
    assert 'old-ciphertext' not in json.dumps(saved)
    assert saved['history_model']['protocol'] == 'gemini_generate'
    sessions._load_cache()
    sessions.apply_model_config(replace(config(), api_style='gemini_generate'))
    assert len(list((sessions.cache_path.parent / 'session_handoffs').glob('*.json'))) == 1


def test_parameter_change_does_not_rewrite_cache(sessions, monkeypatch):
    sessions._history_model = model_identity(config())
    monkeypatch.setattr(sessions, '_save_cache', lambda: pytest.fail('parameter change rewrote history'))
    sessions.apply_model_config(replace(config(), temperature=0.8))


@pytest.mark.parametrize('api_style', ['chat', 'responses', 'interactions', 'gemini_generate'])
async def test_rollback_preserves_native_history_on_followup_and_restart(sessions, monkeypatch, api_style):
    from agent.session import AgentSession
    from agent.protocol import LoopResult

    cfg = replace(config(), api_style=api_style)
    native = {
        'chat': {'reasoning_content': 'synthetic thought'},
        'responses': {'responses_output': [
            {'type': 'reasoning', 'encrypted_content': 'synthetic ciphertext'}]},
        'interactions': {'gemini_steps': [{'type': 'thought', 'signature': 'synthetic signature'}],
                         'gemini_interaction': {'id': 'synthetic chain'}},
        'gemini_generate': {'gemini_content': {'role': 'model', 'parts': [
            {'thought': True, 'text': 'synthetic thought', 'thoughtSignature': 'synthetic signature'}]}},
    }[api_style]
    original = [
        {'role': 'user', 'content': 'synthetic earlier input'},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'synthetic-call', 'type': 'function',
             'function': {'name': 'lookup', 'arguments': '{}'}}], **native},
        {'role': 'tool', 'tool_call_id': 'synthetic-call', 'content': 'synthetic result'},
        {'role': 'assistant', 'content': 'synthetic earlier answer'},
    ]
    active = sessions.current_session
    active.history = deepcopy(original)
    sessions._history_model = model_identity(cfg)
    sessions._context_tokens = 123
    sessions.save_session()
    agent = AgentSession.__new__(AgentSession)
    agent.platform, agent._system_prompt = 'web', 'synthetic prompt'
    agent._lock = asyncio.Lock()
    agent._rollback_state_path = sessions.cache_path.parent / 'rollback.json'
    agent.session_manager, agent.chronicle = sessions, sessions.chronicle
    agent.callback = AsyncMock()
    agent._loop = SimpleNamespace(
        model_config=cfg, client=SimpleNamespace(context_cache=None),
        run=AsyncMock(side_effect=lambda messages, context: LoopResult(
            [*messages, {'role': 'assistant', 'content': 'synthetic reply'}], 'synthetic reply', {})),
    )
    monkeypatch.setattr(sessions, 'maybe_compress_daily', AsyncMock())
    monkeypatch.setattr('agent.session.FileManager', lambda: SimpleNamespace(configured=False))

    await agent.run_round('synthetic input to undo')
    assert (await agent.run_round('/rollback')).stop_reason == 'command'
    agent._prepare_session_history()
    assert sessions.current_session.session_id == active.session_id
    assert sessions.current_session.history == original
    assert sessions.context_tokens == 123
    sessions._load_cache()
    agent._prepare_session_history()
    assert sessions.current_session.history == original
    assert sessions._history_model == model_identity(cfg)
    await agent.run_round('synthetic followup')
    messages = agent._loop.run.call_args.args[0]
    assert messages[1:1 + len(original)] == original
    assert not (sessions.cache_path.parent / 'session_handoffs').exists()


def test_restore_uses_checkpoint_model_to_detect_real_connection_change(sessions):
    previous = model_identity(config())
    checkpoint = deepcopy(sessions.current_session.history)
    active = sessions.current_session
    target = replace(config(), base_url='https://other.test')
    sessions.apply_model_config(target)
    sessions.restore_session(active.session_id, active.start_time, checkpoint, 123, history_model=previous)
    assert sessions._history_model == previous
    sessions.apply_model_config(target)
    assert sessions.current_session.history == dialogue_history(checkpoint)
    assert sessions._history_model == model_identity(target)


def test_agent_startup_reconciles_configuration_changed_while_stopped(sessions, monkeypatch, tmp_path):
    from agent.session import AgentSession
    cfg = replace(config(), api_style='gemini_generate')
    sessions._history_model = model_identity(config())
    original = deepcopy(sessions.current_session.history)
    sessions.save_session()
    monkeypatch.setattr('agent.session.ChronicleManager', lambda: sessions.chronicle)
    monkeypatch.setattr(AgentSession, '_create_loop', lambda self: SimpleNamespace(model_config=cfg))
    AgentSession(sessions.manager, None, sessions,
                 system_prompt_builder=SimpleNamespace(build=lambda: 'Synthetic'),
                 rollback_state_path=str(tmp_path / 'rollback.json'), active_model_path=str(tmp_path / 'model.json'))
    assert sessions.current_session.history == dialogue_history(original)
    assert json.loads(sessions.cache_path.read_text())['history_model']['protocol'] == 'gemini_generate'


async def test_agent_without_chat_credentials_preserves_history_and_rejects_rounds(sessions, monkeypatch):
    from agent.session import AgentSession
    from config.providers import CredentialConfigurationError

    def missing_credentials(role):
        assert role == "chat"
        raise CredentialConfigurationError("Synthetic credential details")

    original = deepcopy(sessions.current_session.history)
    monkeypatch.setattr('agent.session.ChronicleManager', lambda: sessions.chronicle)
    manager = SimpleNamespace(get_model=missing_credentials)
    callback = AsyncMock()
    agent = AgentSession(manager, callback, sessions, system_prompt_builder=SimpleNamespace(build=lambda: 'Synthetic'))

    assert not agent.background_ready
    agent.set_callback(callback)
    with pytest.raises(RuntimeError, match='主对话尚未就绪'):
        await agent.run_round("Synthetic input")
    assert callback.await_args.args[0].data["status"] == "failed"
    assert sessions.current_session.history == original
    await agent.close()


def test_failed_cache_publication_preserves_memory_and_disk(sessions, monkeypatch):
    import agent.session_manager as session_manager
    disk = sessions.cache_path.read_bytes()
    original = deepcopy(sessions.current_session.history)
    original_replace = session_manager.os.replace

    def fail_cache(source, target):
        if target == sessions.cache_path:
            raise OSError('synthetic disk full')
        original_replace(source, target)

    monkeypatch.setattr(session_manager.os, 'replace', fail_cache)
    with pytest.raises(OSError):
        sessions.apply_model_config(config())
    assert sessions.current_session.history == original
    assert sessions._history_model is None
    assert sessions.cache_path.read_bytes() == disk


@pytest.mark.parametrize("failure", [None, "backup", "cache"])
def test_old_history_is_backed_up_before_repair_and_failures_preserve_original(sessions, monkeypatch, failure):
    from agent.session import AgentSession
    original = [None, {"role": "assistant", "content": None},
                {"role": "user", "content": "synthetic old input"},
                {"role": "assistant", "content": None,
                 "gemini_steps": [{"type": "thought", "signature": "synthetic signature"}]}]
    sessions.current_session.history = deepcopy(original)
    sessions.save_session()
    disk = sessions.cache_path.read_bytes()
    agent = AgentSession.__new__(AgentSession)
    agent.session_manager = sessions
    write = sessions._write_cache

    def fail(data, path):
        if ((failure == "backup" and path.parent.name == "history_repairs")
                or (failure == "cache" and path == sessions.cache_path)):
            raise OSError("synthetic repair save failure")
        write(data, path)

    monkeypatch.setattr(sessions, "_write_cache", fail)
    if failure:
        with pytest.raises(OSError, match="repair save failure"):
            agent._repair_session_history(sessions.current_session)
        assert sessions.current_session.history == original
        assert sessions.cache_path.read_bytes() == disk
    else:
        agent._repair_session_history(sessions.current_session)
        assert sessions.current_session.history == original[2:]
        # 再检查不会重复修复或重复备份。
        agent._repair_session_history(sessions.current_session)
    backups = list((sessions.cache_path.parent / "history_repairs").glob("*.json"))
    if failure == "backup":
        assert backups == []
    else:
        assert len(backups) == 1
        assert json.loads(backups[0].read_text(encoding="utf-8"))["history"] == original


def test_entire_null_history_is_backed_up_as_null_before_repair(sessions):
    from agent.session import AgentSession
    sessions.current_session.history = None
    sessions.save_session()
    agent = AgentSession.__new__(AgentSession)
    agent.session_manager = sessions
    agent._repair_session_history(sessions.current_session)
    assert sessions.current_session.history == []
    backup = next((sessions.cache_path.parent / "history_repairs").glob("*.json"))
    assert json.loads(backup.read_text(encoding="utf-8"))["history"] is None


def test_dialogue_history_preserves_text_send_message_receipts():
    raw = [
        {"role": "user", "content": "notify both"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "send_message", "arguments": '{"platform":"qq","text":"First OK"}'}},
                {"id": "c2", "function": {"name": "send_message", "arguments": '{"platform":"tg","text":"Partial fail"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "已发送:2段 | 附件:1"},
        {"role": "tool", "tool_call_id": "c2", "content": '{"error":"发送未全部确认","sent_chunks":1,"total_chunks":2}'},
    ]
    assert dialogue_history(raw) == [
        {"role": "user", "content": "notify both"},
        {"role": "assistant", "content": "First OK"},
    ]
