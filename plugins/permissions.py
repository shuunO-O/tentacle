"""插件能力权限目录与运行时鉴权辅助。"""

from __future__ import annotations

from collections.abc import Iterable


PERMISSION_CATALOG: dict[str, dict[str, str]] = {
    "tools:register": {"level": "low", "summary": "注册工具到模型工具表"},
    "skills:provide": {"level": "low", "summary": "提供 Skill 文档与工作流说明"},
    "prompts:append": {"level": "low", "summary": "向系统提示词 Guild 区块追加插件说明"},
    "commands:register": {"level": "low", "summary": "注册自定义 /slash 斜杠命令"},
    "hooks:observe": {"level": "medium", "summary": "监听对话轮次、工具结果与记忆事件"},
    "hooks:mutate": {"level": "high", "summary": "在轮次前、工具调用前或心跳前注入上下文/修改调用"},
    "fs:plugin_data": {"level": "low", "summary": "读写插件私有数据目录 data/plugins/<id>/"},
    "fs:workspace_read": {"level": "medium", "summary": "读取 Tentacle 工作区及附件文件"},
    "fs:workspace_write": {"level": "high", "summary": "写入工作区并生成可供发送/下载的附件"},
    "memory:read": {"level": "medium", "summary": "检索记忆库与日记近况（LadybugStore）"},
    "memory:write": {"level": "high", "summary": "写入记忆节点、关系或日记（LadybugStore / Chronicle）"},
    "goals:read": {"level": "medium", "summary": "读取当前目标清单（GoalManager）"},
    "goals:write": {"level": "high", "summary": "修改或更新目标状态与备注（GoalManager）"},
    "scheduler:read": {"level": "low", "summary": "查看已预约的定时任务（Scheduler）"},
    "scheduler:write": {"level": "high", "summary": "创建、修改或取消定时任务（Scheduler）"},
    "situation:write": {"level": "high", "summary": "更新当前近况与随手记（keep_in_mind / situation）"},
    "channels:register": {"level": "high", "summary": "注册新的双向消息平台通道（send_message + EventFanout）"},
    "web:routes": {"level": "high", "summary": "挂载受设备与密码鉴权保护的后端 API 子路由"},
    "network:outbound": {"level": "medium", "summary": "访问外部网络或服务"},
    "background:task": {"level": "high", "summary": "随服务启停运行后台长连接、轮询或常驻任务"},
    "mcp:connect": {"level": "medium", "summary": "连接并托管外部 MCP 工具服务"},
}


class PermissionDeniedError(PermissionError):
    """插件尝试调用未在 manifest.yaml 中声明并获批的能力。"""


class PluginManifestError(ValueError):
    """插件清单或目录结构不合法。"""


def validate_permissions(items: object) -> tuple[str, ...]:
    """校验 permissions 字段，返回去重保序的能力元组。"""
    if items is None:
        return ()
    if not isinstance(items, (list, tuple)):
        raise PluginManifestError("permissions 必须为字符串列表")
    seen: list[str] = []
    for raw in items:
        if not isinstance(raw, str) or not raw.strip():
            raise PluginManifestError("permissions 条目必须为非空字符串")
        perm = raw.strip()
        if perm not in PERMISSION_CATALOG:
            raise PluginManifestError(f"未知插件权限: {perm}")
        if perm not in seen:
            seen.append(perm)
    return tuple(seen)


def require_permission(granted: set[str] | frozenset[str], perm: str, plugin_id: str) -> None:
    """运行期强校验权限；缺失时抛出 PermissionDeniedError。"""
    if perm not in granted:
        raise PermissionDeniedError(f"插件 {plugin_id} 缺少权限 {perm}")


def describe_permissions(perms: Iterable[str]) -> list[dict[str, str]]:
    """生成可供主 Agent 与 WebUI 审核展示的权限说明列表。"""
    details: list[dict[str, str]] = []
    for perm in perms:
        meta = PERMISSION_CATALOG.get(perm, {"level": "unknown", "summary": "未登记权限"})
        details.append({
            "permission": perm,
            "level": meta["level"],
            "summary": meta["summary"],
        })
    return details
