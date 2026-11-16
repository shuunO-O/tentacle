"""Configuration for external coding tasks."""

from dataclasses import dataclass, field
from pathlib import Path

from runtime.paths import get_paths, state_path


# Older local overrides can still contain the retired read-only agent settings.
_LEGACY_OPTIONS = {
    "prompt_path", "command_timeout_seconds", "max_snapshot_bytes", "docker_binary",
    "docker_host", "sandbox_image", "sandbox_memory_mb", "sandbox_cpus", "sandbox_pids",
    "sandbox_tmp_mb", "read_roots", "sqlite_databases", "query_timeout_seconds",
    "web_timeout_seconds", "web_max_bytes", "web_read_rules",
}
_CODING_OPTIONS = {"dsh_binary", "dsh_home"}
_LEGACY_CODING_OPTIONS = {"repo_path", "workspaces_path"}
LEGACY_TOOLS = {
    "terminal_run", "file_list", "file_read", "skills_list", "skill_view", "sqlite_query",
    "graph_read", "public_search", "web_read", "get_time", "request_input",
}


@dataclass
class SubagentConfig:
    enabled: bool = False
    model_role: str = "subagent"  # Used only to recognize legacy tool lists.
    db_path: Path = field(default_factory=lambda: state_path("data/subagents.db"))
    workspace_root: Path = field(default_factory=lambda: get_paths().workspace)
    dsh_home: Path = field(default_factory=lambda: state_path("data/dsh"))
    dsh_binary: str = "dsh"
    max_concurrency: int = 1
    max_pending_turns: int = 50
    max_turns_per_task: int = 100
    max_notification_depth: int = 8
    max_runtime_seconds: float = 1800
    wait_timeout_seconds: float = 60
    max_output_chars: int = 65536
    max_context_chars: int = 30000
    max_query_rows: int = 200

    @classmethod
    def from_raw(cls, raw: dict | None, root: Path | None = None, *, coding: dict | None = None):
        root = root or get_paths().state_root
        if raw is not None and not isinstance(raw, dict):
            raise ValueError("subagents 必须为配置对象")
        if coding is not None and (not isinstance(coding, dict)
                                   or coding.keys() - _CODING_OPTIONS - _LEGACY_CODING_OPTIONS):
            raise ValueError("coding 必须为配置对象，且只接受 dsh_binary、dsh_home")
        values = {key: value for key, value in (raw or {}).items() if key not in _LEGACY_OPTIONS}
        unknown = values.keys() - (cls.__dataclass_fields__.keys() - _CODING_OPTIONS - {"workspace_root"})
        if unknown:
            raise ValueError(f"未知子代理配置项: {', '.join(sorted(unknown))}")
        values.update({key: value for key, value in (coding or {}).items() if key in _CODING_OPTIONS})
        for key, default in (("db_path", "data/subagents.db"), ("dsh_home", "data/dsh")):
            value = values.get(key, default)
            if not isinstance(value, str) or not value.strip():
                section = "subagents" if key == "db_path" else "coding"
                raise ValueError(f"{section}.{key} 必须为路径")
            path = Path(value).expanduser()
            values[key] = (root / path).resolve()
        config = cls(workspace_root=root.resolve() / "workspace", **values)
        if type(config.enabled) is not bool:
            raise ValueError("subagents.enabled 必须为布尔值")
        for name in ("dsh_binary", "model_role"):
            value = getattr(config, name)
            if not isinstance(value, str) or not value.strip() or value.startswith("-"):
                raise ValueError(f"subagents.{name} 无效")
        if ("/" in config.dsh_binary or "\\" in config.dsh_binary) and not Path(config.dsh_binary).is_absolute():
            raise ValueError("coding.dsh_binary 须为命令名或绝对路径")
        for name in ("max_concurrency", "max_pending_turns", "max_turns_per_task", "max_notification_depth",
                     "max_output_chars", "max_context_chars", "max_query_rows"):
            value = getattr(config, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"subagents.{name} 必须为正整数")
        for name in ("max_runtime_seconds", "wait_timeout_seconds"):
            value = getattr(config, name)
            if type(value) not in (int, float) or not 0 < value < float("inf"):
                raise ValueError(f"subagents.{name} 必须为正数")
        if (config.dsh_home.is_relative_to(config.workspace_root)
                or config.workspace_root.is_relative_to(config.dsh_home)):
            raise ValueError("DSH_HOME 必须与 workspace 分离")
        return config
