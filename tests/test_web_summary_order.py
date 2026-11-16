"""摘要管理列表的稳定排序与分页：仅使用临时数据库和模拟记录。"""

import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

from web.server import create_app
from web.settings import WebSettings


def summary_client(tmp_path, records):
    db_path = tmp_path / "summaries.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE summaries (id INTEGER PRIMARY KEY, summary TEXT, start_time REAL, end_time REAL)"
        )
        conn.executemany("INSERT INTO summaries VALUES (?, ?, ?, ?)", records)
    app = create_app(
        conn_manager=None,
        web_adapter=None,
        agent_session=None,
        heartbeat=None,
        session_manager=None,
        chronicle=SimpleNamespace(db_path=db_path),
        settings=WebSettings(str(tmp_path / "webui_settings.json")),
        goal_manager=None,
    )
    return TestClient(app)


def test_summary_equal_times_descending_across_pages(tmp_path):
    # 最近一批共享起始时间，复现 290…300、289…1 的显示顺序。
    records = [
        (i, f"模拟摘要 {i}", float(i if i < 290 else 1000), 2000.0)
        for i in range(1, 301)
    ]
    with summary_client(tmp_path, records) as client:
        ids = []
        for offset in range(0, 300, 10):
            response = client.get("/api/admin/summaries", params={"offset": offset, "limit": 10})
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 300
            ids.extend(row[0] for row in data["rows"])
    assert ids == list(range(300, 0, -1))


def test_summary_time_remains_primary_sort_key(tmp_path):
    records = [
        (1, "模拟较新事件", 300.0, 400.0),
        (2, "模拟较旧事件 A", 100.0, 200.0),
        (3, "模拟较旧事件 B", 100.0, 200.0),
    ]
    with summary_client(tmp_path, records) as client:
        response = client.get("/api/admin/summaries")
        assert response.status_code == 200
        assert [row[0] for row in response.json()["rows"]] == [1, 3, 2]
