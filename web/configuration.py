"""配置路由沿用应用的设备和密码鉴权。"""

import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from config.store import ConfigurationConflict, ConfigurationError
from web.schemas import (
    ConfigurationActionResponse,
    ConfigurationDiscoverResponse,
    ConfigurationRestartResponse,
    ConfigurationSnapshotResponse,
)


class ChangeBody(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    changes: dict[str, object] = Field(min_length=1, max_length=300)


class RestoreBody(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    backup: str = Field(max_length=80)
    apply: bool = False


class StructureBody(BaseModel):
    revision: str = Field(min_length=64, max_length=64)
    operations: list[dict] = Field(min_length=1, max_length=50)
    apply: bool = False


class RevisionBody(BaseModel):
    revision: str = Field(min_length=64, max_length=64)


class RestartBody(RevisionBody):
    reembed: bool = False


class DiscoverBody(RevisionBody):
    provider: str = Field(min_length=1, max_length=64)


def configuration_router(service, auth_dep):
    router = APIRouter(prefix="/api/configuration", dependencies=[auth_dep])

    async def call(action, *args):
        if service is None:
            raise HTTPException(503, "当前实例未装配配置管理模块")
        try:
            return await action(*args)
        except ConfigurationConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ConfigurationError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError:
            raise HTTPException(503, "配置文件操作失败或正被其他进程修改；请稍后刷新") from None

    @router.post(
        "/restart",
        status_code=202,
        response_model=ConfigurationRestartResponse,
        response_model_exclude_unset=True,
    )
    async def restart(body: RestartBody, background: BackgroundTasks):
        await call(service.prepare_restart if service else None, body.revision, body.reembed)
        if not service.restart.reserve(body.revision if body.reembed else None):
            raise HTTPException(409, "应用正在准备重启")
        background.add_task(service.restart.request)
        return service.restart.snapshot()

    @router.get(
        "",
        response_model=ConfigurationSnapshotResponse,
        response_model_exclude_unset=True,
    )
    async def snapshot():
        return await call(service.snapshot if service else None)

    @router.post(
        "/discover-models",
        response_model=ConfigurationDiscoverResponse,
        response_model_exclude_unset=True,
    )
    async def discover_models(body: DiscoverBody):
        return await call(service.discover_models if service else None, body.provider, body.revision)

    @router.post(
        "/preview",
        response_model=ConfigurationActionResponse,
        response_model_exclude_unset=True,
    )
    async def preview(body: ChangeBody):
        return await call(service.preview if service else None, body.changes, body.revision)

    @router.put(
        "",
        response_model=ConfigurationActionResponse,
        response_model_exclude_unset=True,
    )
    async def save(body: ChangeBody):
        return await call(service.save if service else None, body.changes, body.revision)

    @router.post(
        "/environment",
        response_model=ConfigurationActionResponse,
        response_model_exclude_unset=True,
    )
    async def save_environment(request: Request):
        # 避免请求校验错误回显输入值，也拒绝浏览器能跨站提交的普通表单。
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "请使用 JSON 提交凭据")
        payload = bytearray()
        async for chunk in request.stream():
            if len(payload) + len(chunk) > 65536:
                raise HTTPException(413, "凭据请求过大")
            payload.extend(chunk)
        try:
            body = json.loads(payload)
            if (not isinstance(body, dict) or set(body) != {"path", "value", "revision"}
                    or not all(isinstance(body[key], str) for key in body)
                    or len(body["revision"]) != 64 or len(body["path"]) > 256):
                raise ValueError
        except (ValueError, RecursionError):
            raise HTTPException(422, "凭据请求格式无效") from None
        return await call(service.save_environment_value if service else None,
                          body["path"], body["value"], body["revision"])

    @router.post(
        "/apply",
        response_model=ConfigurationSnapshotResponse,
        response_model_exclude_unset=True,
    )
    async def apply():
        async def action():
            service.request_apply()
            return await service.snapshot()
        return await call(action)

    @router.post(
        "/structure",
        response_model=ConfigurationActionResponse,
        response_model_exclude_unset=True,
    )
    async def structure(body: StructureBody):
        return await call(service.structure if service else None, body.operations, body.revision, body.apply)

    @router.post(
        "/restore",
        response_model=ConfigurationActionResponse,
        response_model_exclude_unset=True,
    )
    async def restore(body: RestoreBody):
        return await call(service.restore if service else None, body.backup, body.revision, body.apply)

    return router
