"""认证、设备绑定与 Web 设置路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response

from web.deps import AppContext
from web.schemas import (
    AuthDevicesResponse,
    AuthStateResponse,
    DeviceAuthBody,
    DeviceAuthResponse,
    EnrollBody,
    LoginBody,
    LoginResponse,
    NewDevicesBody,
    NewDevicesResponse,
    OkResponse,
    SetPasswordBody,
    SetPasswordResponse,
    SettingsBody,
    WebSettingsDto,
)

logger = logging.getLogger(__name__)


def auth_router(ctx: AppContext, auth_dep) -> APIRouter:
    router = APIRouter()
    settings = ctx.settings

    @router.get(
        "/api/auth/state",
        response_model=AuthStateResponse,
        response_model_exclude_unset=True,
    )
    async def auth_state(request: Request):
        """公开：前端据此决定显示登录还是设备绑定遮罩。"""
        return {
            "has_password": settings.has_password,
            "device_auth": settings.requires_device,
            "device_ok": ctx.device_ok(request),
            "freeze": settings.freeze_info() is not None,
        }

    @router.post(
        "/api/auth/enroll",
        response_model=OkResponse,
        response_model_exclude_unset=True,
    )
    async def auth_enroll(body: EnrollBody, response: Response):
        """公开但需一次性绑定码（仅主机脚本 gen-code 产生，一码一设备）。"""
        if not settings.allow_new_devices:
            raise HTTPException(status_code=403, detail="新设备入口已关闭")
        if (settings.freeze_info() or {}).get("invalid"):
            raise HTTPException(status_code=503, detail="冻结文件无法读取，请在主机修复后重新绑定")
        token = settings.enroll_device(body.code, body.name)
        if token is None:
            raise HTTPException(status_code=403, detail="绑定码无效或已过期")
        ctx.set_device_cookie(response, token)
        logger.warning("[auth] 新设备绑定成功")
        return {"ok": True}

    @router.get(
        "/api/auth/devices",
        dependencies=[auth_dep],
        response_model=AuthDevicesResponse,
        response_model_exclude_unset=True,
    )
    async def auth_devices(request: Request):
        """已绑定设备列表（WebUI 只降权：这里没有生成码端点，码仅主机脚本产生）。"""
        return {
            "devices": settings.list_devices(),
            "device_auth": settings.device_auth,
            "device_bound": ctx.has_device_cookie(request),
            "allow_new_devices": settings.allow_new_devices,
            "has_password": settings.has_password,
            "freeze": settings.freeze_info() is not None,
            "pending_code": settings.pending_code_info(),
        }

    @router.delete(
        "/api/auth/devices/{device_id}",
        dependencies=[auth_dep],
        response_model=OkResponse,
        response_model_exclude_unset=True,
    )
    async def auth_revoke_device(device_id: str):
        if not settings.revoke_device(device_id):
            raise HTTPException(status_code=404, detail="设备不存在")
        if ctx.conn_manager is not None:
            await ctx.conn_manager.revalidate()
        logger.warning(f"[auth] 设备已吊销: {device_id[:12]}…")
        return {"ok": True}

    @router.put(
        "/api/auth/device_auth",
        dependencies=[auth_dep],
        response_model=DeviceAuthResponse,
        response_model_exclude_unset=True,
    )
    async def auth_set_device_auth(body: DeviceAuthBody, request: Request, response: Response):
        """
        开启设备验证可以复用已绑定 cookie；登记新设备必须凭主机绑定码。
        冻结期间禁止切换设备验证，恢复只走主机 unlock。
        """
        if settings.freeze_info() is not None:
            raise HTTPException(status_code=403, detail="紧急冻结期间无法切换设备验证，请在主机解除冻结")
        if body.enabled and not ctx.has_device_cookie(request):
            token = settings.enroll_device(body.code, body.name or "设置页设备")
            if token is None:
                raise HTTPException(status_code=403, detail="绑定当前设备需附主机脚本生成的一次性绑定码")
            ctx.set_device_cookie(response, token)
        try:
            settings.set_device_auth(body.enabled)
        except ValueError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        if ctx.conn_manager is not None:
            await ctx.conn_manager.revalidate()
        logger.warning(f"[auth] 设备验证已{'开启' if body.enabled else '关闭'}")
        return {"ok": True, "device_auth": settings.device_auth}

    @router.put(
        "/api/auth/new_devices",
        dependencies=[auth_dep],
        response_model=NewDevicesResponse,
        response_model_exclude_unset=True,
    )
    async def auth_set_new_devices(body: NewDevicesBody, request: Request):
        if body.enabled:
            if not settings.allow_new_devices:
                raise HTTPException(status_code=403, detail="重新开放新设备请在主机运行 scripts/device_token.py allow-new")
        else:
            if not ctx.has_device_cookie(request):
                raise HTTPException(status_code=403, detail="请先绑定当前设备，再关闭新设备入口")
            settings.deny_new_devices()
            if ctx.conn_manager is not None:
                await ctx.conn_manager.revalidate()
            logger.warning("[auth] 新设备入口已关闭")
        return {"ok": True, "allow_new_devices": settings.allow_new_devices}

    @router.post(
        "/api/auth/login",
        response_model=LoginResponse,
        response_model_exclude_unset=True,
    )
    async def login(body: LoginBody):
        if not settings.has_password:
            return {"token": "", "has_password": False}
        ok = settings.verify(body.password)
        logger.warning(f"[auth] 登录尝试: has_password=True, 校验={'通过' if ok else '失败'}")
        if not ok:
            raise HTTPException(status_code=401, detail="密码错误")
        return {"token": settings.issue_token(), "has_password": True}

    @router.post(
        "/api/auth/set_password",
        dependencies=[auth_dep],
        response_model=SetPasswordResponse,
        response_model_exclude_unset=True,
    )
    async def set_password(body: SetPasswordBody):
        settings.set_password(body.password.strip() or None)
        if ctx.conn_manager is not None:
            await ctx.conn_manager.revalidate()
        logger.warning(f"[auth] 密码已{'设置' if settings.has_password else '清除'}")
        return {"ok": True, "has_password": settings.has_password}

    @router.get(
        "/api/settings",
        dependencies=[auth_dep],
        response_model=WebSettingsDto,
        response_model_exclude_unset=True,
    )
    async def get_settings():
        return settings.get_public()

    @router.put(
        "/api/settings",
        dependencies=[auth_dep],
        response_model=WebSettingsDto,
        response_model_exclude_unset=True,
    )
    async def put_settings(body: SettingsBody):
        settings.update(
            show_reasoning=body.show_reasoning,
            split_messages=body.split_messages,
        )
        # 同步推送给所有前端，让开关立即生效
        if ctx.conn_manager is not None:
            await ctx.conn_manager.broadcast({"type": "settings", "data": settings.get_public()})
        return settings.get_public()

    return router
