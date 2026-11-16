"""合成近况与临时提示词；不读取私人 prompt 或调用模型。"""

import json

import pytest

from agent.prompt import SystemPromptBuilder
from runtime.tool_registry import ToolRegistry
from tools import situation_notes


@pytest.fixture
def builder(tmp_path):
    prompt_dir = tmp_path / "prompt"
    prompt_dir.mkdir()
    (prompt_dir / "soul.md").write_text("synthetic soul", encoding="utf-8")
    return SystemPromptBuilder(
        prompt_dir=str(prompt_dir), skills_dir=str(tmp_path / "skills"),
        preview_dir=str(tmp_path / "preview"),
    )


def test_situation_is_last_and_does_not_refresh_cached_prompt(builder):
    builder.write_situation("尚在考虑一个合成计划，未确定。")
    prompt = builder.build()
    assert prompt.endswith("<sisuation>\n尚在考虑一个合成计划，未确定。\n</sisuation>\n")
    assert prompt.index("</available_skills>") < prompt.index("<sisuation>")

    builder.write_situation("合成计划已确定。")
    assert builder.read_situation() == "合成计划已确定。"
    assert builder.build() == prompt
    assert builder.preview_path.read_text(encoding="utf-8") == prompt

    rebuilt = SystemPromptBuilder(
        prompt_dir=str(builder.prompt_dir), skills_dir=str(builder.skills_dir),
        preview_dir=str(builder.preview_path.parent),
    ).build()
    assert rebuilt.endswith("<sisuation>\n合成计划已确定。\n</sisuation>\n")


def test_missing_and_cleared_situation_are_empty(builder):
    assert builder.read_situation() == ""
    assert builder.build().endswith("<sisuation>\n\n</sisuation>\n")
    builder.write_situation("synthetic note")
    builder.write_situation("")
    assert builder.read_situation() == ""


@pytest.mark.parametrize("stage", ["fsync", "replace"])
def test_failed_write_preserves_old_version_and_removes_temporary_file(builder, monkeypatch, stage):
    builder.write_situation("old synthetic note")

    def fail(*args):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(f"agent.prompt.os.{stage}", fail)
    with pytest.raises(OSError, match="synthetic storage failure"):
        builder.write_situation("new synthetic note")
    assert builder.read_situation() == "old synthetic note"
    assert not list(builder.prompt_dir.glob(".situation-*.tmp"))


async def test_registered_tool_reads_replaces_clears_and_reports_save_failure(builder, monkeypatch):
    registry = ToolRegistry()
    situation_notes.register_tools(registry)
    monkeypatch.setattr(situation_notes, "_builder", builder)
    assert registry.dependency_errors({"situation_notes"}, {"builder": builder}) == []

    async def call(args):
        result = await registry.execute("situation_notes", args)
        return result if result == "已保存" else json.loads(result)

    assert await call({"action": "read"}) == {"content": ""}
    assert await call({"action": "write", "content": "synthetic note"}) == "已保存"
    assert await call({"action": "read"}) == {"content": "synthetic note"}
    assert "error" in await call({"action": "write"})
    assert "error" in await call({"action": "write", "content": None})
    assert "error" in await call({"action": "append", "content": "invalid"})
    assert builder.read_situation() == "synthetic note"

    def fail(content):
        raise OSError("synthetic storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(builder, "write_situation", fail)
        result = await call({"action": "write", "content": "must not be saved"})
    assert "synthetic storage failure" in result["error"]
    assert builder.read_situation() == "synthetic note"
    assert await call({"action": "write", "content": ""}) == "已保存"
    assert await call({"action": "read"}) == {"content": ""}


def test_read_failure_is_not_treated_as_empty_situation(builder):
    (builder.prompt_dir / "situation.md").mkdir()
    with pytest.raises(OSError):
        builder.build()
