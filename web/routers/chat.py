"""聊天发送、中断、历史分页与断线重连同步路由。"""

from __future__ import annotations

import asyncio
import inspect
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from agent.commands import command_catalog
from agent.segments import split_segments
from web.deps import AppContext
from web.schemas import (
    ChatCommandsResponse,
    ChatHistoryResponse,
    ChatSend,
    ChatSendResponse,
    ChatStop,
    ChatStopResponse,
    ChatSyncResponse,
    CommandBody,
)

logger = logging.getLogger(__name__)


def _round_response(result, *, legacy_command: bool = False):
    terminal = getattr(result, "round_status", None)
    ok = terminal is None or terminal["status"] == "completed"
    response = {"ok": ok, "total_tokens": result.total_usage.get("total_tokens", 0), "round": terminal}
    if result.stop_reason == "command":
        response["command"] = {"text": result.final_answer, "refresh_history": True}
        if legacy_command:
            response["text"] = result.final_answer
    if not ok:
        response["detail"] = terminal["error"] or "本轮未完成"
        return JSONResponse(response, status_code=200 if terminal["status"] == "cancelled" else 500)
    return response


def _round_error(exc: Exception):
    return JSONResponse(
        {"ok": False, "detail": str(exc), "round": getattr(exc, "round_status", None)},
        status_code=400 if isinstance(exc, ValueError) else 500,
    )


def chat_router(ctx: AppContext, auth_dep) -> APIRouter:
    router = APIRouter(prefix="/api/chat", dependencies=[auth_dep])
    agent_session = ctx.agent_session
    chronicle = ctx.chronicle
    settings = ctx.settings

    def history_payload(limit: int) -> dict:
        limit = max(1, min(limit, 200))
        msgs = chronicle.get_recent_rounds("", limit + 1)
        round_keys = list(dict.fromkeys(m["round_id"] or m["id"] for m in msgs))
        has_more = len(round_keys) > limit
        if has_more:
            msgs = [m for m in msgs if (m["round_id"] or m["id"]) != round_keys[0]]
        out = []
        for m in msgs:
            item = dict(m)
            if m["role"] == "assistant":
                item["segments"] = split_segments(m["content"]) if settings.split_messages else [m["content"]]
            out.append(item)
        return {"messages": out, "has_more": has_more}

    @router.post(
        "/send",
        response_model=ChatSendResponse,
        response_model_exclude_unset=True,
    )
    async def chat_send(body: ChatSend, request: Request):
        if getattr(agent_session, "_restarting", False):
            raise HTTPException(503, "应用正在准备重启，请恢复后重新发送")
        text = body.text.strip()
        image_records = []
        local_ids = list(dict.fromkeys((body.images or []) + (body.files or [])))
        if len(local_ids) > 10:
            raise HTTPException(400, "单轮最多 10 个附件")
        if local_ids:
            # local_id -> FileManager 记录；file_id 可缺失（方言层 base64 通道兜底）
            fm = ctx.get_file_manager()
            for local_id in local_ids:
                record = await fm.get_file(local_id)
                if record:
                    image_records.append(fm.public_record(record))
                else:
                    raise HTTPException(400, "附件已删除，请重新上传")
        if not text and not image_records:
            raise HTTPException(status_code=400, detail="消息不能为空")

        adapters = ctx.plugin_manager.get_http_chat_adapters() if ctx.plugin_manager is not None else ()
        active_turns = []
        for adapter in adapters:
            turn = adapter.prepare(
                request=request,
                platform=body.platform,
                request_id=body.request_id,
                client_meta=body.client_meta,
            )
            if inspect.isawaitable(turn):
                turn = await turn
            if turn is not None:
                active_turns.append(turn)

        should_shield = any(bool(getattr(t, "shield_disconnect", False)) for t in active_turns)

        async def execute():
            entered = []
            status = "unknown"
            try:
                for t in active_turns:
                    enter_fn = getattr(t, "enter", None)
                    if callable(enter_fn):
                        enter_fn()
                    entered.append(t)
                result = await agent_session.run_round(
                    text,
                    body.platform,
                    images=image_records or None,
                    request_id=body.request_id,
                )
                terminal = getattr(result, "round_status", None)
                status = terminal["status"] if terminal else "unknown"
                return _round_response(result)
            except Exception as exc:
                status = (getattr(exc, "round_status", None) or {}).get("status", "failed")
                logger.exception("聊天回合失败")
                return _round_error(exc)
            finally:
                for t in reversed(entered):
                    finish_fn = getattr(t, "finish", None)
                    if callable(finish_fn):
                        try:
                            res = finish_fn(status)
                            if inspect.isawaitable(res):
                                await res
                        except Exception:
                            logger.warning("客户端状态保存失败，聊天结果保留", exc_info=True)
                    exit_fn = getattr(t, "exit", None)
                    if callable(exit_fn):
                        try:
                            exit_fn()
                        except Exception:
                            logger.warning("客户端上下文恢复失败", exc_info=True)

        # 移动端退到后台等场景允许由插件标记保活，避免连接断开取消服务端 Agent 任务。
        return await asyncio.shield(ctx.retain_client_task(execute())) if should_shield else await execute()

    @router.post(
        "/stop",
        response_model=ChatStopResponse,
        response_model_exclude_unset=True,
    )
    async def chat_stop(body: ChatStop):
        try:
            return agent_session.request_stop(round_id=body.round_id, request_id=body.request_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get(
        "/commands",
        response_model=ChatCommandsResponse,
        response_model_exclude_unset=True,
    )
    async def chat_commands():
        return {"commands": command_catalog()}

    @router.post(
        "/command",
        response_model=ChatSendResponse,
        response_model_exclude_unset=True,
    )
    async def chat_command(body: CommandBody):
        """旧客户端兼容入口，识别和执行仍统一交给 run_round。"""
        if getattr(agent_session, "_restarting", False):
            raise HTTPException(503, "应用正在准备重启，请恢复后再试")
        if not body.text.strip().startswith("/") or len(body.text.strip()) <= 1:
            raise HTTPException(status_code=400, detail="不是有效的斜杠命令")
        try:
            result = await agent_session.run_round(body.text, request_id=body.request_id)
        except Exception as exc:
            logger.exception("命令回合失败")
            return _round_error(exc)
        if result.stop_reason != "command":
            raise HTTPException(status_code=400, detail="不是有效的斜杠命令")
        return _round_response(result, legacy_command=True)

    @router.get(
        "/history",
        response_model=ChatHistoryResponse,
        response_model_exclude_unset=True,
    )
    async def chat_history(limit: int = 50):
        return history_payload(limit)

    @router.get(
        "/sync",
        response_model=ChatSyncResponse,
        response_model_exclude_unset=True,
    )
    async def chat_sync(limit: int = 50):
        # 同一事件循环中同步取样，归档与在途事件之间没有 await 窗口。
        payload = history_payload(limit)
        return {**payload, **ctx.web_adapter.chat_state.snapshot(agent_session.rounds_snapshot())}

    return router
