"""
Tentacle 唯一启动入口（WebUI + 插件/心跳/定时任务）

Web、插件通道和心跳共享一个 AgentSession，不要启动多个实例。
云端生产由 systemd 管理；本地开发启动方式如下：

用法：
    ./start.sh            # 一键启动（推荐）
    python run_web.py     # 或直接运行（需先激活 .venv）
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，确保 tools/ 下的模块能直接导入顶层模块
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

from agent.session import AgentSession
from attachments.manager import get_file_manager
from memory.concept_audit import ConceptAuditLog
from memory.ladybug_store import LadybugStore
from memory.reembedding import reembed_saved_config
from runtime.goals import get_goal_manager
from runtime.heartbeat import Heartbeat
from runtime.logging import setup_logging
from runtime.paths import get_paths, state_path
from config.manager import ConfigManager
from config.environment import updated_env_keys
from runtime.scheduler import get_scheduler
from agent.session_manager import SessionManager
from agent.prompt import SystemPromptBuilder
from runtime.terminal_manager import get_terminal_manager
from runtime.usage import get_usage_tracker
from subagents import SubagentManager
from subagents.config import LEGACY_TOOLS, SubagentConfig
from runtime.tool_loader import ToolLoader, ToolStartupError
from plugins.manager import PluginManager
from agent.events import EventFanout

logger = logging.getLogger(__name__)

from web.adapter import ConnectionManager, WebAdapter
from web.server import create_app
from web.settings import get_web_settings
from config.store import ConfigurationError
from attachments.downloads import download_settings
from config.service import ConfigurationService
from config.providers import balance_connection, file_connections, resolved_models, validate_structure
from runtime.control import REEMBED_REVISION_ENV, RestartController
from runtime.home_backup import HomeBackupManager, apply_pending_restore
from runtime.maintenance import MaintenanceController


dotenv_keys: set[str] = set()


def _load_raw_config(manager: ConfigManager) -> dict:
    """读取默认配置与 config.yaml 用户覆盖合并后的配置。"""
    if getattr(manager, "_raw_config", None) is None:
        manager._load_config()
    raw = manager._raw_config
    if not isinstance(raw, dict):
        raise ValueError("config.yaml 必须是配置字典")
    return raw


async def _noop_callback(event):
    """临时回调占位符，在 Adapter 创建前事件不会派发"""
    pass


async def main():
    """初始化全套运行时，并发启动 Web 服务与心跳模块"""
    paths = get_paths()
    os.chdir(paths.state_root)
    # 在读取 .env、配置和打开任何数据库之前应用已确认的备份恢复。
    pending_restore_result = apply_pending_restore(paths.state_root)
    inherited = set(os.environ)
    load_dotenv(paths.dotenv_file)
    dotenv_keys.update(os.environ.keys() - inherited)
    setup_logging()
    logger.info("WebUI 启动")
    if pending_restore_result is not None:
        logger.info("启动期备份恢复结果: %s", pending_restore_result.get("status"))

    manager = ConfigManager()
    # 只消费页面确认的单次请求，重算期间宿主、QQ 和后台任务尚未启动。
    reembed_revision = os.environ.pop(REEMBED_REVISION_ENV, None)
    last_reembed = await reembed_saved_config(manager.files, reembed_revision) if reembed_revision else None
    raw = _load_raw_config(manager)
    validate_structure(raw)
    download_settings((raw.get('media_inputs') or {}).get('download'))
    subagent_config = SubagentConfig.from_raw(raw.get("subagents"), coding=raw.get("coding"))
    tool_loader = ToolLoader()
    tool_loader.load()
    # 已保存的旧角色仍可通过配置校验；coding 任务的模型由 DSH 管理。
    role_tools = {"subagent": LEGACY_TOOLS}
    if subagent_config.model_role not in {"chat", "compress", "embedding", "memory_extractor"}:
        role_tools[subagent_config.model_role] = LEGACY_TOOLS
    selected_tools = tool_loader.validate_config(raw, role_tools=role_tools)
    webui_cfg = raw.get("webui", {}) or {}
    host = webui_cfg.get("host", "0.0.0.0")
    port = int(webui_cfg.get("port", 8420))
    heartbeat_interval = float((raw.get("heartbeat", {}) or {}).get("interval_minutes", 30))

    session_manager = SessionManager(manager)

    # 图片文件管理（DeepSeek Files API）：配置 + session 切换时清理远端文件
    file_manager = get_file_manager()
    file_manager.configure(file_connections(raw))
    session_manager.set_on_session_inactive(
        lambda session: file_manager.cleanup_session_files(session.history)
    )

    # 余额只使用明确指定的供应商连接，图片存储不再决定余额 API。
    get_usage_tracker().configure(balance_connection(raw))

    builder = SystemPromptBuilder(manager=manager)
    subagents = SubagentManager(subagent_config)

    # Web 平台的共享叙事流
    agent_session = AgentSession(
        manager=manager,
        callback=_noop_callback,
        session_manager=session_manager,
        platform="web",
        system_prompt_builder=builder,
    )
    maintenance = MaintenanceController(agent_session)
    agent_session.maintenance = maintenance
    plugin_manager = PluginManager(
        services={
            "builder": builder,
            "chronicle": agent_session.chronicle,
            "goal_manager": get_goal_manager(),
            "file_manager": file_manager,
            "workspace": get_paths().workspace,
            "agent_session": agent_session,
        }
    )
    tool_context = {"session_manager": session_manager, "builder": builder,
                    "subagent_manager": subagents, "maintenance": maintenance,
                    "plugin_manager": plugin_manager}
    tool_loader.bind(tool_context, selected_tools)
    LadybugStore().set_concept_audit(ConceptAuditLog(agent_session.chronicle.db_path.parent / "concept_dedup.db"))

    heartbeat = Heartbeat(
        agent_session=agent_session,
        interval_minutes=heartbeat_interval,
        settings=raw.get("heartbeat"),
    )
    agent_session.heartbeat = heartbeat

    conn_manager = ConnectionManager()
    web_adapter = WebAdapter(
        session=agent_session,
        conn_manager=conn_manager,
        heartbeat=heartbeat,
        platform="web",
    )
    event_fanout = EventFanout(web_adapter._callback)
    agent_session.set_callback(event_fanout)
    web_adapter.set_event_loop(asyncio.get_running_loop())

    async def _subagent_result(event, claim):
        return await agent_session.run_subagent_result(event, claim)

    # 定时任务复用工具发送及共享心跳服务，不维护独立出站通道。
    scheduler = get_scheduler(
        catch_up_grace_hours=float((raw.get("scheduler", {}) or {}).get("catch_up_grace_hours", 24)),
        interval_catch_up_grace_hours=float(
            (raw.get("scheduler", {}) or {}).get("interval_catch_up_grace_hours", 6)
        ),
    )
    scheduler.set_heartbeat(heartbeat)
    plugin_manager._services["scheduler"] = scheduler
    plugin_manager._services["memory_store"] = LadybugStore()
    plugin_manager.bind_event_fanout(event_fanout)
    await plugin_manager.startup(raw)

    # 注入真实终端配置，并把实时输出桥接到 WebSocket（前端 xterm.js 渲染）
    terminal_mgr = get_terminal_manager()
    term_cfg = _load_raw_config(manager).get("terminal", {}) or {}
    terminal_mgr.configure(
        shell=term_cfg.get("shell") or None,
        cwd=str(state_path(term_cfg["cwd"])) if term_cfg.get("cwd") else str(paths.workspace),
        default_timeout=term_cfg.get("default_timeout"),
        deny_commands=term_cfg.get("deny_commands"),
    )
    terminal_mgr.set_output_hook(web_adapter._on_terminal_output)

    def validate_configuration(candidate, changed):
        try:
            selected = tool_loader.validate_config(candidate, role_tools=role_tools)
            errors = tool_loader.registry.dependency_errors(selected, tool_context)
            if errors:
                raise ConfigurationError("工具依赖尚未启用，请先在主机配置依赖并重启")
            roles = {path.split(".")[1] for path in changed if path.startswith("roles.")} & resolved_models(candidate).keys()
            for role in roles:
                manager.get_model(role, raw_config=candidate)
        except ConfigurationError:
            raise
        except ToolStartupError as exc:
            raise ConfigurationError(str(exc)) from None
        except Exception:
            raise ConfigurationError("模型或工具配置校验失败，请检查认证变量及工具名称") from None

    restart = RestartController(agent_session, last_reembed)
    configuration_service = ConfigurationService(
        manager, agent_session, heartbeat, validator=validate_configuration,
        tools=tool_loader.configuration_tools(raw, role_tools=role_tools),
        restart=restart,
    )
    agent_session.configuration_service = configuration_service
    backup_manager = HomeBackupManager(paths.state_root, ladybug_store=LadybugStore(), config_manager=manager)

    app = create_app(
        conn_manager=conn_manager,
        web_adapter=web_adapter,
        agent_session=agent_session,
        heartbeat=heartbeat,
        session_manager=session_manager,
        chronicle=agent_session.chronicle,
        settings=get_web_settings(),
        goal_manager=get_goal_manager(),
        configuration_service=configuration_service,
        subagent_manager=subagents,
        maintenance_controller=maintenance,
        plugin_manager=plugin_manager,
        restart_controller=restart,
        backup_manager=backup_manager,
    )

    web_task = restart_task = None
    try:
        # 工具装配与配置校验全部完成后，才允许恢复任务和启动通知。
        await subagents.start(notify=_subagent_result)
        await heartbeat.run()
        await scheduler.run()
        await backup_manager.start()
        web_task = asyncio.create_task(web_adapter.run(app, host, port))
        restart_task = asyncio.create_task(restart.ready.wait())
        watched = {web_task, restart_task}
        done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        if web_task in done:
            await web_task
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭...")
    finally:
        web_adapter.request_stop()
        await configuration_service.close()
        await backup_manager.stop()
        await heartbeat.stop()
        await scheduler.stop()
        await plugin_manager.shutdown()
        await subagents.stop()
        tool_loader.unbind()
        if web_task is not None:
            await asyncio.gather(web_task, return_exceptions=True)
        if restart_task is not None:
            restart_task.cancel()
            await asyncio.gather(restart_task, return_exceptions=True)
        await agent_session.close()
        await LadybugStore().close()
        await restart.close()
        await maintenance.close()
        logger.info("WebUI 关闭")
    if restart.ready.is_set() and restart.reembed_revision is not None:
        os.environ[REEMBED_REVISION_ENV] = restart.reembed_revision
    return restart.ready.is_set()


if __name__ == "__main__":
    if asyncio.run(main()):
        # 清理完成后 exec；保留 PID/systemd 身份、工作目录和解释器，不需 sudo。
        # 让新进程重读 .env，避免上一次加载的值覆盖更新或被删除的密钥。
        reload_keys = dotenv_keys | updated_env_keys
        restart_env = {key: value for key, value in os.environ.items()
                       if key not in reload_keys or key == REEMBED_REVISION_ENV}
        os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve())], restart_env)
