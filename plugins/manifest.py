"""插件 manifest.yaml 解析、路径越界检查与工具命名规范。"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Any

import yaml

from plugins.permissions import PluginManifestError, describe_permissions, validate_permissions


MANIFEST_FILENAME = "manifest.yaml"
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CONFIG_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
SAFE_TOOL_CHARS_RE = re.compile(r"[^A-Za-z0-9_-]")
CONFIG_TYPES = {"string", "integer", "number", "boolean", "env", "proxy", "catalog_url", "string_list"}
RESERVED_PLUGIN_IDS = {"bundled"}


def qualify_tool_name(prefix: str, item_id: str, raw_name: str) -> str:
    """将插件/MCP 工具名映射为满足 ToolRegistry `[A-Za-z0-9_-]{1,64}` 的限定名。"""
    clean_prefix = SAFE_TOOL_CHARS_RE.sub("_", prefix.strip()) or "ext"
    clean_id = SAFE_TOOL_CHARS_RE.sub("_", item_id.strip()) or "item"
    clean_raw = SAFE_TOOL_CHARS_RE.sub("_", raw_name.strip()).strip("_") or "tool"
    candidate = f"{clean_prefix}_{clean_id}__{clean_raw}"
    if len(candidate) <= 64:
        return candidate
    digest = hashlib.sha1(f"{prefix}:{item_id}:{raw_name}".encode("utf-8")).hexdigest()[:6]
    return f"{candidate[:57]}_{digest}"


def resolve_plugin_subpath(
    plugin_root: Path,
    rel_path: str,
    *,
    allow_dir: bool = False,
    allow_file: bool = True,
) -> Path:
    """安全解析插件目录内相对路径，阻止目录穿越和跳出根目录的符号链接。"""
    if not isinstance(rel_path, str) or not rel_path.strip() or "\x00" in rel_path:
        raise PluginManifestError(f"无效的插件内部路径: {rel_path!r}")
    normalized = rel_path.strip().replace("\\", "/")
    if normalized.startswith("/") or normalized in {".", ".."}:
        raise PluginManifestError(f"插件路径必须为目录内相对路径: {rel_path!r}")
    root_resolved = plugin_root.resolve()
    target = (plugin_root / normalized).resolve()
    if not target.is_relative_to(root_resolved) or target == root_resolved:
        raise PluginManifestError(f"插件路径越界: {rel_path!r}")
    if not target.exists():
        raise PluginManifestError(f"插件路径不存在: {rel_path}")
    if target.is_dir() and not allow_dir:
        raise PluginManifestError(f"期望文件而非目录: {rel_path}")
    if target.is_file() and not allow_file:
        raise PluginManifestError(f"期望目录而非文件: {rel_path}")
    return target


@dataclass(frozen=True)
class ConfigFieldSpec:
    """插件 config_schema 单字段定义。"""

    key: str
    label: str
    type: str = "string"
    default: Any = None
    required: bool = False
    choices: tuple[Any, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    multiline: bool = False
    nullable: bool = False
    help: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "required": self.required,
            "choices": list(self.choices) if self.choices is not None else None,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "multiline": self.multiline,
            "nullable": self.nullable,
            "help": self.help,
        }


@dataclass(frozen=True)
class PluginManifest:
    """插件 manifest.yaml 结构化定义。"""

    id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    entrypoint: str | None = None
    permissions: tuple[str, ...] = ()
    requires_env: tuple[str, ...] = ()
    optional_env: tuple[str, ...] = ()
    skills_dir: str | None = None
    prompt_file: str | None = None
    config_schema: dict[str, ConfigFieldSpec] = field(default_factory=dict)
    root_dir: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "entrypoint": self.entrypoint,
            "permissions": list(self.permissions),
            "permission_details": describe_permissions(self.permissions),
            "requires_env": list(self.requires_env),
            "optional_env": list(self.optional_env),
            "skills_dir": self.skills_dir,
            "prompt_file": self.prompt_file,
            "config_schema": {k: v.to_dict() for k, v in self.config_schema.items()},
        }


def _parse_config_schema(raw_schema: object) -> dict[str, ConfigFieldSpec]:
    if raw_schema is None:
        return {}
    if not isinstance(raw_schema, dict):
        raise PluginManifestError("config_schema 必须为映射对象")
    specs: dict[str, ConfigFieldSpec] = {}
    for key, spec_raw in raw_schema.items():
        if not isinstance(key, str) or not CONFIG_KEY_RE.fullmatch(key):
            raise PluginManifestError(f"config_schema 字段名无效: {key!r}")
        if not isinstance(spec_raw, dict):
            raise PluginManifestError(f"config_schema.{key} 必须为对象")
        field_type = spec_raw.get("type", "string")
        if field_type not in CONFIG_TYPES:
            raise PluginManifestError(f"config_schema.{key}.type 不支持: {field_type!r}")
        label = spec_raw.get("label") or key
        if not isinstance(label, str) or not label.strip():
            raise PluginManifestError(f"config_schema.{key}.label 必须为非空文本")
        choices_raw = spec_raw.get("choices")
        choices = tuple(choices_raw) if isinstance(choices_raw, (list, tuple)) else None
        default_val = spec_raw.get("default")
        required_val = bool(spec_raw.get("required", False))
        auto_nullable = (default_val is None or default_val == "") and not required_val
        specs[key] = ConfigFieldSpec(
            key=key,
            label=label.strip(),
            type=field_type,
            default=default_val,
            required=required_val,
            choices=choices,
            minimum=spec_raw.get("minimum"),
            maximum=spec_raw.get("maximum"),
            multiline=bool(spec_raw.get("multiline", False)),
            nullable=bool(spec_raw.get("nullable", auto_nullable)),
            help=str(spec_raw.get("help") or ""),
        )
    return specs


def _parse_env_list(raw_env: object, field_name: str = "requires_env") -> tuple[str, ...]:
    if raw_env is None:
        return ()
    if not isinstance(raw_env, (list, tuple)):
        raise PluginManifestError(f"{field_name} 必须为环境变量名列表")
    names: list[str] = []
    for entry in raw_env:
        if not isinstance(entry, str) or not ENV_NAME_RE.fullmatch(entry.strip()):
            raise PluginManifestError(f"{field_name} 变量名无效: {entry!r}")
        cleaned = entry.strip()
        if cleaned not in names:
            names.append(cleaned)
    return tuple(names)


def parse_manifest(plugin_dir: Path) -> PluginManifest:
    """读取并校验 plugin_dir/manifest.yaml。"""
    if not plugin_dir.is_dir():
        raise PluginManifestError(f"插件目录不存在: {plugin_dir}")
    root_resolved = plugin_dir.resolve()
    manifest_path = (plugin_dir / MANIFEST_FILENAME).resolve()
    if not manifest_path.is_relative_to(root_resolved) or not manifest_path.is_file():
        raise PluginManifestError(f"缺少有效的 {MANIFEST_FILENAME}")
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PluginManifestError(f"manifest.yaml 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginManifestError("manifest.yaml 顶层必须为配置对象")

    plugin_id = data.get("id")
    if not isinstance(plugin_id, str) or not PLUGIN_ID_RE.fullmatch(plugin_id) or plugin_id in RESERVED_PLUGIN_IDS:
        raise PluginManifestError("manifest.id 须以小写字母开头，仅含小写字母/数字/_/-，最长 48 字符，且不能使用保留名称")
    if plugin_dir.name != plugin_id:
        raise PluginManifestError(f"插件目录名 {plugin_dir.name!r} 与 manifest.id {plugin_id!r} 不一致")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PluginManifestError("manifest.name 必须为非空字符串")
    version = str(data.get("version") or "").strip()
    if not version:
        raise PluginManifestError("manifest.version 不能为空")

    permissions = validate_permissions(data.get("permissions"))
    requires_env = _parse_env_list(data.get("requires_env"), "requires_env")
    optional_env = _parse_env_list(data.get("optional_env"), "optional_env")
    config_schema = _parse_config_schema(data.get("config_schema"))

    entrypoint = data.get("entrypoint")
    if entrypoint is not None:
        if not isinstance(entrypoint, str) or not entrypoint.strip():
            raise PluginManifestError("manifest.entrypoint 必须为模块路径（如 plugin.py:setup）")
        entrypoint = entrypoint.strip()
        mod_rel = entrypoint.split(":", 1)[0]
        resolve_plugin_subpath(plugin_dir, mod_rel, allow_dir=False, allow_file=True)

    skills_dir = data.get("skills_dir")
    if skills_dir is not None:
        if "skills:provide" not in permissions:
            raise PluginManifestError("声明 skills_dir 需要同时申请 permissions: skills:provide")
        resolve_plugin_subpath(plugin_dir, str(skills_dir), allow_dir=True, allow_file=False)
        skills_dir = str(skills_dir).strip()
    elif "skills:provide" in permissions and (plugin_dir / "skills").is_dir():
        resolve_plugin_subpath(plugin_dir, "skills", allow_dir=True, allow_file=False)
        skills_dir = "skills"

    prompt_file = data.get("prompt_file")
    if prompt_file is not None:
        if "prompts:append" not in permissions:
            raise PluginManifestError("声明 prompt_file 需要同时申请 permissions: prompts:append")
        resolve_plugin_subpath(plugin_dir, str(prompt_file), allow_dir=False, allow_file=True)
        prompt_file = str(prompt_file).strip()
    elif "prompts:append" in permissions and (plugin_dir / "prompts" / "guild.md").is_file():
        resolve_plugin_subpath(plugin_dir, "prompts/guild.md", allow_dir=False, allow_file=True)
        prompt_file = "prompts/guild.md"

    return PluginManifest(
        id=plugin_id,
        name=name.strip(),
        version=version,
        description=str(data.get("description") or "").strip(),
        author=str(data.get("author") or "").strip(),
        entrypoint=entrypoint,
        permissions=permissions,
        requires_env=requires_env,
        optional_env=optional_env,
        skills_dir=skills_dir,
        prompt_file=prompt_file,
        config_schema=config_schema,
        root_dir=plugin_dir,
    )


def bundled_plugins_dir() -> Path:
    """随应用版本打包分发的内置插件目录。"""
    from runtime.paths import APP_ROOT

    return APP_ROOT / "plugins" / "bundled"


def discover_plugin_dirs(
    plugins_dir: Path | None = None,
    bundled_dir: Path | None = None,
) -> list[tuple[str, Path, bool]]:
    """统一扫描内置与用户插件目录，返回 `(plugin_id, path, is_bundled)`（用户目录同名覆盖内置）。"""
    from runtime.paths import state_path

    bdir = bundled_dir if bundled_dir is not None else bundled_plugins_dir()
    udir: Path | None = plugins_dir
    if udir is None:
        try:
            udir = state_path("plugins")
        except ValueError:
            # 离线生成或未设置 TENTACLE_HOME 时仍可发现随程序打包的内置插件。
            udir = None
    found: dict[str, tuple[Path, bool]] = {}

    if bdir.is_dir():
        for entry in sorted(bdir.iterdir()):
            if entry.is_dir() and not entry.name.startswith((".", "_")):
                found[entry.name] = (entry, True)

    if udir is not None and udir.is_dir():
        for entry in sorted(udir.iterdir()):
            if entry.is_dir() and not entry.name.startswith((".", "_")) and entry.name not in RESERVED_PLUGIN_IDS:
                found[entry.name] = (entry, False)

    return [(pid, path, bundled) for pid, (path, bundled) in sorted(found.items())]
