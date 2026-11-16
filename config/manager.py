"""共享配置管理器；按需加载配置，不启动服务。"""

import os
from copy import deepcopy
from pathlib import Path

from config.files import ConfigFiles
from config.providers import CredentialConfigurationError, resolved_models, validate_structure
from agent.protocol import ModelConfig


class ConfigManager:
    """
    配置管理器（单例）

    按需获取模型配置，支持多模型场景（如 chat、compress）。
    """
    _instance = None

    def __new__(cls, config_path: str | Path | None = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config_path = config_path
            cls._instance._raw_config = None
        return cls._instance

    def _load_config(self):
        """加载配置文件"""
        self._raw_config = self.files.read()[2]
        validate_structure(self._raw_config)
        from config.google_auth import protect_credentials
        protect_credentials(self._raw_config)

    def snapshot(self) -> dict:
        """返回当前生效配置的独立副本；调用方不能修改管理器内存。"""
        if self._raw_config is None:
            self._load_config()
        return deepcopy(self._raw_config)

    @property
    def config_path(self) -> Path:
        return self.files.path

    @property
    def files(self) -> ConfigFiles:
        return ConfigFiles(self._config_path)

    def replace_config(self, raw: dict) -> None:
        """由配置应用流程在叙事流锁内发布已经校验的配置。"""
        self._raw_config = deepcopy(raw)
        from config.google_auth import protect_credentials
        protect_credentials(raw)

    def get_model(self, role: str, *, raw_config: dict | None = None) -> ModelConfig:
        """
        根据角色获取模型配置

        Args:
            role: 模型角色，如 "chat"、"compress"

        Returns:
            ModelConfig: 该角色的模型配置

        Raises:
            ValueError: 角色不存在、未绑定模型或认证配置不完整
        """
        raw = self.snapshot() if raw_config is None else raw_config
        models = resolved_models(raw)
        if role not in models:
            if role == "chat" or role in raw.get("roles", {}):
                raise CredentialConfigurationError(f"模型角色 {role} 未绑定模型")
            raise ValueError(f"模型角色不存在: {role}")

        m = models[role]
        auth = m.get("auth", "api_key")
        api_key_env = m.get("api_key_env", "")
        api_key = os.getenv(api_key_env, "") if api_key_env else ""

        if auth == "api_key" and not api_key:
            raise CredentialConfigurationError(f"模型 {role} 的 API Key 未配置，请设置环境变量: {api_key_env}")
        if auth not in ("api_key", "adc"):
            raise ValueError(f"模型 {role} 的 auth 无效: {auth}（可选: api_key, adc）")

        project_env = m.get("project_env", "GOOGLE_CLOUD_PROJECT")
        project = m.get("project") or os.getenv(project_env, "")
        if auth == "adc" and not project:
            raise CredentialConfigurationError(f"模型 {role} 的 Google Cloud project 未配置，请设置环境变量: {project_env}")

        tools_config = m.get("tools", {})
        tool_names = tools_config.get("enabled")
        files_config = raw.get("files", {})
        service_tier = m.get("service_tier")
        if service_tier not in (None, "standard", "priority", "flex"):
            raise ValueError(
                f"模型 {role} 的 service_tier 无效: {service_tier}（可选: standard, priority, flex）"
            )
        heartbeat = raw.get("heartbeat", {}) or {}
        heartbeat_tier = heartbeat.get("service_tier", "flex")
        if heartbeat_tier not in (None, "standard", "priority", "flex"):
            raise ValueError("heartbeat.service_tier 无效（可选: standard, priority, flex, null）")
        heartbeat_timeout = float(heartbeat.get("model_timeout_seconds", 300))
        if not 0 < heartbeat_timeout <= 1800:
            raise ValueError("heartbeat.model_timeout_seconds 必须在 0 到 1800 秒之间")

        model_id = (raw.get("roles", {}).get(role) or {}).get("model", "")
        provider_id = (raw.get("models", {}).get(model_id) or {}).get("provider", "") if model_id else ""
        return ModelConfig(
            provider_id=provider_id, model_id=model_id, role=role,
            provider_type=(raw.get('providers', {}).get(provider_id) or {}).get('type', '') if provider_id else '',
            name=m.get("name", "gpt-4o-mini"),
            base_url=m.get("base_url", "https://api.openai.com/v1"),
            api_key=api_key,
            temperature=m.get("temperature"),
            reasoning_effort=m.get("reasoning_effort", "high"),
            stream=m.get("stream", True),
            max_iterations=m.get("max_iterations", 10),
            tool_names=tool_names,
            response_format=m.get("response_format"),
            user_id=m.get("user_id"),
            api_style=m.get("api_style", "chat"),
            provider=m.get("provider", "deepseek"),
            vision=bool(m.get("vision", False)),
            audio=bool(m.get("audio", False)),
            video=bool(m.get("video", False)),
            chat_file_id_channel=bool(files_config.get("chat_file_id", True)),
            proxy=m.get("proxy"),
            credentials_env=m.get("credentials_env", ""),
            gcs_base_url=m.get("gcs_base_url", ""),
            auth=auth,
            project=project or None,
            location=m.get("location") or os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
            max_retries=max(0, int(m.get("max_retries", 3))),
            retry_base_seconds=max(0.1, float(m.get("retry_base_seconds", 1.0))),
            service_tier=service_tier,
            explicit_cache=m.get("explicit_cache"),
            interactions_stateful=bool(m.get("interactions_stateful", False)),
            heartbeat_service_tier=heartbeat_tier,
            heartbeat_timeout_seconds=heartbeat_timeout,
            heartbeat_max_retries=max(0, int(heartbeat.get("max_retries", 1))),
        )

    def list_model_roles(self) -> list[str]:
        """主对话职责固定为 chat。"""
        return ["chat"]

    def get_memory_config(self) -> dict:
        """
        获取记忆模块配置

        Returns:
            dict: 包含 ladybug、embedding、search 等子配置
        """
        if self._raw_config is None:
            self._load_config()
        return self._raw_config.get("memory", {})
