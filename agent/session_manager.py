"""
Session 管理模块

负责 session 的生命周期、持久化、缓存和切换。
"""

import asyncio
import json
import logging
import os
import random
import tempfile
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional

from agent.chronicle import ChronicleManager
from runtime.timezone import day_boundaries, local_datetime, timezone_name
from agent.compressor import Compressor
from config.manager import ConfigManager
from agent.state import Session
from agent.handoff import compatible_history, dialogue_history, interactions_to_generate, model_identity
from agent.prompt import resolve_names
from plugins.hooks import hook_bus as global_hook_bus

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 1_048_576  # 1M
WARNING_THRESHOLD_PERCENT = 40  # 上下文占用提醒阈值


class SessionManager:
    """
    Session 管理器（单例）

    职责：
    - 维护当前 active session 的获取与创建
    - 管理 session 的持久化（sessions 表）
    - 维护 active session 的内存 + 缓存
    - 在 token 阈值触发时提醒用户，由用户手动切换 session
    - 按天压缩历史对话并写入 summaries 表
    - 把摘要注入到新 session 的第一条消息

    设计原则：
    - session 是叙事流的物理切片，由 /session 或心跳完成近况交接后切换。
    - 压缩与 session 切换解耦：压缩按日历天进行，自动切换资格由 Heartbeat 判断。
    - 提醒判据是「当前上下文占用」（本轮最后一次 API 请求的 prompt_tokens）达到阈值，
      而非累积 token 达到阈值。
    - 提醒阈值固定为上下文窗口的 40%，每个 session 只提醒一次，状态随缓存持久化。
    """

    _instance: Optional["SessionManager"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        manager: ConfigManager,
        db_path: str = "data/chronicle.db",
        cache_path: str = "data/active_session.json",
        compression_state_path: str = "data/compression_state.json",
    ):
        # 单例防止重复初始化
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self.manager = manager
        self.chronicle = ChronicleManager(db_path)
        self.cache_path = Path(cache_path)
        self.compression_state_path = Path(compression_state_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        self._context_window = self._load_context_window("chat")
        self._current_session: Optional[Session] = None
        self._context_tokens: int = 0
        self._context_warning_sent: bool = False
        self._history_model = None
        self.compressor = Compressor(manager, self.chronicle)

        self._last_compressed_date: Optional[date] = None
        self._load_compression_state()
        self._load_cache()

        # session 切换回调（如 FileManager 清理远端图片），签名为
        # async def callback(session: Session)，None 表示未注册
        self._on_session_inactive: Optional[Callable[[Session], Awaitable[None]]] = None

    def set_on_session_inactive(self, callback: Callable[[Session], Awaitable[None]]):
        """注册 session 切换回调（在旧 session 标记 inactive 前触发）"""
        self._on_session_inactive = callback

    def _load_context_window(self, role: str) -> int:
        """从配置中读取指定模型角色的上下文窗口大小。"""
        try:
            if getattr(self.manager, "_raw_config", None) is None:
                self.manager._load_config()
            raw = getattr(self.manager, "_raw_config", {}) or {}
            from config.providers import resolved_models
            models = resolved_models(raw)
            model = models.get(role, {})
            return int(model.get("context_window", DEFAULT_CONTEXT_WINDOW))
        except Exception as e:
            logger.warning(f"读取 context_window 失败，使用默认值: {e}")
            return DEFAULT_CONTEXT_WINDOW

    def set_model_role(self, role: str) -> None:
        """模型热切换后同步 session 上下文提醒阈值。"""
        self._context_window = self._load_context_window(role)

    def apply_model_config(self, cfg):
        """Called under the narrative lock (or before startup) when config takes effect.

        Save the prior state before changing it. A failed atomic cache write keeps
        both memory and the previous cache intact; startup can retry safely.
        """
        target = model_identity(cfg)
        if self._history_model == target:
            return
        session = self._current_session
        previous = self._history_model
        history = session.history if session else None
        tokens = self._context_tokens
        rebuild = not compatible_history(previous, target)
        if session is not None:
            snapshot = self._cache_data()
            directory = self.cache_path.parent / 'session_handoffs'
            directory.mkdir(parents=True, exist_ok=True)
            self._write_cache(snapshot, directory / (uuid.uuid4().hex + '.json'))
        try:
            if session is not None:
                if rebuild:
                    converted = interactions_to_generate(history, previous, target)
                    session.history = converted if converted is not None else dialogue_history(history)
                else:
                    session.history = [{k: v for k, v in msg.items() if k != 'gemini_interaction'} for msg in history]
            self._history_model = target
            self._context_tokens = 0
            self._save_cache()
        except Exception:
            self._history_model, self._context_tokens = previous, tokens
            if session is not None:
                session.history = history
            raise

    def _load_compression_state(self):
        """从文件读取最后压缩日期"""
        self._compression_state_saved = False
        if not self.compression_state_path.exists():
            self._last_compressed_date = None
            return
        try:
            with open(self.compression_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            date_str = data.get("last_compressed_date")
            self._last_compressed_date = date.fromisoformat(date_str) if date_str else None
            self._compression_state_saved = True
        except Exception as e:
            logger.error(f"读取压缩状态失败: {e}", exc_info=True)
            self._last_compressed_date = None

    def _save_compression_state(self):
        """原子保存进度；写失败阻止自动切换，下一轮先重试保存已有进度。"""
        self._compression_state_saved = False
        self.compression_state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"last_compressed_date": self._last_compressed_date.isoformat(), "timezone": timezone_name()}
        self._write_cache(data, self.compression_state_path)
        self._compression_state_saved = True

    def daily_compression_ready(self, now: float | None = None) -> bool:
        """昨天及之前的摘要进度已完成并持久化，才能执行自动日切。"""
        yesterday = local_datetime(now).date() - timedelta(days=1)
        return (self._last_compressed_date is not None
                and self._last_compressed_date >= yesterday
                and self._compression_state_saved)

    @staticmethod
    def _day_boundaries(day: date) -> tuple[float, float]:
        """返回某一天在程序时区的起止时间戳。"""
        return day_boundaries(day)

    def _get_initial_compression_date(self) -> date:
        """
        在没有压缩状态时，决定从哪一天开始压缩

        优先从 chronicle 中最早的对话日期开始，确保不会漏掉历史天数。
        如果没有历史消息，则从今天前一天开始（相当于只压缩昨天）。
        """
        earliest_ts = self.chronicle.get_earliest_message_time()
        if earliest_ts is not None:
            return local_datetime(earliest_ts).date() - timedelta(days=1)
        return local_datetime().date() - timedelta(days=1)

    async def maybe_compress_daily(self) -> bool:
        """
        按天压缩历史对话

        每天首次被调用时，会压缩所有未压缩的完整日期（从今天往前推，不含今天）。

        Returns:
            是否实际执行了压缩（没有可压缩的完整日期时返回 False）
        """
        today = local_datetime().date()

        if self._last_compressed_date is None:
            try:
                initial_date = self._get_initial_compression_date()
            except Exception:
                logger.exception("读取每日压缩起点失败，保留待处理状态")
                return False
            self._last_compressed_date = initial_date
            self._save_compression_state()
        elif not self._compression_state_saved:
            # 摘要可能已经提交到 DB，只重试游标落盘，不重复调用模型。
            self._save_compression_state()

        # 没有需要补压的完整日期
        if self._last_compressed_date >= today - timedelta(days=1):
            return False

        pending_days: list[date] = []
        d = self._last_compressed_date + timedelta(days=1)
        while d <= today - timedelta(days=1):
            pending_days.append(d)
            d += timedelta(days=1)

        if not pending_days:
            return False

        compressed_any = False
        for d in pending_days:
            start_ts, end_ts = self._day_boundaries(d)
            try:
                messages = self.chronicle.get_messages_by_time_range(start_ts, end_ts)
            except Exception:
                logger.exception("读取每日对话失败，date=%s，下次重试", d.isoformat())
                break
            if not messages:
                # 当天没有对话，直接跳过并记录日期
                self._last_compressed_date = d
                self._save_compression_state()
                continue

            success = await self.compressor.compress_date(d, messages)
            if not success:
                logger.warning(f"[压缩失败] date={d.isoformat()}，下次重试")
                break

            self._last_compressed_date = d
            self._save_compression_state()
            compressed_any = True

        if compressed_any:
            logger.info(f"[每日压缩完成] 已处理至 {self._last_compressed_date.isoformat()}")
            await (getattr(self, "hook_bus", None) or global_hook_bus).emit_observe("on_daily_compress", {
                "last_compressed_date": self._last_compressed_date.isoformat(),
            })

        return compressed_any

    async def manual_switch_session(self) -> str:
        """
        结束当前 session 并立即创建新 session（宿主手动切换或心跳交接共用）

        新 session 复用 _create_session() 逻辑，自带最近 7 天摘要 + 最近 10 轮注入，
        保证模型在新 session 中仍保有上下文延续。

        Returns:
            新 session 的 session_id
        """
        new_session = self._create_session()
        logger.info(f"[session 切换] 新 session: {new_session.session_id}")
        return new_session.session_id

    async def manual_compress(self) -> str:
        """
        手动触发每日压缩，补压昨天及之前所有未压缩的完整日期

        Returns:
            给用户看的反馈文本
        """
        compressed = await self.maybe_compress_daily()
        last = self._last_compressed_date.isoformat() if self._last_compressed_date else "无"
        if not self.daily_compression_ready():
            return f"记忆整理未完成，已整理至 {last}；未完成的日期将稍后重试"
        if compressed:
            return f"记忆整理完成，已整理至 {last}"
        return f"没有需要整理的记忆（已整理至 {last}）"

    def _generate_session_id(self) -> str:
        """生成 session_id: sess_YYYYMMDD_XXXX"""
        now = local_datetime()
        rand = random.randint(0, 9999)
        return f"sess_{now.strftime('%Y%m%d')}_{rand:04d}"

    def _display_names(self) -> tuple[str, str]:
        """上下文标签里的称呼；名称来自配置，缺失时用中性称呼。"""
        manager = getattr(self, "manager", None)
        try:
            config = manager.snapshot() if manager is not None else {}
        except Exception:
            logger.warning("读取称呼配置失败，回退中性称呼", exc_info=True)
            config = {}
        return resolve_names(config)

    def _assemble_summary_message(self) -> str:
        """
        组装新 session 的第一条摘要消息

        包含：昨天及之前六天的摘要 + 最近十轮对话
        """
        yesterday = local_datetime().date() - timedelta(days=1)
        first_day = yesterday - timedelta(days=6)
        start_time, _ = self._day_boundaries(first_day)
        _, end_time = self._day_boundaries(yesterday)

        summaries = self.chronicle.get_summaries(start_time, end_time, strict=True)

        # 按日期分组摘要
        summaries_by_date: dict[str, list[str]] = {}
        for s in summaries:
            date_key = local_datetime(s["start_time"]).strftime("%Y-%m-%d")
            summaries_by_date.setdefault(date_key, []).append(s["summary"])

        summary_lines = ["<session_summary>", "截至昨天的最近七天摘要如下"]
        # 遍历昨天及之前六个完整日历日，没有摘要的日期也显示“无”
        for day_offset in range(7):
            date_key = (first_day + timedelta(days=day_offset)).isoformat()
            day_summaries = summaries_by_date.get(date_key, [])
            summary_lines.append(f"{date_key}：")
            if day_summaries:
                for i, summary in enumerate(day_summaries, 1):
                    summary_lines.append(f"{i}. {summary}")
            else:
                summary_lines.append("1. 无")

        summary_lines.extend(["以上便是最近七天的全部摘要", "</session_summary>"])
        recent_lines = [
            "<recent_messages>",
            "以下是最近的十轮对话交流，可能会和压缩摘要有重复",
            "",
        ]

        # 从全部归档中选择真实用户发起的最近十轮，排除心跳等后台轮次的输入与输出。
        recent_messages = self.chronicle.get_recent_rounds(
            session_id="", limit=10, conversation_only=True, strict=True,
        )
        user_name, agent_name = self._display_names()
        for msg in recent_messages:
            display_role = {"user": user_name, "assistant": agent_name}.get(msg["role"], msg["role"])
            recent_lines.append(f"{display_role}: {msg['content']}")
        recent_lines.append("</recent_messages>")

        return "\n\n".join(["这里是 session 的开始。", "\n".join(summary_lines), "\n".join(recent_lines)])

    def _create_session(self) -> Session:
        """先准备内容，再提交数据库和缓存；失败保留旧内存及旧 session。"""
        old_session = self._current_session
        session_id = self._generate_session_id()
        start_time = time.time()

        summary_message = self._assemble_summary_message()
        history = [{"role": "user", "content": summary_message}]

        session = Session(
            session_id=session_id,
            start_time=start_time,
            history=history,
            status="active",
        )

        # 不提前删除旧缓存。只对本次调用做失败回退，不引入跨重启恢复协议。
        previous_cache = (json.loads(self.cache_path.read_text(encoding="utf-8"))
                          if self.cache_path.exists() else None)
        data = {
            "session_id": session_id, "history": history, "context_tokens": 0,
            "context_warning_sent": False, "history_model": self._history_model,
        }
        cache_published = False
        try:
            with self.chronicle.session_transition(session_id, start_time):
                self._write_cache(data, self.cache_path)
                cache_published = True
        except Exception:
            # 缓存发布失败时事务自动回滚；若是随后提交 DB 失败，恢复此前缓存。
            if cache_published:
                try:
                    if previous_cache is None:
                        self.cache_path.unlink(missing_ok=True)
                    else:
                        self._write_cache(previous_cache, self.cache_path)
                except Exception:
                    logger.exception("session 切换失败后恢复旧缓存失败")
            raise

        self._current_session = session
        self._context_tokens = 0
        self._context_warning_sent = False
        if old_session is not None:
            old_session.end_time = start_time
            old_session.status = "inactive"
            self._notify_session_inactive(old_session)

        logger.info(f"[session 创建] {session_id}")
        return session

    def _notify_session_inactive(self, session: Session):
        """切换提交成功后才允许清理旧 session 的外部资源。"""
        if self._on_session_inactive is not None:
            try:
                asyncio.get_running_loop().create_task(self._safe_on_inactive(session))
            except RuntimeError:
                # 无运行中的事件循环（如同步上下文调用），跳过异步回调
                logger.warning("[session 切换] 无事件循环，跳过 session_inactive 回调")

    async def _safe_on_inactive(self, session: Session):
        """安全执行 session 切换回调，异常不影响切换流程"""
        try:
            await self._on_session_inactive(session)
        except Exception as e:
            logger.error(f"session_inactive 回调执行失败: {e}", exc_info=True)

    def _deactivate_sessions(self, session_ids: list[str]):
        """将指定 session 列表标记为 inactive"""
        for session_id in session_ids:
            try:
                db_session = self.chronicle.get_session(session_id)
                if db_session is None or db_session.get("status") != "active":
                    continue
                end_time = time.time()
                self.chronicle.upsert_session(session_id, db_session["start_time"], end_time, "inactive")
                logger.info(f"[session 结束] {session_id}")
            except Exception as e:
                logger.error(f"标记 session inactive 失败: {session_id}: {e}", exc_info=True)

    def _clear_cache(self):
        """清空缓存文件"""
        try:
            if self.cache_path.exists():
                self.cache_path.unlink()
        except Exception as e:
            logger.error(f"清空 session 缓存失败: {e}", exc_info=True)

    def _save_cache(self):
        """保存当前 active session 到缓存文件"""
        if self._current_session is None:
            self._clear_cache()
            return

        self._write_cache(self._cache_data(), self.cache_path)

    def _cache_data(self):
        return {
            "session_id": self._current_session.session_id,
            "history": self._current_session.history,
            "context_tokens": self._context_tokens,
            "context_warning_sent": self._context_warning_sent,
            "history_model": getattr(self, '_history_model', None),
        }

    def backup_history_before_repair(self):
        """修复旧历史前先原子保存原始状态；备份失败由调用方停止修复。"""
        directory = self.cache_path.parent / "history_repairs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (uuid.uuid4().hex + ".json")
        self._write_cache(self._cache_data(), path)
        return path

    @staticmethod
    def _write_cache(data, path):
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False,
                                             dir=path.parent, prefix=".session-") as f:
                temp_path = Path(f.name)
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, path)
        except Exception as e:
            logger.error(f"保存 session 缓存失败: {e}", exc_info=True)
            raise
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("清理 session 临时文件失败", exc_info=True)

    def _load_cache(self):
        """从缓存文件或数据库恢复 active session

        原则：数据库里最新的 active session 是权威来源。
        缓存只能用于恢复最新 active session 的历史；如果缓存指向旧 session，则忽略缓存。
        """
        cache_data = None
        if self.cache_path.exists():
            try:
                with open(self.cache_path, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
            except Exception as e:
                logger.error(f"读取 session 缓存失败: {e}", exc_info=True)
                self._clear_cache()

        # 先查数据库里最新的 active session（权威来源）
        latest_active = self.chronicle.get_active_session()
        if latest_active is not None:
            latest_session_id = latest_active["session_id"]

            # 如果缓存指向的正是最新的 active session，加载缓存历史
            if cache_data and cache_data.get("session_id") == latest_session_id:
                # 清理其他可能存在的旧 active session
                self._deactivate_sessions([
                    s["session_id"]
                    for s in self.chronicle.get_active_sessions()
                    if s["session_id"] != latest_session_id
                ])

                session = Session(
                    session_id=latest_session_id,
                    start_time=latest_active["start_time"],
                    end_time=latest_active.get("end_time"),
                    status="active",
                    history=cache_data.get("history", []),
                )

                self._current_session = session
                self._history_model = cache_data.get('history_model')
                # 旧版本缓存字段为 accumulated_tokens（累积值必然偏大），
                # 与新语义 context_tokens 不兼容，读到时一律重置为 0
                self._context_tokens = cache_data.get("context_tokens", 0)
                self._context_warning_sent = bool(cache_data.get("context_warning_sent", False))

                logger.info(f"[session 缓存恢复] {latest_session_id}")
                return

            # 缓存指向旧 session 或已失效：用最新 active session 恢复，清空缓存
            self._clear_cache()
            self._deactivate_sessions([
                s["session_id"]
                for s in self.chronicle.get_active_sessions()
                if s["session_id"] != latest_session_id
            ])

            session = Session(
                session_id=latest_session_id,
                start_time=latest_active["start_time"],
                end_time=latest_active.get("end_time"),
                status="active",
                history=[],
            )

            self._current_session = session
            self._context_tokens = 0
            self._context_warning_sent = False

            logger.info(f"[session DB 恢复] {latest_session_id}")
            return

        # 数据库里没有任何 active session：清空缓存，等待创建新 session
        self._clear_cache()

    def _get_warning_threshold(self) -> int:
        """上下文提醒阈值固定为当前模型上下文窗口的 40%。"""
        return int(self._context_window * WARNING_THRESHOLD_PERCENT / 100)

    def get_or_create_active_session(self) -> Session:
        """
        获取当前 active session，必要时创建新 session

        触发条件：
        - 当前没有 active session
        """
        # 如果缓存未加载，尝试从缓存或数据库恢复
        if self._current_session is None:
            self._load_cache()

        # 创建新 session
        if self._current_session is None:
            return self._create_session()

        return self._current_session

    async def add_usage(self, context_tokens: int) -> str | None:
        """
        记录当前上下文占用，首次达到阈值时返回给用户的提醒文本。

        达到阈值后保留当前 session；只有 /session 才会手动切换。
        压缩由 maybe_compress_daily() 按天触发。

        Args:
            context_tokens: 当前上下文窗口占用 token 数
                （本轮最后一次 API 请求的 prompt_tokens，即整个 history + system 的实时大小）

        Returns:
            每个 session 首次达到阈值时返回提醒文本，否则返回 None。
        """
        self._context_tokens = context_tokens
        threshold = self._get_warning_threshold()
        warning = None
        if self._current_session is not None and not self._context_warning_sent and context_tokens >= threshold:
            self._context_warning_sent = True
            percent = context_tokens / self._context_window * 100
            warning = (
                f"[Session 提醒] 当前上下文已占用 {percent:.1f}%"
                f"（{context_tokens:,} / {self._context_window:,} tokens），已达到提醒阈值。"
                "当前 session 会继续保留；需要切换时请发送 /session。"
            )
            logger.info(
                f"[token 阈值提醒] context={self._context_tokens}, threshold={threshold}"
            )
        self._save_cache()
        return warning

    def record_input_time(self):
        """记录用户最新输入时间（保留接口，仅用于刷新缓存）"""
        self._save_cache()

    def save_session(self):
        """显式保存当前 session 状态到缓存"""
        self._save_cache()

    def restore_session(
        self,
        session_id: str,
        start_time: float,
        history: list[dict],
        context_tokens: int,
        history_model: dict | None = None,
    ):
        """由对话回退恢复内存 session 与缓存；数据库元数据由调用方原子处理。"""
        if self._current_session is None or self._current_session.session_id != session_id:
            self._context_warning_sent = False
        self._current_session = Session(
            session_id=session_id,
            start_time=start_time,
            history=history,
            status="active",
        )
        self._context_tokens = context_tokens
        # Restore the checkpoint's identity: the active model may have changed
        # since that history was captured. Legacy checkpoints remain unknown.
        self._history_model = history_model
        self._save_cache()

    @property
    def current_session(self) -> Optional[Session]:
        return self._current_session

    @property
    def context_tokens(self) -> int:
        return self._context_tokens
