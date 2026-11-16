"""
pytest 全局夹具

关键点：
1. sys.path 注入项目根（模仿 run_web.py 的做法），tests/ 下直接 import 顶层模块
2. GoalManager 是全局单例，测试用临时库时必须先重置单例（autouse），
   避免上一个测试的实例（指向已销毁的 tmp_path）泄漏到下一个测试
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def app_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TENTACLE_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def reset_goal_manager_singleton():
    """每个测试前后重置 GoalManager 单例，保证隔离。"""
    import runtime.goals as goal_manager

    original = goal_manager._goal_manager_instance
    goal_manager._goal_manager_instance = None
    yield
    goal_manager._goal_manager_instance = original


@pytest.fixture(autouse=True)
def reset_tool_registry_singleton():
    """每个测试前后重置 ToolRegistry 单例，保证测试间互不污染。"""
    import runtime.tool_registry as tool_registry

    original = tool_registry.ToolRegistry._instance
    original_tools = dict(tool_registry.registry._tools)
    tool_registry.registry._tools.clear()
    tool_registry.ToolRegistry._instance = None
    yield
    tool_registry.registry._tools.clear()
    tool_registry.registry._tools.update(original_tools)
    tool_registry.ToolRegistry._instance = original


@pytest.fixture(autouse=True)
def reset_scheduler_singleton():
    """每个测试前后重置 Scheduler 单例，避免测试实例（指向已销毁的 tmp_path）泄漏。"""
    import runtime.scheduler as scheduler

    original = scheduler._scheduler_instance
    scheduler._scheduler_instance = None
    yield
    scheduler._scheduler_instance = original


@pytest.fixture
def goal_db(tmp_path):
    """指向临时库的 GoalManager 实例（单例已由上一个 fixture 重置）。"""
    from runtime.goals import GoalManager

    db_path = tmp_path / "test_chronicle.db"
    return GoalManager(str(db_path))


@pytest.fixture(autouse=True)
def reset_session_manager_singleton():
    """Session 测试使用临时库，不能复用其他测试的内存态与缓存路径。"""
    from agent.session_manager import SessionManager

    original = SessionManager._instance
    SessionManager._instance = None
    yield
    SessionManager._instance = original
