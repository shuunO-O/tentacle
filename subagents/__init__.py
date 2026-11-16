"""独立子任务运行时；不创建第二个 AgentSession。"""

__all__ = ["SubagentManager"]


def __getattr__(name):
    if name == "SubagentManager":
        from subagents.manager import SubagentManager
        return SubagentManager
    raise AttributeError(name)
