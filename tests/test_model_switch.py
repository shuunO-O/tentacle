import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from agent.session import AgentSession
from agent.commands import parse_command, run_command
from config.service import ConfigurationService
from config.manager import ConfigManager


class FakeLoop:
    def __init__(self, model_config, callback=None):
        self.model_config = model_config
        self.client = self
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_SWITCH_TEST_KEY", "synthetic")
    monkeypatch.setattr('config.service.AgentLoop', FakeLoop)
    raw = {"config_version": 2,
           "providers": {"demo": {"type": "deepseek", "api_key_env": "MODEL_SWITCH_TEST_KEY"}},
           "models": {"fast": {"provider": "demo", "name": "model-fast", "context_window": 10000},
                      "backup": {"provider": "demo", "name": "model-backup", "context_window": 20000}},
           "roles": {"chat": {"model": "fast", "max_iterations": 50, "tools": {"enabled": ["get_time"]}},
                     "compress": {"model": "fast", "max_iterations": 3, "selectable": True}}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    manager = ConfigManager.__new__(ConfigManager)
    manager._raw_config, manager._config_path = deepcopy(raw), str(path)
    result = AgentSession.__new__(AgentSession)
    result.manager, result.callback, result.role = manager, None, "chat"
    result._lock = asyncio.Lock()
    result.session_manager = SimpleNamespace(set_model_role=lambda role: None)
    result._loop = FakeLoop(manager.get_model("chat"))
    result.configuration_service = ConfigurationService(
        manager, result, SimpleNamespace(interval_seconds=1800, configure=Mock()))
    return result


async def test_switch_catalog_model_keeps_chat_policy_and_persists_reference(agent):
    previous = agent._loop
    assert await agent.switch_model("backup") == "model-backup"
    assert agent.role == "chat" and agent.model_id == "backup"
    assert agent._loop.model_config.max_iterations == 50
    assert agent._loop.model_config.tool_names == ["get_time"]
    assert previous.closed
    saved = agent.configuration_service.store.read()[2]
    assert saved["roles"]["chat"]["model"] == "backup"
    assert saved["roles"]["compress"]["model"] == "fast"
    assert agent.manager.get_model("compress").max_iterations == 3
    fields = (await agent.configuration_service.snapshot())["fields"]
    assert not any(f["path"].endswith(".selectable") for f in fields)


async def test_model_commands_use_model_ids_and_fixed_chat_role(agent):
    assert parse_command("/MODEL use Mixed_Case_ID") == "model use Mixed_Case_ID"
    assert await run_command("model current", agent) == "[命令] 当前模型: fast (model-fast)，角色: chat"
    assert await run_command("model list", agent) == "[命令] 可切换模型: fast *, backup"
    assert await run_command("model use backup", agent) == "[命令] 已切换模型: backup (model-backup)，角色: chat"
    assert (await run_command("model use compress", agent)).startswith("[命令] 切换失败：")


async def test_pending_provider_blocks_model_switch_without_overwriting_chat(agent):
    store = agent.configuration_service.store
    store.save({"providers.demo.proxy": "http://proxy.test:8080"}, store.read()[0])
    with pytest.raises(ValueError, match="等待重启"):
        await agent.switch_model("backup")
    assert agent.model_id == "fast"
    assert store.read()[2]["roles"]["chat"]["model"] == "fast"
