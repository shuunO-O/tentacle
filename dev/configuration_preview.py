"""配置页面的合成后端；只在临时目录写示例，不导入 Agent 或读取项目配置。"""

import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import Depends, HTTPException, Request
from config.store import ConfigurationConflict, ConfigStore, fields_for, set_value, value_at
from config.providers import resolved_models
from web.configuration import configuration_router

SAMPLE = """# Synthetic preview only
config_version: 2
providers:
  example:
    type: openai_compatible
    label: 示例 OpenAI 兼容连接
    auth: api_key
    base_url: https://example.test/v1
    api_key_env: TENTACLE_PREVIEW_ONLY
  typesafe:
    type: typesafe
    label: 示例 TypeSafe / JEV 连接
    auth: api_key
    base_url: https://api.typesafe.ai/v1
    api_key_env: TENTACLE_PREVIEW_JEV
models:
  example/chat:
    label: 主对话示例模型
    provider: example
    name: example-chat
    api_style: chat
    context_window: 131072
    vision: true
    defaults:
      temperature: 0.3
      stream: true
  example/fast:
    label: 轻量推理模型
    provider: example
    name: example-fast
    api_style: chat
    context_window: 65536
    defaults:
      temperature: 0.2
      stream: true
  typesafe/jev:
    label: JEV 概念评分模型
    provider: typesafe
    name: synthetic-jev
    api_style: systemone
roles:
  chat:
    label: 主对话
    model: example/chat
    selectable: true
    tools: {enabled: [get_time, web_search]}
  worker:
    label: 后台任务 Worker
    model: example/fast
    tools: {enabled: []}
  concept_alignment:
    label: 新记忆概念对齐
    model: typesafe/jev
  concept_dedup:
    label: 历史概念去重
    model: typesafe/jev
memory:
  concept_alignment:
    enabled: false
    model_role: concept_alignment
  concept_dedup:
    model_role: concept_dedup
heartbeat:
  interval_minutes: 60
webui:
  port: 8420
"""


def attach_configuration(app, authenticated):
    class PreviewRestart:
        def __init__(self, service):
            self.service = service
            self.instance_id = uuid.uuid4().hex
            self.state = "idle"
            self.reembedding = False
            self.last_reembed = None

        def snapshot(self):
            return {"available": True, "instance_id": self.instance_id, "state": self.state, "synthetic": True,
                    "reembedding": self.reembedding, "last_reembed": self.last_reembed}

        def reserve(self, reembed_revision=None):
            if self.state != "idle":
                return False
            self.state = "waiting"
            self.reembedding = reembed_revision is not None
            return True

        async def request(self):
            self.service.active = deepcopy(self.service.store.read()[2])
            self.instance_id = uuid.uuid4().hex
            self.state = "idle"
            if self.reembedding:
                self.last_reembed = {"state": "succeeded", "message": "记忆向量重算完成（合成演示）。"}
            self.reembedding = False

    class PreviewService:
        def __init__(self):
            self.temporary = None
            self.store = None
            self.active = None

        def initialize(self):
            if self.store is None:
                self.temporary = tempfile.TemporaryDirectory(prefix="tentacle-config-preview-")
                path = Path(self.temporary.name) / "config.yaml"
                path.write_text(SAMPLE, encoding="utf-8")
                self.store = ConfigStore(path)
                self.active = self.store.read()[2]
                self.restart = PreviewRestart(self)

        async def snapshot(self):
            self.initialize()
            data = self.store.public(self.active)
            for field in data["fields"]:
                if field["effect"] == "immediate":
                    field.update(active=field["value"], pending=False)
            data.update(active_role="chat", applying=False, error=None,
                        restart=self.restart.snapshot(),
                        active_model={"name": resolved_models(self.active)["chat"]["name"]},
                        restart_required=any(f["pending"] and f["effect"] == "restart" for f in data["fields"]),
                        tools={role: ["get_time", "web_search", "web_fetch", "browser"] for role in resolved_models(self.store.read()[2])})
            return data

        async def prepare_restart(self, revision, reembed=False):
            self.initialize()
            loaded = self.store.read()
            if loaded[0] != revision:
                raise ConfigurationConflict("配置已被修改，请刷新后再重启")
            declared = [p for p in fields_for(loaded[2]) if value_at(loaded[2], p, "__absent__") != "__absent__"]
            self.store.validate(loaded[2], declared)
            return self.restart.snapshot()

        async def discover_models(self, provider_id, revision):
            from config.catalog import import_candidates
            from config.providers import connection

            self.initialize()
            loaded = self.store.read()
            if loaded[0] != revision:
                raise ConfigurationConflict("配置已被修改，请刷新后重试")
            connection(loaded[2], provider_id)
            result = {"truncated": False, "models": [
                {"name": "example-chat", "label": "已配置示例模型"},
                {"name": "example-discovered-fast", "label": "自动发现示例 · Fast", "context_window": 32768},
                {"name": "example-discovered-large", "label": "自动发现示例 · Large", "context_window": 131072},
            ]}
            return {"revision": revision, **import_candidates(loaded[2], provider_id, result)}

        async def preview(self, changes, revision):
            self.initialize()
            return self.store.preview(changes, revision)

        async def save(self, changes, revision):
            self.initialize()
            result = self.store.save(changes, revision)
            self.request_apply()
            return {**result, "status": await self.snapshot()}

        async def restore(self, backup, revision, apply=False):
            self.initialize()
            result = self.store.restore(backup, revision, apply=apply)
            if apply:
                self.request_apply()
                result["status"] = await self.snapshot()
            return result

        async def structure(self, operations, revision, apply=False):
            self.initialize()
            result = self.store.structure(operations, revision, apply=apply)
            if apply:
                result["status"] = await self.snapshot()
            return result

        def request_apply(self):
            self.initialize()
            saved = self.store.read()[2]
            updated = deepcopy(self.active)
            for path, field in fields_for(self.active).items():
                if field.effect == "round":
                    if path.startswith("roles."):
                        role = path.split(".")[1]
                        policy = saved["roles"].get(role)
                        if policy is None:
                            continue
                        model = saved["models"][policy["model"]]
                        active_provider = {k: v for k, v in self.active["providers"].get(model["provider"], {}).items() if k not in {"models_url", "balance_mode", "balance_url", "label"}}
                        saved_provider = {k: v for k, v in saved["providers"].get(model["provider"], {}).items() if k not in {"models_url", "balance_mode", "balance_url", "label"}}
                        active_model = {k: v for k, v in self.active["models"].get(policy["model"], {}).items() if k != "label"}
                        if active_model != {k: v for k, v in model.items() if k != "label"} or active_provider != saved_provider:
                            continue
                    set_value(updated, path, value_at(saved, path, field.default))
            self.active = updated

    service = PreviewService()

    async def auth(request: Request):
        if not authenticated(request):
            raise HTTPException(401, "预览身份验证")

    app.include_router(configuration_router(service, Depends(auth)))

    @app.post("/__preview__/configuration/restart")
    async def synthetic_restart(request: Request):
        await auth(request)
        service.initialize()
        service.active = deepcopy(service.store.read()[2])
        return {"synthetic": True}

    @app.on_event("shutdown")
    async def cleanup_configuration():
        if service.temporary:
            await asyncio.to_thread(service.temporary.cleanup)
