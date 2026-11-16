"""运行时配置应用：保存与应用分离，现有回合在唯一叙事流锁内完整结束。"""

import asyncio
import logging
import os
from copy import deepcopy

from agent.loop import AgentLoop
from config.environment import updated_env_keys
from config.store import ConfigStore, fields_for, inherit_change, public_value, remove_value, set_value, value_at
from config.providers import CHAT_NOT_READY, ConfigurationError, connection, resolved_models

logger = logging.getLogger(__name__)


class ConfigurationService:
    def __init__(self, manager, agent_session, heartbeat, *, validator=None, tools=None, restart=None):
        self.manager = manager
        self.session = agent_session
        self.heartbeat = heartbeat
        self.boot = manager.snapshot()
        self.fields = fields_for(self.boot)
        self.store = ConfigStore(manager.files, validator=validator)
        self.tools = tools or {}
        self._write_lock = asyncio.Lock()
        self._task = None
        self.error = None
        self._environment_changed = False
        self.restart = restart
        self._catalog_lock = asyncio.Lock()

    async def discover_models(self, provider_id, revision):
        from config.store import ConfigurationConflict
        from config.catalog import fetch_catalog, import_candidates

        if self._catalog_lock.locked():
            raise ConfigurationError("模型目录正在读取，请稍后再试")
        async with self._catalog_lock:
            loaded = await asyncio.to_thread(self.store.read)
            if loaded[0] != revision:
                raise ConfigurationConflict("配置已被修改，请刷新后再读取模型列表")
            self._ensure_writable()
            # 先校验落盘连接，凭据只在进程内读取；前端不能提交临时地址或密钥。
            await asyncio.to_thread(ConfigStore(self.store.files).validate, loaded[2], [])
            result = await fetch_catalog(connection(loaded[2], provider_id))
            if (await asyncio.to_thread(self.store.read))[0] != revision:
                raise ConfigurationConflict("读取期间配置发生变化，请刷新后重试")
            return {"revision": revision, **import_candidates(loaded[2], provider_id, result)}

    def _ensure_writable(self):
        if getattr(self.session, "_restarting", False) or (self.restart and self.restart.state != "idle"):
            raise ConfigurationError("应用正在准备重启，请恢复后再修改配置")

    def _effective(self, saved):
        effective = self.manager.snapshot()
        for role in list(effective["roles"]):
            if role == "chat" and self._role_ready(saved, role, effective):
                for path, field in self.fields.items():
                    if field.effect != "round" or not path.startswith(f"roles.{role}."):
                        continue
                    value = value_at(saved, path, "__absent__")
                    if value != "__absent__":
                        set_value(effective, path, value)
                    else:
                        parts = path.split(".")
                        target = effective
                        for part in parts[:-1]:
                            target = target.get(part, {})
                        target.pop(parts[-1], None)
        for field in self.fields.values():
            if field.path.startswith("heartbeat."):
                set_value(effective, field.path, value_at(saved, field.path, field.default))
        return effective

    def _role_ready(self, saved, role, active=None):
        active = self.manager.snapshot() if active is None else active
        if role not in saved.get("roles", {}):
            return False
        model_id = saved["roles"][role].get("model")
        model = saved["models"].get(model_id)
        def runtime_values(item):
            return {k: v for k, v in (item or {}).items() if k not in {"models_url", "balance_mode", "balance_url", "label"}}
        return (model is not None and model_id in active["models"]
                and runtime_values(active["models"][model_id]) == runtime_values(model)
                and runtime_values(active["providers"].get(model["provider"]))
                == runtime_values(saved["providers"].get(model["provider"])))

    def _effect(self, saved, path, active=None):
        field = self.fields.get(path)
        if field is None:
            return "restart"
        if field.effect == "immediate":
            return field.effect
        if path.startswith("roles.") and not self._role_ready(saved, path.split(".")[1], active):
            return "restart"
        return field.effect

    async def snapshot(self):
        active = self.manager.snapshot()
        loaded = await asyncio.to_thread(self.store.read)
        result = await asyncio.to_thread(self.store.public, active, loaded=loaded)
        # 文件 IO 期间可能完成应用；在同一事件循环步内重新取运行态，避免陈旧状态停止轮询。
        active = self.manager.snapshot()
        effects = {p: self._effect(loaded[2], p, active) for p in self.fields}
        active_fields = fields_for(active)
        # 主对话固定使用 chat；其他职责的配置在重启后加载。
        for field in result["fields"]:
            original = self.fields.get(field["path"])
            field["effect"] = effects.get(field["path"], "restart")
            definition = original or fields_for(loaded[2])[field["path"]]
            live = value_at(active, field["path"], active_fields.get(field["path"], definition).default)
            saved_value = value_at(loaded[2], field["path"], field["default"])
            if field["effect"] == "immediate":
                live = saved_value  # 目录查询直接读取已保存的元数据，不涉及运行中的模型客户端。
            field.update(active=public_value(definition, live), pending=live != saved_value)
        _, _, saved = loaded
        # 动态字段与默认值的物化均不影响重启状态；未知/未管理字段仍需重启。
        def restart_projection(raw):
            result = deepcopy(raw)
            for provider in result["providers"].values():
                for metadata in ("models_url", "balance_mode", "balance_url"):
                    provider.pop(metadata, None)
            for section in ("providers", "models", "roles"):
                for item in result[section].values():
                    item.pop("label", None)
            for field in self.fields.values():
                if effects.get(field.path) != "round":
                    continue
                parts = field.path.split(".")
                target = result
                parents = []
                for part in parts[:-1]:
                    if not isinstance(target, dict) or part not in target:
                        break
                    parents.append((target, part))
                    target = target[part]
                else:
                    if isinstance(target, dict):
                        target.pop(parts[-1], None)
                    for parent, key in reversed(parents):
                        if parent[key] == {}:
                            del parent[key]
            return result
        running = getattr(self.session._loop, "model_config", None)
        model_id = active.get("roles", {}).get(self.session.role, {}).get("model")
        result.update(active_role=self.session.role,
                      active_model={"name": running.name, "provider": running.provider,
                                    "model_id": model_id,
                                    "provider_id": active["models"][model_id]["provider"] if model_id else None,
                                    "api_style": running.api_style, "auth": running.auth,
                                     "service_tier": running.service_tier, "tools": running.tool_names or []} if running else None,
                      tools={role: self.tools.get(role, self.tools.get("chat", [])) for role in saved["roles"]},
                      applying=self._task is not None and not self._task.done(),
                      error=self.error or (CHAT_NOT_READY if running is None else None),
                      restart_required=self._environment_changed or restart_projection(saved) != restart_projection(self.boot))
        result["restart"] = self.restart.snapshot() if self.restart else {"available": False, "state": "unavailable"}
        return result

    async def select_model(self, model_id):
        """仅更新 chat 的模型引用；工具和角色策略保留。"""
        async with self._write_lock:
            self._ensure_writable()
            revision, _, saved = await asyncio.to_thread(self.store.read)
            if model_id not in saved["models"]:
                raise ConfigurationError("模型不存在")
            candidate = deepcopy(saved)
            candidate["roles"]["chat"]["model"] = model_id
            if not self._role_ready(candidate, "chat"):
                raise ConfigurationError("该模型或供应商有修改等待重启，请先重启应用")
            await asyncio.to_thread(self.store.save, {"roles.chat.model": model_id}, revision)
            self.request_apply()
        await self._task
        if self.error:
            raise ConfigurationError(self.error)
        if self.session._loop is None:
            raise ConfigurationError(CHAT_NOT_READY)
        return self.session._loop.model_config.name

    async def prepare_restart(self, revision, reembed=False):
        if self.restart is None:
            raise ConfigurationError("当前启动方式未装配内置重启")
        async with self._write_lock:
            self._ensure_writable()
            loaded = await asyncio.to_thread(self.store.read)
            if loaded[0] != revision:
                from config.store import ConfigurationConflict
                raise ConfigurationConflict("配置已被修改，请刷新后再重启")
            declared = [p for p in fields_for(loaded[2]) if value_at(loaded[2], p, "__absent__") != "__absent__"]
            # 可选后台角色仍延迟检查凭据；重启预检不把未使用角色变成启动必需项。
            checker = ConfigStore(self.store.files)
            await asyncio.to_thread(checker.validate, loaded[2], declared)
            if self.store.validator:
                await asyncio.to_thread(self.store.validator, loaded[2], [])
            if "chat" in resolved_models(loaded[2]):
                try:
                    self.manager.get_model("chat", raw_config=loaded[2])
                except ValueError:
                    raise ConfigurationError("主对话认证配置不完整，请检查后再重启") from None
            if reembed:
                role = value_at(loaded[2], "memory.embedding.model_role", "embedding")
                try:
                    self.manager.get_model(role, raw_config=loaded[2])
                except ValueError:
                    raise ConfigurationError("向量嵌入角色的配置或认证不完整") from None
        return self.restart.snapshot()

    async def preview(self, changes, revision):
        result = await asyncio.to_thread(self.store.preview, changes, revision)
        _, _, saved = await asyncio.to_thread(self.store.read)
        for path, value in changes.items():
            if inherit_change(path, value):
                remove_value(saved, path)
            else:
                set_value(saved, path, value)
        active = self.manager.snapshot()
        for change in result["changes"]:
            change["effect"] = self._effect(saved, change["path"], active)
        return result

    async def restore(self, backup, revision, apply=False):
        async with self._write_lock:
            if apply:
                self._ensure_writable()
            result = await asyncio.to_thread(self.store.restore, backup, revision, apply=apply)
            if apply:
                self.request_apply()
        if apply:
            result["status"] = await self.snapshot()
        return result

    async def structure(self, operations, revision, apply=False):
        async with self._write_lock:
            if apply:
                self._ensure_writable()
            result = await asyncio.to_thread(self.store.structure, operations, revision, apply=apply)
        if apply:
            result["status"] = await self.snapshot()
        return result

    async def save(self, changes, revision):
        async with self._write_lock:
            self._ensure_writable()
            result = await asyncio.to_thread(self.store.save, changes, revision)
            self.request_apply()
        return {**result, "status": await self.snapshot()}

    async def save_environment_value(self, path, value, revision):
        async with self._write_lock:
            self._ensure_writable()
            name = await asyncio.to_thread(self.store.save_environment_value, path, value, revision)
            if path.endswith(".credentials_env"):
                from config.google_auth import protect_credential_env
                protect_credential_env(name)
            os.environ[name] = value
            updated_env_keys.add(name)
            self._environment_changed = True
        return {"status": await self.snapshot()}

    def request_apply(self):
        self.error = None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._apply())

    async def _apply(self):
        try:
            async with self.session._lock:
                async with self._write_lock:
                    # 合并等待期间的全部保存，使用最新磁盘版本，不应用旧草稿。
                    _, _, saved = await asyncio.to_thread(self.store.read)
                    effective = self._effective(saved)
                    previous = self.manager.snapshot()
                    changed = [p for p, f in self.fields.items() if self._effect(saved, p, previous) == "round"
                               and value_at(effective, p, f.default) != value_at(previous, p, f.default)]
                    if not changed:
                        return
                    self.store.validate(effective, changed)
                    current = self.session._loop
                    new_loop = None
                    if current is not None or any(p.startswith(f"roles.{self.session.role}.") for p in changed):
                        cfg = self.manager.get_model(self.session.role, raw_config=effective)
                        if current is None or cfg != current.model_config:
                            new_loop = AgentLoop(model_config=cfg, callback=self.session._on_event)
                    interval = value_at(effective, "heartbeat.interval_minutes", 30)
                    previous_interval = self.heartbeat.interval_seconds / 60
                    # 以下发布不 await，其他任务不会看到只更新了一半的配置。
                    try:
                        self.manager.replace_config(effective)
                        self.session.session_manager.set_model_role(self.session.role)
                        if self.heartbeat.interval_seconds != interval * 60:
                            self.heartbeat.set_interval(interval)
                        self.heartbeat.configure(effective.get("heartbeat"))
                        prepare = getattr(self.session.session_manager, 'apply_model_config', None)
                        if new_loop is not None and prepare is not None:
                            prepare(cfg)
                        if new_loop is not None:
                            self.session._loop = new_loop
                    except Exception:
                        self.manager.replace_config(previous)
                        self.session.session_manager.set_model_role(self.session.role)
                        if self.heartbeat.interval_seconds != previous_interval * 60:
                            self.heartbeat.set_interval(previous_interval)
                        self.heartbeat.configure(previous.get("heartbeat"))
                        if new_loop is not None:
                            await new_loop.client.close()
                        raise
                    if new_loop is not None and current is not None:
                        try:
                            await current.client.close()
                        except Exception:
                            logger.warning("旧模型连接关闭失败（新配置已生效）")
        except asyncio.CancelledError:
            raise
        except Exception:
            # 客户端异常可能包含 URL 或认证信息，不回传异常正文。
            self.error = "动态应用失败，原运行配置保留；请检查配置后重试，或使用页面中的重启应用。"
            logger.error("配置动态应用失败；请核对模型与工具依赖配置")

    async def close(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
