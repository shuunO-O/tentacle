"""
terminal_manager 保护区写命令拦截测试

覆盖 _protected_write_check / _is_protected_target：
重定向、rm/mv/cp 等写命令、sed -i、sudo 包装、管道分段、误伤场景（读操作放行）。
"""

import pytest

from runtime.terminal_manager import TerminalManager


@pytest.fixture
def tm():
    return TerminalManager()


# ---------- 放行（不应误伤） ----------


def test_allow_read_operations(tm):
    assert tm._protected_write_check("cat .env") is None
    assert tm._protected_write_check("grep pattern terminal_manager.py") is None
    assert tm._protected_write_check("ls data/self_backups/") is None
    assert tm._protected_write_check("head -50 self_backup.py") is None
    assert tm._protected_write_check("cp a.txt b.txt && rm c.txt") is None


@pytest.mark.parametrize("path", ["runtime/terminal_manager.py", "runtime/self_backup.py"])
def test_moved_safety_modules_keep_write_protection_and_read_access(tm, path):
    assert tm._protected_write_check(f"echo x > {path}")
    assert tm._protected_write_check(f"rm {path}")
    assert tm._protected_write_check(f"cat {path}") is None


def test_allow_empty(tm):
    assert tm._protected_write_check("") is None
    assert tm._protected_write_check("echo hello") is None


# ---------- 重定向 ----------


def test_block_redirect_to_env(tm):
    reason = tm._protected_write_check("echo KEY=1 > .env")
    assert reason and "安全审查" in reason


def test_block_append_redirect_to_immutable(tm):
    reason = tm._protected_write_check("echo x >> tools/file_edit.py")
    assert reason and "安全审查" in reason
    reason_legacy = tm._protected_write_check("echo x >> tools/str_replace_editor.py")
    assert reason_legacy and "安全审查" in reason_legacy


def test_block_stderr_redirect(tm):
    reason = tm._protected_write_check("run.sh 2> .env")
    assert reason and "安全审查" in reason


# ---------- 写命令 ----------


def test_block_rm_env(tm):
    assert tm._protected_write_check("rm .env")


def test_block_rm_self_backups(tm):
    assert tm._protected_write_check("rm -rf data/self_backups/")


def test_block_mv_overwrite_immutable(tm):
    assert tm._protected_write_check("mv evil.py terminal_manager.py")


def test_block_cp_into_immutable(tm):
    """cp 源 目标 形式：目标在写命令参数中即拦（保守策略）"""
    assert tm._protected_write_check("cp x.py self_backup.py")


def test_block_sudo_wrapped_write(tm):
    assert tm._protected_write_check("sudo rm .env")


def test_block_tee_and_sed_i(tm):
    assert tm._protected_write_check("echo x | tee .env")
    assert tm._protected_write_check("sed -i 's/a/b/' self_backup.py")


def test_block_second_segment_after_pipe(tm):
    reason = tm._protected_write_check("gen.sh | tee data/self_backups/audit.log")
    assert reason and "audit" in reason.lower() or "self_backups" in str(reason)
