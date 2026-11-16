"""file_edit 工具测试：覆盖新建、替换、replace_all、安全检查、路径规范化与备份审计。"""

import json
import time

import pytest

import tools.file_edit as fe
from runtime.paths import get_paths
from runtime.tool_registry import ToolRegistry


@pytest.fixture
def root(tmp_path):
    """tests/conftest.py 已将 TENTACLE_HOME 指向 tmp_path。"""
    return tmp_path


def run(**kwargs):
    return fe.file_edit(kwargs)


def write(root, rel, content):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------- create mode ----------


def test_create_success(root):
    out = run(path="workspace/new.txt", new_str="content")
    assert "New file created successfully" in out
    assert (root / "workspace/new.txt").read_text(encoding="utf-8") == "content"


def test_create_empty_old_str_creates_new_file(root):
    out = run(path="workspace/empty_old.txt", old_str="", new_str="hello")
    assert "New file created successfully" in out
    assert (root / "workspace/empty_old.txt").read_text(encoding="utf-8") == "hello"


def test_create_rejects_existing(root):
    write(root, "workspace/exists.txt", "old")
    out = run(path="workspace/exists.txt", new_str="new")
    assert "Cannot overwrite" in out
    assert (root / "workspace/exists.txt").read_text(encoding="utf-8") == "old"


def test_create_rejects_directory(root):
    (root / "workspace/dir").mkdir(parents=True)
    out = run(path="workspace/dir", new_str="x")
    assert "is a directory" in out


def test_requires_new_str(root):
    assert "new_str` is required" in run(path="workspace/x.txt")


# ---------- replace mode ----------


def test_replace_success(root):
    write(root, "workspace/a.txt", "foo\nbar\nbaz")
    out = run(path="workspace/a.txt", old_str="bar", new_str="BAR")
    assert "edited successfully" in out
    assert (root / "workspace/a.txt").read_text(encoding="utf-8") == "foo\nBAR\nbaz"


def test_replace_delete_with_empty_new_str(root):
    write(root, "workspace/a.txt", "foo\nbar")
    out = run(path="workspace/a.txt", old_str="bar", new_str="")
    assert "edited successfully" in out
    assert (root / "workspace/a.txt").read_text(encoding="utf-8") == "foo\n"


def test_replace_missing_target(root):
    out = run(path="workspace/missing.txt", old_str="a", new_str="b")
    assert "does not exist" in out


def test_replace_not_found_truncates_long_old_str(root):
    write(root, "workspace/a.txt", "foo")
    long_old = "x" * 350
    out = run(path="workspace/a.txt", old_str=long_old, new_str="y")
    assert "did not appear verbatim" in out
    assert "x" * 200 + "..." in out
    assert long_old not in out
    assert (root / "workspace/a.txt").read_text(encoding="utf-8") == "foo"


def test_replace_multiple_matches_lists_lines(root):
    write(root, "workspace/a.txt", "x\nfoo\ny\nfoo\nz")
    out = run(path="workspace/a.txt", old_str="foo", new_str="X")
    assert "Multiple occurrences" in out
    assert "lines [2, 4]" in out
    assert (root / "workspace/a.txt").read_text(encoding="utf-8") == "x\nfoo\ny\nfoo\nz"


def test_replace_all_replaces_every_occurrence(root):
    write(root, "workspace/a.txt", "x\nfoo\ny\nfoo\nz")
    out = run(path="workspace/a.txt", old_str="foo", new_str="X", replace_all=True)
    assert "edited successfully" in out
    assert (root / "workspace/a.txt").read_text(encoding="utf-8") == "x\nX\ny\nX\nz"


def test_replace_all_must_be_boolean(root):
    write(root, "workspace/a.txt", "foo")
    assert "boolean" in run(path="workspace/a.txt", old_str="foo", new_str="bar", replace_all="yes")


# ---------- 安全检查 ----------


def test_binary_rejected(root):
    path = root / "workspace/bin.dat"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"abc\x00def")
    out = run(path="workspace/bin.dat", old_str="abc", new_str="xyz")
    assert "binary file" in out


def test_non_utf8_rejected(root):
    path = root / "workspace/bad.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\xfa")
    assert "not UTF-8" in run(path="workspace/bad.txt", old_str="a", new_str="b")


def test_oversize_rejected(root, monkeypatch):
    monkeypatch.setattr(fe, "_MAX_FILE_SIZE", 10)
    write(root, "workspace/big.txt", "x" * 100)
    assert "too large" in run(path="workspace/big.txt", old_str="x", new_str="y")


# ---------- 路径规范化与敏感文件保护 ----------


def test_dotdot_traversal_cannot_bypass_forbidden_area(root):
    target = root / "data" / "self_backups" / "audit.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"old": true}\n', encoding="utf-8")
    out = run(
        path="workspace/../data/self_backups/audit.log",
        old_str='{"old": true}',
        new_str='{"tampered": true}',
    )
    assert "Write rejected" in out
    assert "audit & backup area" in out
    assert '{"old": true}' in target.read_text(encoding="utf-8")


def test_dotdot_traversal_cannot_bypass_backup(root):
    write(root, "prompt/soul.md", "original")
    out = run(path="data/../prompt/soul.md", old_str="original", new_str="changed")
    assert "edited successfully" in out
    assert "已备份原文件" in out
    backups = list((root / "data" / "self_backups" / "prompt").glob("soul.md.*"))
    assert len(backups) == 1


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "config/.env",
        "config/webui_settings.json",
        "data/device_enroll_pending.json",
        ".ssh/id_rsa",
        "certs/cert.pem",
    ],
)
def test_sensitive_credential_paths_rejected(root, path):
    out_create = run(path=path, new_str="secret=2")
    assert "Write rejected" in out_create
    assert not (root / path).exists()

    write(root, path, "secret=1")
    out = run(path=path, old_str="secret=1", new_str="secret=2")
    assert "Write rejected" in out
    assert (root / path).read_text(encoding="utf-8") == "secret=1"


# ---------- 保护区备份 ----------


def test_backup_and_audit_in_prompt_and_same_second_collision(root, monkeypatch):
    write(root, "prompt/soul.md", "v0")
    monkeypatch.setattr(time, "strftime", lambda fmt: "20261113-120000" if fmt == "%Y%m%d-%H%M%S" else "2026-11-13 12:00:00")

    out1 = run(path="prompt/soul.md", old_str="v0", new_str="v1")
    out2 = run(path="prompt/soul.md", old_str="v1", new_str="v2")
    assert "edited successfully" in out1 and "已备份原文件" in out1
    assert "edited successfully" in out2 and "已备份原文件" in out2

    backups = sorted((root / "data" / "self_backups" / "prompt").glob("soul.md.*"))
    assert len(backups) == 2
    assert {b.read_text(encoding="utf-8") for b in backups} == {"v0", "v1"}

    audit_lines = (root / "data" / "self_backups" / "audit.log").read_text(encoding="utf-8").strip().splitlines()
    entries = [json.loads(line) for line in audit_lines]
    assert [e["command"] for e in entries] == ["replace", "replace"]
    assert entries[0]["path"] == "prompt/soul.md"


def test_create_in_skills_audits_without_backup(root):
    out = run(path="skills/test-skill/SKILL.md", new_str="hello")
    assert "New file created successfully" in out
    assert "保护区新建文件" in out
    skill_backups = list((root / "data" / "self_backups" / "skills").glob("*")) if (root / "data" / "self_backups" / "skills").exists() else []
    assert skill_backups == []
    audit = (root / "data" / "self_backups" / "audit.log").read_text(encoding="utf-8")
    assert "skills/test-skill/SKILL.md" in audit


def test_source_code_change_under_home_backs_up(root):
    write(root, "web/server.py", "old = 1\n")
    out = run(path="web/server.py", old_str="old = 1", new_str="new = 1")
    assert "已备份原文件" in out
    backups = list((root / "data" / "self_backups" / "web").glob("server.py.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old = 1\n"


@pytest.mark.parametrize("rel", ["workspace/note.md", "logs/note.md"])
def test_excluded_dir_no_backup(root, rel):
    write(root, rel, "content")
    out = run(path=rel, old_str="content", new_str="new")
    assert "edited successfully" in out
    assert "已备份" not in out
    assert not (root / "data" / "self_backups").exists()


# ---------- 不可变清单（state_root + app_root） ----------


@pytest.mark.parametrize(
    "path",
    [
        "tools/file_edit.py",
        "runtime/terminal_manager.py",
        "runtime/self_backup.py",
        "tools/self_rollback.py",
        "scripts/rollback.py",
    ],
)
def test_immutable_files_rejected_in_state_root(root, path):
    write(root, path, "x = 1\n")
    out = run(path=path, old_str="x = 1", new_str="y = 2")
    assert "Write rejected" in out
    assert "immutable list" in out
    assert (root / path).read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.parametrize(
    "path",
    [
        "tools/file_edit.py",
        "runtime/terminal_manager.py",
        "runtime/self_backup.py",
        "tools/self_rollback.py",
        "scripts/rollback.py",
    ],
)
def test_immutable_files_rejected_in_app_root(root, path):
    app_target = get_paths().app_root / path
    out = run(path=str(app_target), old_str="def ", new_str="def hacked_")
    assert "Write rejected" in out
    assert "immutable list" in out


def test_immutable_recreate_rejected(root):
    out = run(path="tools/file_edit.py", new_str="hacked = True")
    assert "Write rejected" in out
    assert not (root / "tools/file_edit.py").exists()


def test_new_tool_file_allowed(root):
    out = run(path="tools/my_new_tool.py", new_str="VALUE = 1")
    assert "New file created successfully" in out
    assert (root / "tools/my_new_tool.py").read_text(encoding="utf-8") == "VALUE = 1"


# ---------- Python 语法校验 ----------


def test_python_syntax_error_rejected(root):
    write(root, "tools/broken.py", "VALUE = 1\n")
    out = run(path="tools/broken.py", old_str="VALUE = 1", new_str="def oops(:\n    pass")
    assert "Write rejected" in out
    assert "invalid Python syntax at line 1" in out
    assert (root / "tools/broken.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_python_valid_code_written(root):
    write(root, "tools/fine.py", "VALUE = 1\n")
    out = run(path="tools/fine.py", old_str="VALUE = 1", new_str="VALUE = 2")
    assert "edited successfully" in out

    write(root, "workspace/notes.txt", "def not checked(:")
    out2 = run(path="workspace/notes.txt", old_str="not", new_str="never")
    assert "edited successfully" in out2


# ---------- 参数校验 ----------


def test_empty_path(root):
    assert "non-empty string" in run(path="  ", new_str="x")


async def test_register_tools_and_registry_execute(root):
    reg = ToolRegistry()
    fe.register_tools(reg)
    assert reg.has("file_edit")
    out = await reg.execute("file_edit", {"path": "workspace/reg.txt", "new_str": "hello"})
    assert "New file created successfully" in out
    assert (root / "workspace/reg.txt").read_text(encoding="utf-8") == "hello"
