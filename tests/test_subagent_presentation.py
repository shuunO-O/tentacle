"""Only GET endpoints; authentication is identical for list and detail."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from web.subagents import subagent_router


def test_subagent_routes_are_authenticated_and_read_only():
    async def auth(request: Request):
        if request.headers.get("authorization") != "Bearer synthetic":
            raise HTTPException(401)
    manager = SimpleNamespace(presentation=AsyncMock(return_value={"enabled": True, "tasks": []}))
    app = FastAPI()
    app.include_router(subagent_router(manager, Depends(auth)))
    with TestClient(app) as client:
        assert client.get("/api/subagents").status_code == 401
        assert client.get("/api/subagents/" + "a" * 32).status_code == 401
        manager.presentation.assert_not_called()
        client.headers["Authorization"] = "Bearer synthetic"
        assert client.get("/api/subagents").status_code == 200
        assert client.post("/api/subagents", json={}).status_code == 405
        assert client.delete("/api/subagents/" + "a" * 32).status_code == 405
        assert client.get("/api/subagents?limit=99999").status_code == 422
        manager.presentation.side_effect = LookupError()
        assert client.get("/api/subagents/" + "a" * 32).status_code == 404
        manager.presentation.side_effect = RuntimeError("not public")
        response = client.get("/api/subagents")
        assert response.status_code == 503 and "not public" not in response.text


def test_disabled_subagent_presentation_does_not_create_runtime():
    app = FastAPI()
    async def auth():
        pass
    app.include_router(subagent_router(None, Depends(auth)))
    with TestClient(app) as client:
        assert client.get("/api/subagents").json() == {"enabled": False, "tasks": []}
        assert client.get("/api/subagents/" + "a" * 32).status_code == 404


async def test_presentation_pages_stored_turns_without_writes_or_execution(tmp_path):
    from subagents.manager import SubagentManager

    config = SimpleNamespace(enabled=True, db_path=tmp_path / "synthetic.db", max_concurrency=1)
    manager = SubagentManager(config)
    # Build a synthetic task DB without the Linux-only secure file creation path.
    import sqlite3
    with sqlite3.connect(config.db_path) as conn:
        conn.executescript("""
            CREATE TABLE tasks(id TEXT PRIMARY KEY, objective TEXT, background TEXT, platform TEXT,
              status TEXT, archived INTEGER DEFAULT 0, progress TEXT DEFAULT '', cleanup_error TEXT, dsh_session_id TEXT,
              created_at REAL, updated_at REAL);
            CREATE TABLE turns(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, message TEXT,
              status TEXT, delivery TEXT, result TEXT, error TEXT, usage TEXT, created_at REAL, finished_at REAL);
        """)
    manager._running = True
    task_id = "a" * 32
    def seed(conn):
        conn.execute("INSERT INTO tasks(id,objective,background,platform,status,progress,created_at,updated_at) "
                     "VALUES(?, 'objective', 'background', 'web', 'completed', 'old progress', 1, 2)", (task_id,))
        for message in ("first", "followup"):
            conn.execute("INSERT INTO turns(task_id,message,status,delivery,result,usage,created_at,finished_at) "
                         "VALUES(?,?,'completed','delivered','result','{}',1,2)", (task_id, message))
    manager.store.transaction(seed)
    before = manager.store.transaction(lambda conn: list(conn.iterdump()))
    listing = await manager.presentation()
    assert listing["tasks"][0]["id"] == task_id
    assert listing["tasks"][0]["delivery"] == "delivered"
    assert "progress" not in listing["tasks"][0]
    latest = await manager.presentation(task_id, limit=1)
    assert latest["has_more"] and latest["turns"][0]["message"] == "followup"
    assert latest["task"]["background"] == "background" and "history" not in latest["task"]
    assert "progress" not in latest["task"]
    older = await manager.presentation(task_id, before=latest["next_before"], limit=1)
    assert not older["has_more"] and older["turns"][0]["message"] == "first"
    assert manager.store.transaction(lambda conn: list(conn.iterdump())) == before
    assert manager._workers == {}
    with pytest.raises(LookupError):
        await manager.presentation("b" * 32)
    with pytest.raises(ValueError):
        await manager.presentation("../invalid")
