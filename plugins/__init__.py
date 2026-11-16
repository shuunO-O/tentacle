"""Tentacle 插件与 MCP 扩展引擎。"""

from plugins.context import PluginContext
from plugins.manager import PluginManager, PluginState
from plugins.manifest import ConfigFieldSpec, PluginManifest, parse_manifest, qualify_tool_name
from plugins.permissions import PERMISSION_CATALOG, PermissionDeniedError, PluginManifestError

__all__ = [
    "ConfigFieldSpec",
    "PERMISSION_CATALOG",
    "PermissionDeniedError",
    "PluginContext",
    "PluginManager",
    "PluginManifest",
    "PluginManifestError",
    "PluginState",
    "parse_manifest",
    "qualify_tool_name",
]
