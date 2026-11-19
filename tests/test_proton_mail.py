"""内置 proton_mail 插件测试：二进制探测、工具逻辑、脱敏与心跳简报。"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
import sys

import pytest


PLUGIN_DIR = Path(__file__).parent.parent / "plugins" / "bundled" / "proton_mail"
INSTALL_COMMAND = "curl -fsSL https://raw.githubusercontent.com/roman-16/proton-cli/main/scripts/install.sh | sh"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("proton_mail_plugin", PLUGIN_DIR / "plugin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.tools: dict[str, object] = {}
        self.hooks: dict[str, object] = {}
        self.logger = logging.getLogger("test.proton_mail")

    def register_tool(self, name, description, parameters, func, *, is_async=None, auto_expose=True):
        self.tools[name] = func

    def on(self, event, handler, *, priority=100):
        self.hooks[event] = handler


def _write_fake_proton(tmp_path: Path) -> Path:
    script = tmp_path / "proton"
    script.write_text(
        f"#!{sys.executable}\n"
        """import json
import sys

args = [item for item in sys.argv[1:] if item not in {"--no-input", "--no-color"}]
if args[:2] == ["account", "get"]:
    print(json.dumps({
        "email": "user@example.com",
        "username": "user",
        "session": "valid",
        "unlocked": True,
        "used_space": 1048576,
        "max_space": 10485760,
    }))
elif args[:3] == ["mail", "messages", "list"]:
    if "--unread" in args:
        print(json.dumps({
            "total": 1,
            "messages": [
                {"id": "unread123456", "from_name": "Alice", "from_address": "alice@example.com",
                 "subject": "Unread subject", "time": 1700000000, "unread": True},
            ],
        }))
    else:
        print(json.dumps({
            "total": 2,
            "messages": [
                {"id": "message123456", "from_name": "Bob", "from_address": "bob@example.com",
                 "subject": "Read subject", "time": 1700000000, "unread": False},
                {"id": "message234567", "from_name": "", "from_address": "carol@example.com",
                 "subject": "", "time": 1700000001, "unread": True},
            ],
        }))
elif args[:3] == ["mail", "messages", "get"]:
    print("Subject: Read subject\\n\\nHello from Proton.")
elif args[:3] == ["mail", "messages", "send"]:
    print(json.dumps({"sent": True}))
else:
    print("unexpected args: " + " ".join(args), file=sys.stderr)
    sys.exit(2)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def proton_module(monkeypatch, tmp_path):
    module = _load_plugin()
    monkeypatch.setattr(module, "_SYSTEM_PROTON_PATHS", ())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", "")
    return module


def test_binary_detection_prefers_config_then_path_then_home(proton_module, monkeypatch, tmp_path):
    assert proton_module._find_proton() is None

    home_binary = tmp_path / "home" / ".local" / "bin" / "proton"
    home_binary.parent.mkdir(parents=True)
    home_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    home_binary.chmod(0o755)
    assert proton_module._find_proton() == str(home_binary)

    configured = tmp_path / "custom-proton"
    configured.write_text("#!/bin/sh\n", encoding="utf-8")
    configured.chmod(0o755)
    assert proton_module._find_proton(str(configured)) == str(configured)

    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    path_binary = path_dir / "proton"
    path_binary.write_text("#!/bin/sh\n", encoding="utf-8")
    path_binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(path_dir))
    assert proton_module._find_proton() == str(path_binary)


@pytest.mark.parametrize("operation", ["status", "list", "read", "send"])
def test_missing_binary_returns_install_and_login_guidance(proton_module, operation):
    ctx = FakeContext()
    proton_module.setup(ctx)
    tool = ctx.tools["proton_mail"]

    result = tool({"operation": operation, "message_id": "x", "to": "a@example.com", "subject": "s", "body": "b"})

    assert INSTALL_COMMAND in result
    assert "proton account login" in result
    assert "未检测到 proton CLI" in result


def test_tool_status_list_read_send_with_fake_cli(proton_module, tmp_path):
    executable = _write_fake_proton(tmp_path)
    ctx = FakeContext({"executable": str(executable)})
    proton_module.setup(ctx)
    tool = ctx.tools["proton_mail"]

    status = tool({"operation": "status"})
    assert "user@example.com" in status
    assert "1.00 MB / 10.00 MB" in status

    messages = tool({"operation": "list", "limit": 10})
    assert "【收件箱邮件】（共 2 封，展示前 2 封）" in messages
    assert "Read subject" in messages
    assert "（无主题）" in messages
    assert "carol@example.com" in messages

    unread = tool({"operation": "list", "unread_only": True})
    assert "【未读邮件】（共 1 封，展示前 1 封）" in unread
    assert "[未读] Unread subject" in unread

    content = tool({"operation": "read", "message_id": "message123456"})
    assert "Hello from Proton." in content

    sent = tool({"operation": "send", "to": "dave@example.com", "subject": "Hi", "body": "Body"})
    assert "邮件已成功发送至 dave@example.com" in sent


def test_tool_rejects_invalid_operations_and_empty_send_fields(proton_module, tmp_path):
    executable = _write_fake_proton(tmp_path)
    ctx = FakeContext({"executable": str(executable)})
    proton_module.setup(ctx)
    tool = ctx.tools["proton_mail"]

    assert "不支持的操作" in tool({"operation": "delete"})
    assert "收件人 to 不能为空" in tool({"operation": "send", "subject": "s", "body": "b"})
    assert "主题 subject 不能为空" in tool({"operation": "send", "to": "a@example.com", "body": "b"})
    assert "正文 body 不能为空" in tool({"operation": "send", "to": "a@example.com", "subject": "s"})


def test_heartbeat_briefing_and_limit_zero(proton_module, tmp_path):
    executable = _write_fake_proton(tmp_path)
    ctx = FakeContext({"executable": str(executable), "heartbeat_unread_limit": 5})
    proton_module.setup(ctx)
    hook = ctx.hooks["before_heartbeat"]

    result = hook({})
    assert "1 封未读邮件" in result["briefing"]
    assert "Alice" in result["briefing"]
    assert "Unread subject" in result["briefing"]

    disabled_ctx = FakeContext({"executable": str(executable), "heartbeat_unread_limit": 0})
    proton_module.setup(disabled_ctx)
    assert disabled_ctx.hooks["before_heartbeat"]({}) == {}


def test_manifest_matches_bundled_plugin_contract():
    from plugins.manifest import parse_manifest

    manifest = parse_manifest(PLUGIN_DIR)
    assert manifest.id == "proton_mail"
    assert manifest.entrypoint == "plugin.py:setup"
    assert "tools:register" in manifest.permissions
    assert "hooks:mutate" in manifest.permissions
    assert set(manifest.config_schema) == {"executable", "heartbeat_unread_limit"}


def test_plugin_text_has_no_personal_hardcoding():
    for filename in ("plugin.py", "manifest.yaml", "README.md"):
        text = (PLUGIN_DIR / filename).read_text(encoding="utf-8")
        assert "苏白" not in text
        assert "suubai@proton.me" not in text
