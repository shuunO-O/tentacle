"""内置鸿蒙客户端联动插件：挂载 /api/plugins/ext/harmony/*、注册闹钟工具与 HTTP 回合实况窗适配。"""

from __future__ import annotations

import json
import re
from typing import Any, Literal
from zoneinfo import ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from .phone_service import PhoneService, current_phone
from .push import HarmonyPush


class PhoneRegistration(BaseModel):
    device_id: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    secret: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9-]+$")
    push_token: str = Field(default="", max_length=4096)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    alarms: bool = False
    live_view: bool = False


class PhoneReceipt(BaseModel):
    status: Literal["executed", "failed", "unknown"]
    result: str = Field(default="", max_length=300)


class PhoneRound(BaseModel):
    activity_id: int = Field(ge=1, le=2147483647)
    event: Literal["PROGRESS"] = "PROGRESS"  # Explicitly a development scenario, not a production entitlement.


def authorized_device(service: PhoneService, request: Request, owner_fn) -> dict[str, Any]:
    device = request.headers.get("X-Tentacle-Device", "")
    secret = request.headers.get("X-Tentacle-Device-Secret", "")
    if not device or not secret:
        raise HTTPException(401, "缺少手机设备凭证")
    try:
        return service.authorize(device, secret, owner_fn(request))
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


def build_harmony_router(
    service: PhoneService,
    *,
    session_owner_fn,
    session_guard_fn,
) -> APIRouter:
    router = APIRouter()

    @router.post("/register")
    async def register(body: PhoneRegistration, request: Request):
        guard = session_guard_fn(request)
        owner_key = session_owner_fn(request)
        try:
            service.register(
                body.device_id,
                body.secret,
                owner_key,
                body.push_token,
                body.timezone,
                body.alarms,
                body.live_view,
                guard,
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        except (ValueError, ZoneInfoNotFoundError):
            raise HTTPException(400, "手机时区无效") from None
        return {"registered": True, "push_configured": bool(service.push and service.push.configured)}

    @router.get("/state")
    async def state(request: Request):
        device = authorized_device(service, request, session_owner_fn)
        try:
            return service.state(device["id"])
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None

    @router.post("/actions/{action_id}/receipt")
    async def receipt(action_id: str, body: PhoneReceipt, request: Request):
        device = authorized_device(service, request, session_owner_fn)
        if not re.fullmatch(r"[a-f0-9]{32}", action_id):
            raise HTTPException(400, "操作ID无效")
        try:
            service.acknowledge(device["id"], action_id, body.status, body.result)
        except LookupError:
            raise HTTPException(404, "操作不存在") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None
        return {"ok": True}

    @router.delete("/register")
    async def unregister(request: Request):
        device = authorized_device(service, request, session_owner_fn)
        service.unregister(device["id"])
        return {"ok": True}

    return router


class _HarmonyTurnSession:
    shield_disconnect = True

    def __init__(
        self,
        *,
        service: PhoneService,
        device_id: str,
        owner_key: str,
        request_id: str | None,
        has_live_view: bool,
        retain_fn,
    ):
        self._service = service
        self._device_id = device_id
        self._owner_key = owner_key
        self._request_id = request_id
        self._has_live_view = has_live_view
        self._retain_fn = retain_fn
        self._token = None

    def enter(self) -> None:
        self._token = current_phone.set((self._service, self._device_id, self._owner_key))

    def finish(self, status: str) -> None:
        if self._has_live_view and self._request_id:
            self._service.finish_round(self._request_id, status)
            self._retain_fn(self._service.push_round(self._request_id))

    def exit(self) -> None:
        if self._token is not None:
            current_phone.reset(self._token)
            self._token = None


class _HarmonyChatAdapter:
    def __init__(self, *, service: PhoneService, allow_live_view: bool, web_proxy):
        self._service = service
        self._allow_live_view = allow_live_view
        self._web = web_proxy

    def prepare(
        self,
        *,
        request: Request,
        platform: str | None,
        request_id: str | None,
        client_meta: dict[str, Any] | None,
    ) -> _HarmonyTurnSession | None:
        phone = None
        if request.headers.get("X-Tentacle-Device"):
            phone = authorized_device(self._service, request, self._web.session_owner)

        raw_live = client_meta.get("live_view") if isinstance(client_meta, dict) else None
        live_view: PhoneRound | None = None
        if raw_live is not None:
            if not self._allow_live_view:
                raise HTTPException(403, "鸿蒙插件配置已禁用实况窗同步")
            try:
                live_view = PhoneRound.model_validate(raw_live)
            except ValidationError as exc:
                raise HTTPException(422, exc.errors()) from None

            if phone is None or not phone["live"] or not request_id or platform != "harmony":
                raise HTTPException(400, "实况窗需要已注册的鸿蒙设备与请求ID")
            try:
                self._service.begin_round(phone["id"], request_id, live_view.activity_id, live_view.event)
            except (ValueError, PermissionError) as exc:
                raise HTTPException(409, str(exc)) from None

        if phone is None:
            return None

        return _HarmonyTurnSession(
            service=self._service,
            device_id=phone["id"],
            owner_key=phone["owner"],
            request_id=request_id,
            has_live_view=live_view is not None,
            retain_fn=self._web.retain_task,
        )


def setup(ctx) -> None:
    cfg = ctx.config
    wait_seconds = float(cfg.get("action_wait_seconds", 12) or 12)
    allow_alarms = bool(cfg.get("allow_alarms", True))
    allow_live_view = bool(cfg.get("allow_live_view", True))

    push = HarmonyPush(env_getter=ctx.get_env)
    service = PhoneService(
        ctx.data_path("phone.db"),
        broadcast=ctx.web.broadcast,
        push=push,
    )

    async def phone_alarm_tool(args: dict[str, Any]) -> str:
        if not allow_alarms:
            return json.dumps({"status": "error", "error": "管理员已在插件配置中禁用手机闹钟功能"}, ensure_ascii=False)
        target = current_phone.get()
        if target is None:
            return json.dumps(
                {"status": "error", "error": "当前回合没有已授权的鸿蒙手机，请从手机客户端发起"},
                ensure_ascii=False,
            )
        try:
            bound_service, device = target[:2]
            result = await bound_service.alarm(
                device,
                args,
                wait_seconds=wait_seconds,
                expected_owner=target[2] if len(target) > 2 else None,
            )
            if result["status"] == "pending":
                result["message"] = "仅已排队，尚未设置成功。需打开手机客户端确认；不要声称已设置闹钟。"
            return json.dumps(result, ensure_ascii=False)
        except (ValueError, PermissionError, LookupError) as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)

    ctx.register_tool(
        name="phone_alarm",
        description="在当前鸿蒙手机设置一次性闹钟或查询执行回执（仅在收到明确要求时使用）。at 须含时区偏移；"
                    "仅 status=executed 可告知设置成功，pending 为等待手机执行，unknown 勿自动重试。",
        parameters={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["create", "status"]},
                "title": {"type": "string", "description": "create 必填，提醒事项，最多100字"},
                "at": {"type": "string", "description": "create 必填，ISO时间含偏移，例如2026-11-04T09:00:00+08:00"},
                "action_id": {"type": "string", "description": "status 必填，之前回执的 action_id"},
            },
            "required": ["operation"],
        },
        func=phone_alarm_tool,
    )

    router = build_harmony_router(
        service,
        session_owner_fn=ctx.web.session_owner,
        session_guard_fn=ctx.web.make_session_guard,
    )
    ctx.register_router(router)
    ctx.register_http_chat_adapter(
        _HarmonyChatAdapter(service=service, allow_live_view=allow_live_view, web_proxy=ctx.web)
    )
    ctx.harmony_service = service
