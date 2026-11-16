"""启动时发现、注册和装配工具；不启动任务、不执行工具、不检查远端凭据。"""

import importlib
import logging
import pkgutil

from runtime.tool_registry import registry as global_registry
from config.providers import resolved_models

logger = logging.getLogger(__name__)


class ToolStartupError(ValueError):
    pass


class ToolLoader:
    def __init__(self, registry=global_registry, package="tools"):
        self.registry = registry
        self.package = package
        self.modules = {}

    def load(self):
        """按模块名排序发现工具；失败恢复注册表，成功后重复调用不重注册。"""
        if self.modules:
            return
        snapshots = [(self.registry, dict(self.registry._tools))]
        if self.registry is not global_registry:
            snapshots.append((global_registry, dict(global_registry._tools)))
        modules = {}
        module_name = self.package
        try:
            package = importlib.import_module(self.package)
            discovered = sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name)
            for item in discovered:
                if item.name.startswith("_"):
                    continue
                module_name = f"{self.package}.{item.name}"
                if item.ispkg:
                    raise ValueError("工具目录只支持单层模块；辅助包请以 _ 开头")
                module = importlib.import_module(module_name)
                register = getattr(module, "register_tools", None)
                if not callable(register):
                    raise ValueError("缺少 register_tools(registry)；辅助模块请以 _ 开头")
                register(self.registry)
                modules[module_name] = module
        except Exception as exc:
            for registry, snapshot in snapshots:
                registry._tools.clear()
                registry._tools.update(snapshot)
            raise ToolStartupError(f"工具加载失败 [{module_name}]: {exc}") from exc
        self.modules = modules
        logger.info("已加载 %s 个工具模块", len(modules))

    def validate_config(self, raw: dict, *, role_tools=None) -> set[str]:
        """汇总所有角色的工具配置错误，独立执行器使用各自的工具命名空间。"""
        role_tools = role_tools or {}
        errors = []
        selected = set()
        try:
            models = resolved_models(raw)
            models = {**raw["roles"], **models}
        except ValueError as exc:
            raise ToolStartupError(str(exc)) from None
        if not isinstance(models, dict):
            raise ToolStartupError("models 必须是角色配置字典")
        for role, model in models.items():
            path = f"roles.{role}.tools"
            if not isinstance(model, dict) or not isinstance(model.get("tools", {}), dict):
                errors.append(f"{path} 必须是字典")
                continue
            enabled = model.get("tools", {}).get("enabled")
            # 与 AgentLoop 保持一致：未配置或 null 均不向模型暴露工具。
            if enabled is None:
                continue
            if not isinstance(enabled, list):
                errors.append(f"{path}.enabled 必须是工具名列表")
                continue
            seen = set()
            for name in enabled:
                if not isinstance(name, str) or not name:
                    errors.append(f"{path}.enabled 包含无效工具名: {name!r}")
                    continue
                if name in seen:
                    errors.append(f"{path}.enabled 工具名重复: {name}")
                seen.add(name)
                available = name in role_tools[role] if role in role_tools else self.registry.is_builtin(name)
                if not available:
                    if role not in role_tools and self.registry.has(name):
                        errors.append(
                            f"{path}.enabled 不能配置插件或 MCP 工具（已自动暴露，请在 /plugins 管理）: {name}"
                        )
                    else:
                        errors.append(f"{path}.enabled 引用了未注册工具: {name}")
                elif role not in role_tools:
                    selected.add(name)
        if errors:
            raise ToolStartupError("工具配置校验失败：\n" + "\n".join(errors))
        return selected

    def configuration_tools(self, raw: dict, *, role_tools=None) -> dict[str, list[str]]:
        """生成配置页角色可选工具清单（仅含核心内置工具，不含插件与 MCP 动态工具）。"""
        role_tools = role_tools or {}
        core_tools = sorted(self.registry.builtin_names())
        roles = raw["roles"]
        return {
            **{role: list(core_tools) for role in roles},
            **{role: sorted(names) for role, names in role_tools.items()},
        }

    def bind(self, context: dict, selected: set[str]):
        """先校验依赖及绑定声明，再统一注入；None 可用于明确关闭的可选功能。"""
        bindings = []
        errors = self.registry.dependency_errors(selected, context)
        provided = set()
        for module in self.modules.values():
            declared = getattr(module, "TOOL_BINDINGS", {})
            if not isinstance(declared, dict):
                errors.append(f"{module.__name__}.TOOL_BINDINGS 必须是字典")
                continue
            for key, setter in declared.items():
                if not isinstance(key, str) or not key or not callable(setter):
                    errors.append(f"{module.__name__} 无法装配依赖: {key}")
                else:
                    provided.add(key)
                    bindings.append((module.__name__, setter, context.get(key)))
        for name in sorted(selected):
            for key in self.registry._tools[name]["requires"]:
                if key not in provided:
                    errors.append(f"工具 {name} 缺少依赖绑定声明: {key}")
        if errors:
            raise ToolStartupError("工具依赖校验失败：\n" + "\n".join(errors))
        for module_name, setter, value in bindings:
            try:
                setter(value)
            except Exception as exc:
                raise ToolStartupError(f"工具依赖装配失败 [{module_name}]: {exc}") from exc

    def unbind(self):
        for module in self.modules.values():
            for setter in getattr(module, "TOOL_BINDINGS", {}).values():
                setter(None)
