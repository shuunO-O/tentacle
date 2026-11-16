"""供应商连接、模型目录与角色解析。纯配置逻辑，不读凭据、不创建客户端。"""

from copy import deepcopy
from dataclasses import asdict, dataclass
import re
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


class CredentialConfigurationError(ConfigurationError):
    """A configured role cannot run until its model and credentials are available."""


CHAT_NOT_READY = "主对话尚未就绪，请在应用配置中绑定模型，并在主机配置凭据"


@dataclass(frozen=True)
class ProviderSpec:
    label: str
    base_url: str
    auth: str
    api_style: str
    protocols: tuple[str, ...]
    compatibility: str = "openai_compatible"
    user_id_in_extra_body: bool = False
    responses_auto_tool_choice: bool = False
    chat_file_ids: bool = False
    balance: bool = False


PROVIDERS = {
    "openai_compatible": ProviderSpec("自定义 OpenAI 兼容连接", "", "api_key", "chat", ("chat", "responses", "systemone")),
    "typesafe": ProviderSpec("TypeSafe / JEV", "https://api.typesafe.ai/v1", "api_key", "systemone", ("systemone",)),
    "openrouter": ProviderSpec("OpenRouter", "https://openrouter.ai/api/v1", "api_key", "chat", ("chat", "responses", "systemone")),
    "openai": ProviderSpec("OpenAI", "https://api.openai.com/v1", "api_key", "responses", ("chat", "responses")),
    "deepseek": ProviderSpec("DeepSeek", "https://api.deepseek.com", "api_key", "chat", ("chat", "responses"),
                             "deepseek", True, True, True, True),
    "google_developer": ProviderSpec("Google Gemini API", "https://generativelanguage.googleapis.com/v1beta",
                                     "api_key", "interactions", ("interactions", "gemini_generate"), "google"),
    "google_vertex": ProviderSpec("Google Vertex AI", "https://aiplatform.googleapis.com/v1beta1",
                                  "adc", "interactions", ("interactions", "gemini_generate"), "google"),
}

CONNECTION_KEYS = {"base_url", "api_key_env", "proxy", "project", "project_env", "location", "auth", "credentials_env", "gcs_base_url"}
MODEL_KEYS = {"name", "api_style", "vision", "audio", "video", "context_window"}
PARAMETERS = {"temperature", "reasoning_effort", "stream", "max_retries", "retry_base_seconds",
              "service_tier", "response_format"}
POLICIES = {"max_iterations", "tools", "user_id", "explicit_cache", "interactions_stateful", "selectable"}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9-]{0,63}\Z")
MODEL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z_0-9-]{0,63}(?:/[A-Za-z_0-9-]{1,64})?\Z")
CONCEPT_ROLES = ("concept_alignment", "concept_dedup")
_LEGACY_CONCEPT_KEYS = {"base_url", "api_key_env", "model", "proxy"}


def concept_connection(raw, section):
    """从模型与角色目录解析记忆对齐/去重角色连接。"""
    config = _mapping(_mapping(raw.get("memory", {}), "memory").get(section, {}), "memory." + section)
    if _LEGACY_CONCEPT_KEYS & config.keys():
        raise ConfigurationError(f"memory.{section} 不再支持直连字段，请改用 model_role 绑定模型角色")
    role = config.get("model_role", section)
    roles = raw.get("roles")
    if isinstance(roles, dict) and role in roles and all(k in raw for k in ("providers", "models")):
        models = resolved_models(raw)
        if role not in models:
            return None
        model = models[role]
        if model.get("api_style") != "systemone" or model.get("auth", "api_key") != "api_key":
            raise ConfigurationError(f"memory.{section} 需要使用 System One 协议及 API Key 认证的模型角色")
        return {
            "base_url": model.get("base_url", ""),
            "api_key_env": model.get("api_key_env", ""),
            "proxy": model.get("proxy"),
            "model": model["name"],
        }
    if "model_role" in config and role != section:
        raise ConfigurationError(f"memory.{section} 引用了不存在的模型角色")
    return None


def compatibility(provider):
    # google 映射仅保留接口兼容标识解析；未知字符串采用通用协议行为。
    if provider == 'google':
        return PROVIDERS['google_developer']
    return PROVIDERS.get(provider, PROVIDERS["openai_compatible"]) if isinstance(provider, str) else PROVIDERS["openai_compatible"]


def catalog():
    return {key: asdict(spec) for key, spec in PROVIDERS.items()}


def automatic_identifier(kind, value, existing):
    """按类型/模型连接生成可引用的 ID；备注不参与标识生成。"""
    limit = 64
    if kind == "providers":
        base = value.get("type") or "provider"
        if not isinstance(base, str):
            base = "provider"
        if base == "openai_compatible":
            base = "provider"
    elif kind == "models":
        name = re.sub(r"[^A-Za-z_0-9-]+", "-", str(value.get("name", ""))).strip("-") or "model"
        provider = str(value.get("provider") or "provider")
        base = provider + "/" + name[:64]
        limit = len(provider) + 1 + 64
    else:
        base = "role"
    if kind != "models":
        base = re.sub(r"[^A-Za-z_0-9-]+", "-", base).strip("-") or "item"
    if not re.match(r"[A-Za-z_]", base):
        base = "id-" + base
    base = base[:limit]
    candidate, suffix = base, 2
    while candidate in existing:
        tail = "-" + str(suffix)
        candidate = base[:limit - len(tail)] + tail
        suffix += 1
    return candidate


def _mapping(value, path):
    if not isinstance(value, dict):
        raise ConfigurationError(f"{path} 必须为配置对象")
    return value


def _keys(value, allowed, path):
    if set(value) - allowed:
        raise ConfigurationError(f"{path} 包含不支持的字段")


def _identifier(value, kind=None):
    pattern = MODEL_IDENTIFIER if kind == "models" else IDENTIFIER
    if not isinstance(value, str) or not pattern.fullmatch(value):
        if kind == "models":
            raise ConfigurationError("模型标识须为原标识或 provider/model 格式，各段最长 64 字符；不支持点号和空格")
        raise ConfigurationError("标识须以字母或下划线开头，只含字母、数字、下划线和连字符，最长 64 字符")
    return value


def connection(raw, provider_id):
    providers = _mapping(raw.get("providers"), "providers")
    if not isinstance(provider_id, str) or provider_id not in providers:
        raise ConfigurationError("模型引用了不存在的供应商连接")
    item = _mapping(providers[provider_id], "providers." + provider_id)
    kind = item.get("type")
    if not isinstance(kind, str) or kind not in PROVIDERS:
        raise ConfigurationError("供应商连接必须选择支持的接入类型")
    spec = PROVIDERS[kind]
    return {"base_url": spec.base_url, "auth": spec.auth, **deepcopy(item), "provider": spec.compatibility}


def resolved_models(raw):
    """返回已绑定角色的完整模型配置，未绑定角色仍保留在原始角色表中。"""
    _mapping(raw.get("providers"), "providers")
    models = _mapping(raw.get("models"), "models")
    result = {}
    for role, policy in _mapping(raw.get("roles"), "roles").items():
        _mapping(policy, "roles." + role)
        model_id = policy.get("model")
        if model_id is None:
            continue
        if not isinstance(model_id, str) or model_id not in models:
            raise ConfigurationError(f"roles.{role} 引用了不存在的模型")
        model = _mapping(models[model_id], "models." + model_id)
        provider = connection(raw, model.get("provider"))
        spec = PROVIDERS[provider["type"]]
        result[role] = {key: value for key, value in provider.items() if key in CONNECTION_KEYS}
        result[role].update(provider=spec.compatibility, api_style=model.get("api_style", spec.api_style))
        result[role].update({key: deepcopy(model[key]) for key in MODEL_KEYS if key in model})
        result[role].update(deepcopy(model.get("defaults", {})))
        result[role].update(deepcopy(policy.get("overrides", {})))
        result[role].update({key: deepcopy(policy[key]) for key in POLICIES if key in policy})
    return result


def normalized(raw):
    result = deepcopy(raw)
    result["models"] = resolved_models(raw)
    return result


def references(raw):
    models = {key: [] for key in raw.get("models", {})}
    providers = {key: {"models": [], "roles": [], "uploads": [], "balance": False} for key in raw.get("providers", {})}
    for model_id, model in raw.get("models", {}).items():
        if model.get("provider") in providers:
            providers[model["provider"]]["models"].append(model_id)
    for role, policy in raw.get("roles", {}).items():
        model_id = policy.get("model")
        if model_id in models:
            models[model_id].append(role)
            provider_id = raw["models"][model_id].get("provider")
            if provider_id in providers:
                providers[provider_id]["roles"].append(role)
    for uploader, item in ((raw.get("files") or {}).get("uploaders") or {}).items():
        if isinstance(item, dict) and item.get("provider_ref") in providers:
            providers[item["provider_ref"]]["uploads"].append(uploader)
    balance = (raw.get("usage") or {}).get("balance_provider")
    if balance in providers:
        providers[balance]["balance"] = True
    return {"providers": providers, "models": models}


def validate_structure(raw):
    if "config_version" in raw and (type(raw["config_version"]) is not int or raw["config_version"] != 2):
        raise ConfigurationError("不支持的 config_version")
    for section in ("providers", "models", "roles"):
        entries = _mapping(raw.get(section), section)
        if len(entries) > 100:
            raise ConfigurationError(f"{section} 最多支持 100 个条目")
        for key, item in entries.items():
            _identifier(key, section)
            _mapping(item, section + "." + key)
    for key, item in raw["providers"].items():
        _keys(item, CONNECTION_KEYS | {"type", "label", "models_url", "balance_mode", "balance_url"}, "providers." + key)
        provider = connection(raw, key)
        spec = PROVIDERS[provider["type"]]
        if provider["auth"] != spec.auth:
            raise ConfigurationError(f"providers.{key} 的认证方式与接入类型不匹配")
        for field in ("credentials_env", "gcs_base_url"):
            value = item.get(field, "")
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                raise ConfigurationError(f"providers.{key}.{field} 必须为单行文本")
            if value and provider["type"] != "google_vertex":
                raise ConfigurationError(f"providers.{key}.{field} 仅适用于 Vertex AI")
        if item.get("credentials_env") and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", item["credentials_env"]):
            raise ConfigurationError("credentials_env 必须为环境变量名，不能填写密钥或路径")
        if item.get("gcs_base_url"):
            bucket = urlsplit(item["gcs_base_url"])
            if (bucket.scheme != "gs" or not bucket.hostname or bucket.username or bucket.password
                    or bucket.query or bucket.fragment or bucket.netloc != bucket.hostname):
                raise ConfigurationError("gcs_base_url 必须为 gs://BUCKET[/PREFIX]")
        mode = item.get("balance_mode", "auto")
        if not isinstance(mode, str) or mode not in {"auto", "none", "deepseek"}:
            raise ConfigurationError(f"providers.{key} 的余额查询方式无效")
        if mode == "deepseek" and provider["auth"] != "api_key":
            raise ConfigurationError("DeepSeek 格式余额查询需要 API Key 认证的连接")
        if not provider.get("base_url"):
            raise ConfigurationError(f"providers.{key} 需要 API 地址")
    for key, item in raw["models"].items():
        _keys(item, MODEL_KEYS | {"provider", "label", "defaults"}, "models." + key)
        provider = connection(raw, item.get("provider"))
        if item.get("api_style", PROVIDERS[provider["type"]].api_style) not in PROVIDERS[provider["type"]].protocols:
            raise ConfigurationError(f"models.{key} 的 API 协议与供应商接入类型不匹配")
        if not isinstance(item.get("name"), str) or not item["name"]:
            raise ConfigurationError(f"models.{key} 需要模型名称")
        _keys(_mapping(item.get("defaults", {}), "模型默认参数"), PARAMETERS, "模型默认参数")
        # 未被角色引用的模型也必须通过协议检查。
    for key, item in raw["roles"].items():
        _keys(item, POLICIES | {"model", "overrides", "label"}, "roles." + key)
        _keys(_mapping(item.get("overrides", {}), "角色参数覆盖"), PARAMETERS, "角色参数覆盖")
        _keys(_mapping(item.get("tools", {}), "角色工具配置"), {"enabled"}, "角色工具配置")
        if "explicit_cache" in item:
            _mapping(item["explicit_cache"], "角色缓存配置")
    resolved = resolved_models(raw)
    for section in CONCEPT_ROLES:
        if section in (raw.get("memory") or {}):
            concept_connection(raw, section)
    for role, model in resolved.items():
        if role in {"chat", "compress", "memory_extractor", "subagent", "embedding"} and model.get("api_style") == "systemone":
            raise ConfigurationError(f"roles.{role} 不能使用 JEV System One 模型")
    balance = _mapping(raw.get("usage", {}), "usage").get("balance_provider")
    if balance is not None and not PROVIDERS[connection(raw, balance)["type"]].balance:
        raise ConfigurationError("余额查询目前仅支持 DeepSeek 类型连接")
    file_connections(raw)
    plugins_cfg = raw.get("plugins")
    if plugins_cfg is not None:
        plugins_map = _mapping(plugins_cfg, "plugins")
        for sub in ("items", "mcp"):
            if sub in plugins_map and plugins_map[sub] is not None:
                _mapping(plugins_map[sub], f"plugins.{sub}")


def balance_connection(raw):
    """余额连接必须明确配置；不从文件存储配置推导地址或凭据。"""
    provider_id = (raw.get("usage") or {}).get("balance_provider")
    if not provider_id:
        return {}
    provider = connection(raw, provider_id)
    mode = provider.get("balance_mode", "auto")
    if mode == "none":
        return {}
    if mode != "deepseek" and not PROVIDERS[provider["type"]].balance:
        raise ConfigurationError("该连接尚未实现余额查询")
    return provider


def file_connections(raw):
    """解析默认上传器；独立 Vertex 存储启用按目标连接惰性上传。"""
    files = deepcopy(raw.get("files", {}))
    # 附件先保留本地，实际请求时才按目标连接上传，避免跨账号多传一份。
    files["defer_uploads"] = any(
        p.get("type") == "google_vertex" and (p.get("credentials_env") or p.get("gcs_base_url"))
        for p in raw.get("providers", {}).values())
    for uploader, config in (files.get("uploaders") or {}).items():
        if "provider_ref" in config:
            provider = connection(raw, config["provider_ref"])
            expected = {"deepseek_files": "deepseek", "gemini_files": "google_developer"}.get(uploader)
            if expected != provider["type"]:
                raise ConfigurationError("上传器与所引用的供应商接入类型不匹配")
            if set(config) - {"provider_ref"}:
                raise ConfigurationError("上传器使用 provider_ref 时不能同时填写连接字段")
            files["uploaders"][uploader] = {k: v for k, v in provider.items() if k in {"base_url", "api_key_env", "proxy"}}
    return files
