from agent.prompt import SystemPromptBuilder, render_names
from agent.session_manager import SessionManager
from agent.chronicle import ChronicleManager

import sqlite3

import pytest


def test_system_prompt_wraps_soul_and_ignores_tool_description(tmp_path):
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    for name in ("soul", "master", "memory", "guild"):
        (prompt_dir / f"{name}.md").write_text(f"{name} content\n", encoding="utf-8")
    (prompt_dir / "tool_description.md").write_text("must not appear\n", encoding="utf-8")

    prompt = SystemPromptBuilder(
        prompt_dir=str(prompt_dir),
        skills_dir=str(tmp_path / "skills"),
        preview_dir=str(tmp_path / "preview"),
    ).build()

    assert prompt.startswith("<soul>\nsoul content\n</soul>")
    assert "<guild>\nguild content\n</guild>" in prompt
    assert "tool_description" not in prompt
    assert "must not appear" not in prompt


class _FakeManager:
    """称呼来自配置，测试用固定值验证标签不再写死角色名。"""

    def snapshot(self):
        return {"app": {"user_name": "alice", "agent_name": "bob"}}


def test_guild_uses_example_only_when_local_file_is_missing(tmp_path, monkeypatch):
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    guild = prompt_dir / "guild.md"
    app_root = tmp_path / "application"
    (app_root / "prompt").mkdir(parents=True)
    monkeypatch.setattr("agent.prompt.APP_ROOT", app_root)
    example = app_root / "prompt/guild.example.md"
    example.write_text("Example for {{user_name}}\n", encoding="utf-8")
    builder = SystemPromptBuilder(prompt_dir=str(prompt_dir), skills_dir=str(tmp_path / "skills"),
                                  preview_dir=str(tmp_path / "preview"), manager=_FakeManager())

    assert "<guild>\nExample for alice\n</guild>" in builder.build()

    guild.write_text("Local guide\n", encoding="utf-8")
    prompt = builder.build(force=True)
    assert "<guild>\nLocal guide\n</guild>" in prompt and "Example for alice" not in prompt

    guild.write_text("", encoding="utf-8")
    assert "<guild>" not in builder.build(force=True)

    guild.unlink()
    example.unlink()
    assert "<guild>" not in builder.build(force=True)


def test_names_render_in_every_main_prompt_section_and_preview(tmp_path):
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    source = "{{user_name}} talks to {{agent_name}}; {{other}} stays literal."
    for name in ("soul", "master", "memory", "guild", "situation"):
        (prompt_dir / f"{name}.md").write_text(source, encoding="utf-8")
    skill_dir = tmp_path / "skills" / "example"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        '---\nname: example\ndescription: Help {{user_name}} and {{agent_name}}\n---\n', encoding="utf-8",
    )
    builder = SystemPromptBuilder(prompt_dir=str(prompt_dir), skills_dir=str(tmp_path / "skills"),
                                  preview_dir=str(tmp_path / "preview"), manager=_FakeManager())

    prompt = builder.build()

    assert prompt.count("alice talks to bob; {{other}} stays literal.") == 5
    assert "Help alice and bob" in prompt
    assert "{{user_name}}" not in prompt and "{{agent_name}}" not in prompt
    assert builder.preview_path.read_text(encoding="utf-8") == prompt
    assert (prompt_dir / "soul.md").read_text(encoding="utf-8") == source


@pytest.mark.parametrize("config", [{}, {"app": {"user_name": " ", "agent_name": None}}])
def test_name_placeholders_have_neutral_defaults(config):
    assert render_names("{{user_name}} / {{agent_name}}", config) == "user / assistant"


def test_name_replacements_do_not_expand_inserted_values_again():
    config = {"app": {"user_name": "{{agent_name}}", "agent_name": "bob"}}
    assert render_names("{{user_name}} / {{agent_name}}", config) == "{{agent_name}} / bob"


def test_background_prompt_templates_use_their_manager_names():
    from agent.compressor import Compressor
    from memory.extractor import MemoryExtractor

    compressor = object.__new__(Compressor)
    extractor = object.__new__(MemoryExtractor)
    compressor.manager = extractor.manager = _FakeManager()

    compression_prompt = compressor._load_system_prompt()
    memory_prompt = extractor._load_prompt()

    assert "user：alice" in compression_prompt and "assistant：bob" in compression_prompt
    assert "bob" in memory_prompt
    for prompt in (compression_prompt, memory_prompt):
        assert "{{user_name}}" not in prompt and "{{agent_name}}" not in prompt


class _FakeChronicle:
    def get_summaries(self, _start, _end, *, strict):
        assert strict
        return []

    def get_recent_rounds(self, *, session_id, limit, conversation_only, strict):
        assert conversation_only and strict
        assert session_id == ""
        assert limit == 10
        return [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]


def test_new_session_context_has_separate_xml_sections():
    manager = object.__new__(SessionManager)
    manager.chronicle = _FakeChronicle()
    manager.manager = _FakeManager()

    message = manager._assemble_summary_message()

    assert message.startswith("这里是 session 的开始。\n\n<session_summary>\n")
    assert "\n</session_summary>\n\n<recent_messages>\n" in message
    assert "alice: hello\nbob: hi" in message
    assert "user: hello" not in message
    assert "assistant: hi" not in message
    assert message.endswith("\n</recent_messages>")


@pytest.mark.parametrize("legacy", [False, True])
def test_session_seed_selects_ten_user_rounds_and_excludes_all_heartbeat_outputs(tmp_path, legacy):
    chronicle = ChronicleManager(str(tmp_path / "synthetic.db"))
    for number in range(12):
        session_id = "synthetic-first" if number < 6 else "synthetic-second"
        chronicle.append_messages(session_id, [
            {"role": "user", "platform": "qq" if number % 2 else "web",
             "content": "心跳" if number == 2 else f"real input {number}"},
            {"role": "assistant", "platform": "web", "content": f"real output {number}"},
            {"role": "assistant", "platform": "qq", "content": f"confirmed send {number}"},
        ], round_id=f"real-{number}")
        for part in range(3):
            chronicle.append_messages(session_id, [
                {"role": "user", "platform": "system", "content": f"heartbeat input {number}-{part}"},
                {"role": "assistant", "platform": "web", "content": f"heartbeat output {number}-{part}"},
                {"role": "assistant", "platform": "qq", "content": f"heartbeat send {number}-{part}"},
            ], round_id=f"heartbeat-{number}-{part}")
    if legacy:
        with sqlite3.connect(chronicle.db_path) as db:
            db.execute("UPDATE raw_messages SET round_id = NULL")

    manager = object.__new__(SessionManager)
    manager.chronicle = chronicle
    manager.manager = _FakeManager()
    message = manager._assemble_summary_message()
    recent = message.split("<recent_messages>")[1]
    assert "heartbeat" not in recent
    assert "alice: 心跳" in recent  # 真实用户输入不按正文关键词过滤。
    assert "real input 0\n" not in recent and "real input 1\n" not in recent
    for number in range(2, 12):
        assert f"bob: real output {number}\n" in recent
        assert f"bob: confirmed send {number}\n" in recent
    assert recent.count("alice:") == 10
    assert "heartbeat output 11-2" in str(chronicle.get_recent_rounds(limit=3))
