"""Synthetic read-only child tasks; no model, sandbox or private history."""

from copy import deepcopy
import time

from fastapi import Depends, HTTPException, Request
from web.subagents import subagent_router


def attach_subagents(app, authenticated):
    now = time.time()
    tasks = [
        {"id": "a" * 32, "objective": "核对配置管理的备份恢复流程", "status": "running",
         "archived": False, "created_at": now - 90, "updated_at": now - 5, "delivery": "background", "turn_id": 2,
         "dsh_session_id": "dsh-preview-01",
         "result": "", "error": None, "finished_at": None, "cleanup_error": None},
        {"id": "b" * 32, "objective": "检查不同平台的聊天气泡间距", "status": "completed",
         "archived": False, "created_at": now - 300, "updated_at": now - 120, "delivery": "delivered", "turn_id": 3,
         "result": "已确认平台标签与正文之间有独立间距，用户和助手气泡均适用。", "error": None, "finished_at": now - 120, "cleanup_error": None},
        {"id": "c" * 32, "objective": "检查可选外部资料", "status": "waiting_input",
         "archived": False, "created_at": now - 200, "updated_at": now - 80, "delivery": "pending", "turn_id": 4,
         "result": "需要主 agent 补充待核对的公开地址。", "error": None, "finished_at": now - 80, "cleanup_error": None},
    ]
    dsh_sessions = [
        {"session_id": "dsh-preview-01", "title": "WebUI 布局联调工作区", "workspace": "d:/tentacle", "running": True},
        {"session_id": "dsh-preview-02", "title": "配置中心分层结构检查", "workspace": "d:/tentacle", "running": False},
    ]

    class SyntheticManager:
        coding_available = True

        async def presentation(self, task_id=None, *, before=None, limit=20):
            if app.state.scenario == "error":
                raise RuntimeError("Synthetic unavailable")
            values = [] if app.state.scenario == "empty" else deepcopy(tasks)
            if not task_id:
                return {
                    "enabled": True,
                    "tasks": values[:limit],
                    "has_more": False,
                    "execution_blocked": False,
                    "coding_enabled": True,
                }
            task = next((task for task in values if task["id"] == task_id), None)
            if task is None:
                raise LookupError("子任务不存在")
            task["background"] = "主 agent 提供的合成背景，不包含真实会话或凭据。"
            turns = [
                {"id": i, "message": "核对目标" if i == 1 else "继续核对边界情况", "status": "completed" if i == 1 else task["status"],
                 "delivery": "delivered" if i == 1 else task["delivery"], "result": "第一轮检查已完成。" if i == 1 else task["result"],
                 "error": None, "created_at": now - 90, "finished_at": now - 50 if i == 1 else task["finished_at"],
                 "usage": {"prompt_tokens": 1200, "completion_tokens": 240}} for i in (1, 2)
                if before is None or i < before
            ]
            return {"enabled": True, "task": task, "turns": turns[-limit:], "has_more": False, "next_before": None}

        async def coding_sessions(self):
            return deepcopy(dsh_sessions)

        async def attach(self, session_id: str):
            matched = next((s for s in dsh_sessions if s["session_id"] == session_id), None)
            if matched is None:
                raise ValueError("DSH 会话不存在")
            task_id = "d" * 32
            if not any(t["id"] == task_id for t in tasks):
                tasks.insert(0, {
                    "id": task_id,
                    "objective": f"接管 DSH 会话：{matched['title']}",
                    "status": "attached",
                    "archived": False,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "delivery": "pending",
                    "turn_id": None,
                    "dsh_session_id": session_id,
                    "result": "",
                    "error": None,
                    "finished_at": None,
                    "cleanup_error": None,
                })
            return {
                "task_id": task_id,
                "status": "attached",
                "dsh_session_id": session_id,
                "workspace": matched["workspace"],
            }

    async def auth(request: Request):
        if not authenticated(request):
            raise HTTPException(401, "预览身份验证")

    @app.post("/api/subagents/dsh/access", dependencies=[Depends(auth)])
    async def synthetic_dsh_access():
        raise HTTPException(
            503,
            "当前处于前端合成预览模式；真实部署启动后，点击此处将直接进入 /coding/（DSH 编程工作区）。",
        )

    app.include_router(subagent_router(SyntheticManager(), Depends(auth)))
