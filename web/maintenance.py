"""Web entry point for the shared host maintenance controller."""

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from config.store import ConfigurationConflict, ConfigurationError
from runtime.maintenance import MaintenanceUnavailable
from runtime.maintenance_protocol import valid_id
from web.schemas import MaintenanceOperationDto, MaintenanceStatusResponse


class MaintenanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["check", "update", "restart"]
    version: str | None = Field(default=None, max_length=40)
    revision: str | None = Field(default=None, min_length=64, max_length=64)


def maintenance_router(controller, auth_dep, settings, configuration_service=None):
    router = APIRouter(prefix="/api/maintenance", dependencies=[auth_dep])

    def has_auth():
        return settings.has_password or settings.requires_device

    @router.get(
        "",
        response_model=MaintenanceStatusResponse,
        response_model_exclude_unset=True,
    )
    async def status(operation_id: str | None = None):
        if operation_id is not None and not valid_id(operation_id):
            raise HTTPException(422, "操作 ID 格式无效")
        return {**controller.snapshot(operation_id), "web_allowed": has_auth()}

    @router.post(
        "",
        status_code=202,
        response_model=MaintenanceOperationDto,
        response_model_exclude_unset=True,
    )
    async def request_operation(body: MaintenanceBody, request: Request, background: BackgroundTasks):
        if not has_auth():
            raise HTTPException(403, "请先启用 Web 密码或设备鉴权")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "请使用 JSON 提交维护请求")
        try:
            if body.action == "restart" and configuration_service is not None:
                if body.revision is None:
                    raise HTTPException(422, "重启前请刷新配置版本")
                await configuration_service.prepare_restart(body.revision)
            operation = controller.reserve(body.action, body.version)
        except ConfigurationConflict as error:
            raise HTTPException(409, str(error)) from None
        except ConfigurationError as error:
            raise HTTPException(422, str(error)) from None
        except MaintenanceUnavailable as error:
            raise HTTPException(503, str(error)) from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        async def start_after_response():
            controller.start()

        background.add_task(start_after_response)
        return operation

    return router
