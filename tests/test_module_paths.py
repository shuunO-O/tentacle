"""模块归组后，数据/附件/提示词路径与共享单例仍指向同一项目；不读取运行内容。"""

from pathlib import Path
import subprocess
import sys

import pytest

from agent import loop, protocol, session
from agent.prompt import SystemPromptBuilder
from config.manager import ConfigManager
from config.store import ConfigStore, fields_for
from memory import embeddings
from runtime import tool_loader, tool_registry
from runtime.paths import get_paths, prompt_template, state_path


def test_application_and_persistent_state_have_distinct_roots(tmp_path):
    root = Path(__file__).resolve().parents[1]
    paths = get_paths()
    assert paths.app_root == root and paths.state_root == tmp_path
    assert paths.defaults_file == root / "config.defaults.yaml"
    assert paths.config_file == tmp_path / "config/config.yaml"
    assert paths.dotenv_file == tmp_path / "config/.env"
    assert state_path("data/files/file.pdf") == tmp_path / "data/files/file.pdf"
    builder = SystemPromptBuilder()
    assert builder.prompt_dir == tmp_path / "prompt"
    assert builder.skills_dir == tmp_path / "skills"
    assert builder.preview_path.parent == tmp_path / "tmp"


def test_custom_prompt_overrides_version_template(tmp_path):
    name = "system_prompt_compressor.md"
    assert prompt_template(name) == get_paths().app_root / "prompt" / name
    path = tmp_path / "prompt" / name
    path.parent.mkdir()
    path.write_text("synthetic custom template")
    assert prompt_template(name) == path


def test_tentacle_home_must_be_explicit_and_separate(monkeypatch):
    monkeypatch.delenv("TENTACLE_HOME")
    with pytest.raises(ValueError, match="绝对路径"):
        get_paths()
    monkeypatch.setenv("TENTACLE_HOME", str(Path(__file__).resolve().parents[1]))
    with pytest.raises(ValueError, match="分离"):
        get_paths()


def test_default_store_writes_config_and_credentials_to_home(tmp_path):
    (tmp_path / "config").mkdir()
    store = ConfigStore()
    assert store.read()[2]["roles"]["chat"]["model"] is None
    store.structure([{"op": "create", "kind": "providers", "id": "fixture",
                      "value": {"type": "deepseek", "api_key_env": "FIXTURE"}}],
                    store.read()[0], apply=True)
    store.save_environment_value("providers.fixture.api_key_env", "synthetic-key", store.read()[0])
    assert "synthetic-key" not in (tmp_path / "config/config.yaml").read_text()
    assert "synthetic-key" in (tmp_path / "config/.env").read_text()
    assert list((tmp_path / "data/config_backups").glob("*.yaml"))
    assert all("synthetic-key" not in path.read_text()
               for path in (tmp_path / "data/config_backups").glob("*.yaml"))


def test_all_consumers_share_config_manager_and_tool_registry_identity():
    assert session.ConfigManager is embeddings.ConfigManager is ConfigManager
    assert loop.registry is tool_loader.global_registry is tool_registry.registry
    assert not hasattr(protocol, "ConfigManager")


def test_module_moves_preserve_persisted_media_setting_keys():
    fields = fields_for({"providers": {}, "models": {}, "roles": {}})
    assert {"media_inputs.auto_bilibili", "media_inputs.download.backend", "media_inputs.download.base_url"} <= fields.keys()
    assert not any(key.startswith("attachments.") for key in fields)


def test_startup_imports_defer_optional_heavy_dependencies():
    probe = (
        "import importlib, json, pathlib, sys, types\n"
        "if sys.platform == 'win32':\n"
        "    for m in ('pty', 'termios', 'fcntl'):\n"
        "        sys.modules.setdefault(m, types.ModuleType(m))\n"
        "import run_web\n"
        "for p in sorted(pathlib.Path('tools').glob('*.py')):\n"
        "    if p.stem != '__init__':\n"
        "        importlib.import_module(f'tools.{p.stem}')\n"
        "loaded = sorted({name.split('.')[0] for name in sys.modules})\n"
        "print(json.dumps(loaded))\n"
    )
    root = Path(__file__).resolve().parents[1]
    out = subprocess.check_output([sys.executable, "-c", probe], cwd=root, text=True)
    import json

    loaded = set(json.loads(out))
    deferred = {
        "aiohttp",
        "croniter",
        "cryptography",
        "dateutil",
        "google",
        "httpx2",
        "ladybug",
        "mcp",
        "openai",
        "requests",
        "urllib3",
        "websockets",
        "yarl",
    }
    assert not (loaded & deferred), f"启动导入提前加载了按需依赖: {sorted(loaded & deferred)}"

