"""
心跳模块

按固定间隔为 Agent 提供行动机会；进行中任务提醒可选，目标每 24 小时回顾。
无目标时仍正常唤醒。
窗口内为旧 session 安排近况交接，成功后由会话宿主切换并重建提示词。

用户输入时会调用 reset_timer()，避免心跳和用户输入同时触发。
"""

import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
from pathlib import Path

from runtime.timezone import local_datetime
from runtime.goals import get_goal_manager
from agent.protocol import LoopResult
from plugins.hooks import hook_bus as global_hook_bus

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 30
GOAL_REVIEW_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_HANDOVER_MESSAGE = (
    "本轮心跳是切换 session 前的近况交接。请先用 situation_notes 的 read 读取旧近况，"
    "结合当前完整上下文重新整理，再用 write 保存完整新版本；即使无需修改也请完整写入确认。"
    "保留仍有用的状态和必要的不确定性，合并或删除过时内容。"
    "本轮正常完成、近况保存成功且没有用户输入打断后，程序将切换 session；"
    "交接未完成则保留当前 session，在下一次符合窗口条件的心跳重新交接。"
)


def heartbeat_settings(raw=None):
    """心跳正文与日切策略的共用默认值和校验，不访问运行时数据。"""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError("heartbeat 必须是配置对象")
    switch = raw.get("session_switch", {})
    if not isinstance(switch, dict):
        raise ValueError("heartbeat.session_switch 必须是配置对象")
    settings = {
        "enabled": raw.get("enabled", True),
        "message": raw.get("message", "心跳"),
        "include_in_progress_tasks": raw.get("include_in_progress_tasks", False),
        "session_switch": {
            "enabled": switch.get("enabled", True),
            "window_start": switch.get("window_start", "01:00"),
            "window_end": switch.get("window_end", "07:00"),
            "message": switch.get("message", DEFAULT_HANDOVER_MESSAGE),
        },
    }
    for value in (settings["message"], settings["session_switch"]["message"]):
        if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("心跳及交接消息必须是非空单行文本（最多 4096 字符）")
    if any(type(value) is not bool for value in (settings["enabled"], settings["include_in_progress_tasks"],
                                                 settings["session_switch"]["enabled"])):
        raise ValueError("心跳、进行中任务提醒及 session 切换开关必须为布尔值")
    for key in ("window_start", "window_end"):
        value = settings["session_switch"][key]
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("session 切换窗口必须使用 HH:MM（00:00–23:59）")
    if settings["session_switch"]["window_start"] == settings["session_switch"]["window_end"]:
        raise ValueError("session 切换窗口的开始与结束时间不能相同")
    return settings


class Heartbeat:
    """
    心跳触发器

    与 AgentSession 共享同一个 narrative flow，保证所有平台输入和系统心跳
    都在同一连续上下文中进行。
    """

    def __init__(
        self,
        agent_session,
        interval_minutes: float = DEFAULT_INTERVAL_MINUTES,
        state_file: str | Path = "data/heartbeat_state.json",
        settings: dict | None = None,
    ):
        """
        初始化心跳模块

        Args:
            agent_session: 共享的 AgentSession 实例
            interval_minutes: 心跳间隔（分钟）
            state_file: 目标回顾时间的持久化文件
        """
        self.agent_session = agent_session
        self.interval_seconds = interval_minutes * 60
        self.configure(settings)
        self._state_file = Path(state_file)
        self._last_goal_review_at = self._load_review_time()
        self._running = False
        self._task: asyncio.Task | None = None
        self._next_beat_time = time.time() + self.interval_seconds
        self._timer_generation = 0
        self._beat_lock = asyncio.Lock()
        self._scheduled_lock = asyncio.Lock()

    def configure(self, settings):
        """在会话锁内发布下一轮使用的正文与切换策略，不刷新当前提示词。"""
        previous = getattr(self, 'settings', None)
        self.settings = heartbeat_settings(settings)
        if previous is not None and previous['enabled'] != self.settings['enabled']:
            self.reset_timer()

    def _session_switch_due(self, session, now):
        switch = self.settings["session_switch"]
        local = local_datetime(now)
        created = local_datetime(session.start_time)
        if not switch["enabled"] or created.date() >= local.date():
            return False
        clock = local.strftime("%H:%M")
        start, end = switch["window_start"], switch["window_end"]
        return start <= clock < end if start < end else clock >= start or clock < end

    def _load_review_time(self) -> float:
        try:
            state = json.loads(self._state_file.read_text(encoding="utf-8"))
            timestamp = float(state["last_goal_review_at"])
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError("无效的回顾时间")
            return timestamp
        except FileNotFoundError:
            return 0.0
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.warning("心跳回顾时间读取失败，将在下次心跳回顾目标: %s", type(exc).__name__)
            return 0.0

    def _save_review_time(self, timestamp: float):
        # 即使落盘失败，本进程也不每轮重复完整回顾。
        self._last_goal_review_at = timestamp
        temporary = None
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".heartbeat-", dir=self._state_file.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"last_goal_review_at": timestamp}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_file)
        except OSError as exc:
            logger.warning("心跳回顾时间保存失败: %s", type(exc).__name__)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _format_time(t: float) -> str:
        """格式化时间戳为 [MM.DD-HH:MM]"""
        return local_datetime(t).strftime("[%m.%d-%H:%M]")

    def _build_heartbeat_message(
        self, goal_reminder: str, trigger_time: float, content: str | None = None
    ) -> list[dict]:
        """
        组装心跳注入的 user 消息

        将触发时间、目标提醒合并到同一条 [system] 前缀的 user 消息中。
        目标提醒可为空，空段落不注入。
        """
        trigger_time_str = self._format_time(trigger_time)
        sections = [f"{trigger_time_str}[system] {self.settings['message']}"]
        if content:
            sections.append(f"预约事项：{content}")
        if goal_reminder:
            sections.append(goal_reminder)
        content = "\n\n".join(sections)
        return [{"role": "user", "content": content}]

    async def _build_goal_reminder(self, full_review: bool = False) -> str | None:
        """平时只带进行中任务；完整回顾展示所有目标。查询失败返回 None。"""
        try:
            goals = await get_goal_manager().list_active_goals()
        except Exception as e:
            logger.warning(f"心跳构建目标提醒失败: {e}")
            return None

        if not goals:
            return ""

        priorities = {"high": "高", "medium": "普通", "low": "低"}
        lines = []
        for goal in goals:
            tasks = goal["tasks"] if full_review else [
                task for task in goal["tasks"] if task["status"] == "in_progress"
            ]
            if not full_review and not tasks:
                continue
            priority = priorities.get(goal["priority"], goal["priority"])
            lines.append(f"- [{priority}] {goal['description']}")
            for task in tasks:
                status = "进行中" if task["status"] == "in_progress" else "待办"
                lines.append(f"  {status}：{task['content']}")
        if not lines:
            return ""
        title = "目标回顾：" if full_review else "进行中的目标："
        return "\n".join([title, *lines])

    def reset_timer(self):
        """用户输入时调用，重置心跳计时"""
        self._next_beat_time = time.time() + self.interval_seconds
        self._timer_generation += 1
        logger.debug("心跳计时已重置")

    def set_interval(self, interval_minutes: float):
        """宿主在回合边界更新间隔；重新计时，不补跑或立即触发。"""
        if not math.isfinite(interval_minutes) or interval_minutes <= 0:
            raise ValueError("心跳间隔必须为有限正数")
        self.interval_seconds = interval_minutes * 60
        self.reset_timer()

    async def _beat_once(self, trigger_time: float, expected_generation=None, *, content=None,
                         request_id=None, before_run=None):
        # 固定计时与预约唤醒共用完整编排，串行更新回顾游标。
        async with self._beat_lock:
            if expected_generation is not None and (not self.settings['enabled']
                                                    or self._timer_generation != expected_generation):
                return LoopResult([], '', {}, stop_reason='skipped')
            return await self._execute_beat(trigger_time, expected_generation, content=content,
                                            request_id=request_id, before_run=before_run)

    async def _execute_beat(self, trigger_time, expected_generation, *, content, request_id, before_run):
        """执行一次心跳，无目标时也提供行动机会。"""
        full_review = (
            self._last_goal_review_at == 0
            or trigger_time - self._last_goal_review_at >= GOAL_REVIEW_INTERVAL_SECONDS
        )
        goal_reminder = (
            await self._build_goal_reminder(full_review=full_review)
            if full_review or self.settings["include_in_progress_tasks"] else ""
        )

        hb_hook = await (getattr(self, "hook_bus", None) or global_hook_bus).emit_before_heartbeat({
            "trigger_time": trigger_time,
            "full_review": full_review,
            "goal_reminder": goal_reminder,
            "content": content,
        })
        if hb_hook.get("skip"):
            return LoopResult([], "", {}, stop_reason="skipped")
        briefings = hb_hook.get("briefings") or []
        extra_briefing = "\n\n".join(f"[插件心跳简报]\n{b}" for b in briefings) if briefings else ""

        def _attach_briefing(messages: list[dict]) -> list[dict]:
            if not extra_briefing or not messages:
                return messages
            msgs = [dict(m) for m in messages]
            msgs[-1]["content"] = f"{msgs[-1]['content']}\n\n{extra_briefing}"
            return msgs

        injection = _attach_briefing(self._build_heartbeat_message(goal_reminder or "", trigger_time, content))
        handover_session = None
        handover_date = None

        async def prepare_heartbeat(session):
            # 在 AgentSession 的会话锁内，用实际准入时刻与实际 session 判断。
            nonlocal full_review, goal_reminder, handover_session, handover_date
            now = time.time()
            if self._session_switch_due(session, now):
                if not self.agent_session.session_manager.daily_compression_ready(now):
                    logger.info("[session 交接] 每日摘要尚未完成，保留当前 session，等待下一次心跳")
                    return injection
                handover_session = session
                handover_date = local_datetime(now).date()
                if not full_review:
                    goal_reminder = await self._build_goal_reminder(full_review=True)
                full_review = True
                messages = self._build_heartbeat_message(goal_reminder or "", trigger_time, content)
                messages[0]["content"] += "\n\n" + self.settings["session_switch"]["message"]
                return _attach_briefing(messages)
            return injection

        async def finish_heartbeat(session, result, context):
            if handover_session is not session or result.stop_reason != "complete" or result.error:
                return
            if not context.situation_written:
                logger.info("[session 交接] 本轮未成功写入近况，等待下一次窗口内心跳")
                return

            def eligible():
                now = time.time()
                return (
                    not self.agent_session._pending_user_rounds
                    and not (context.interrupt_event is not None and context.interrupt_event.is_set())
                    and (expected_generation is None or self._timer_generation == expected_generation)
                    and self.agent_session.session_manager.get_or_create_active_session() is session
                    and local_datetime(now).date() == handover_date
                    and self.agent_session.session_manager.daily_compression_ready(now)
                    and self._session_switch_due(session, now)
                )

            if eligible():
                await self.agent_session._switch_session_locked(should_switch=eligible)

        logger.info(f"[心跳触发] {self._format_time(trigger_time)}")

        result = await self.agent_session.run_round(
            source="heartbeat", injection_messages=injection, trigger_time=trigger_time,
            request_id=request_id, before_run=before_run,
            should_run=(None if expected_generation is None
                        else lambda: self.settings['enabled'] and self._timer_generation == expected_generation),
            prepare_heartbeat=prepare_heartbeat, finish_heartbeat=finish_heartbeat,
        )
        if isinstance(result, LoopResult) and result.stop_reason != "complete":
            # A yielded or failed Flex round has not completed the full review.
            return result
        if full_review and goal_reminder is not None:
            self._save_review_time(trigger_time)
        return result

    async def run_scheduled(self, content: str, *, request_id=None, before_run=None):
        """预约唤醒独立于固定计时；用户到达时延后/让路，保留未完成的预约。"""
        # 仅一个预约轮询准入，避免大量到期任务在用户交互时反复并发读库。
        async with self._scheduled_lock:
            while True:
                if before_run is not None and not await before_run():
                    return LoopResult([], '', {}, stop_reason='skipped')
                if not self.agent_session.background_ready:
                    await asyncio.sleep(0.25)
                    continue
                result = await self._beat_once(time.time(), content=content, request_id=request_id,
                                               before_run=before_run)
                if result.stop_reason not in {'skipped', 'yielded'}:
                    return result
                await asyncio.sleep(0.25)

    async def _loop(self):
        """后台心跳循环"""
        while self._running:
            now = time.time()
            if self.settings['enabled'] and now >= self._next_beat_time:
                trigger_time = time.time()
                generation = self._timer_generation
                try:
                    await self._beat_once(trigger_time, expected_generation=generation)
                except Exception:
                    # 本轮保存/交接失败不能终止后台心跳；下一次计时重新尝试。
                    logger.exception("心跳执行失败，保留状态并等待下一次心跳")
                # 用户输入/子任务结果可能在等待叙事流锁时重置计时，不能覆盖它。
                if self._timer_generation == generation:
                    self._next_beat_time = time.time() + self.interval_seconds

            # 避免忙等，最多睡 60 秒或间隔的 1/4
            await asyncio.sleep(min(60.0, self.interval_seconds / 4))

    async def run(self):
        """启动心跳模块"""
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"心跳模块已启动，间隔 {self.interval_seconds / 60} 分钟")

    async def stop(self):
        """停止心跳模块"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("心跳模块已停止")
