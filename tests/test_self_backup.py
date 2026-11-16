"""
self_backup 核心模块测试

覆盖：备份列举（过滤/排序/audit.log 排除）、restore（缺省最新/指定版本/版本不存在）、
恢复前自动快照、路径穿越校验、agent 通道拒绝不可变清单、.env 拒绝。
全部走 tmp_path 隔离，不碰生产 data/。
"""

import json

import pytest

import runtime.self_backup as sb


@pytest.fixture
def root(tmp_path, monkeypatch):
    """把核心模块的根目录与备份区指向临时目录"""
    return tmp_path


def make_backup(root, rel, stamp, content="old"):
    """手工构造一条 editor 格式的备份记录"""
    p = root / rel
    backup_dir = root / "data" / "self_backups" / p.parent.relative_to(root)
    backup_dir.mkdir(parents=True, exist_ok=True)
    f = backup_dir / f"{p.name}.{stamp}"
    f.write_text(content, encoding="utf-8")
    return f


# ---------- list_backups ----------


def test_list_empty(root):
    assert sb.list_backups() == []


def test_list_groups_by_source_and_time(root):
    make_backup(root, "prompt/soul.md", "20260824-120000")
    make_backup(root, "prompt/soul.md", "20260824-130000", content="v2")
    make_backup(root, "web/server.py", "20260824-140000")

    items = sb.list_backups()
    sources = [it["source"] for it in items]
    assert sources == ["prompt/soul.md", "prompt/soul.md", "web/server.py"]
    stamps = [it["timestamp"] for it in items if it["source"] == "prompt/soul.md"]
    assert stamps == ["20260824-120000", "20260824-130000"]


def test_list_filter_by_path(root):
    make_backup(root, "prompt/soul.md", "20260824-120000")
    make_backup(root, "web/server.py", "20260824-130000")

    items = sb.list_backups("web/server.py")
    assert len(items) == 1
    assert items[0]["source"] == "web/server.py"


def test_list_ignores_audit_log_and_unstamped(root):
    audit = root / "data" / "self_backups" / "audit.log"
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("{}\n", encoding="utf-8")
    stray = root / "data" / "self_backups" / "notes.txt"
    stray.write_text("x", encoding="utf-8")

    assert sb.list_backups() == []


def test_list_rejects_escape(root):
    with pytest.raises(ValueError):
        sb.list_backups("../outside.txt")


# ---------- restore ----------


def test_restore_latest_and_snapshot(root):
    target = root / "prompt" / "soul.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("current", encoding="utf-8")
    make_backup(root, "prompt/soul.md", "20260824-120000", content="v1")
    make_backup(root, "prompt/soul.md", "20260824-130000", content="v2")

    result = sb.restore("prompt/soul.md")
    assert result["ok"] is True
    assert result["restored_timestamp"] == "20260824-130000"
    assert target.read_text(encoding="utf-8") == "v2"
    # 恢复前当前版本被快照
    snap = root / "data" / "self_backups" / "prompt" / result["previous_version_snapshot"].split("/")[-1]
    assert snap.read_text(encoding="utf-8") == "current"
    # 审计日志有 restore 记录
    entry = json.loads(audit_text(audit_path(root)).strip().splitlines()[-1])
    assert entry["command"] == "restore"


def audit_path(root):
    return root / "data" / "self_backups" / "audit.log"


def audit_text(p):
    return p.read_text(encoding="utf-8")


def test_restore_specific_timestamp(root):
    target = root / "note.md"
    target.write_text("current", encoding="utf-8")
    make_backup(root, "note.md", "20260824-120000", content="v1")
    make_backup(root, "note.md", "20260824-130000", content="v2")

    result = sb.restore("note.md", timestamp="20260824-120000")
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "v1"


def test_restore_unknown_timestamp(root):
    make_backup(root, "a.md", "20260824-120000")
    result = sb.restore("a.md", timestamp="19990101-000000")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_restore_no_backup_for_file(root):
    result = sb.restore("ghost.md")
    assert result["ok"] is False
    assert "no backups" in result["error"]


def test_restore_rejects_escape(root):
    result = sb.restore("../../etc/passwd")
    assert result["ok"] is False
    assert "escapes project root" in result["error"]


@pytest.mark.parametrize(
    "path",
    ["tools/file_edit.py", "terminal_manager.py", "self_backup.py", "runtime/terminal_manager.py", "runtime/self_backup.py"],
)
def test_restore_agent_channel_refuses_immutable(root, path):
    make_backup(root, path, "20260824-120000")
    result = sb.restore(path, allow_immutable=False)
    assert result["ok"] is False
    assert "immutable" in result["error"]

    # 人工通道允许
    result2 = sb.restore(path, allow_immutable=True)
    assert result2["ok"] is True


def test_restore_never_touches_env(root):
    make_backup(root, ".env", "20260824-120000")
    result = sb.restore(".env", allow_immutable=True)
    assert result["ok"] is False
    assert ".env" in result["error"]


def test_restore_creates_missing_target(root):
    """源文件已不存在时也能恢复（如被误删后找回）"""
    make_backup(root, "tools/lost.py", "20260824-120000", content="recovered = True")
    result = sb.restore("tools/lost.py")
    assert result["ok"] is True
    assert (root / "tools/lost.py").read_text(encoding="utf-8") == "recovered = True"
    assert "previous_version_snapshot" not in result


def test_list_and_restore_collision_suffixed_timestamp(root):
    target = root / "prompt" / "soul.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("current", encoding="utf-8")
    make_backup(root, "prompt/soul.md", "20260824-120000", content="v1")
    make_backup(root, "prompt/soul.md", "20260824-120000-abcd", content="v2")

    items = sb.list_backups("prompt/soul.md")
    assert [it["timestamp"] for it in items] == ["20260824-120000", "20260824-120000-abcd"]

    result = sb.restore("prompt/soul.md", timestamp="20260824-120000-abcd")
    assert result["ok"] is True
    assert target.read_text(encoding="utf-8") == "v2"
