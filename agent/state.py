"""
Session 数据模型

定义会话的数据结构，不包含任何业务逻辑。
"""

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class Session:
    """
    会话对象

    Attributes:
        session_id: 会话唯一标识，格式为 sess_YYYYMMDD_XXXX
        start_time: 会话开始时间戳
        end_time: 会话结束时间戳，未结束时为 None
        status: 会话状态，active 或 inactive
        history: 当前会话的对话历史，不包含 system_prompt
    """

    session_id: str
    start_time: float
    end_time: Optional[float] = None
    status: Literal["active", "inactive"] = "active"
    history: list[dict] = field(default_factory=list)
