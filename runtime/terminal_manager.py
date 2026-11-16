"""
真实终端会话管理模块（PTY）

通过 pty.fork() 创建一个真实的 shell 子进程，父进程通过 master fd 读写。
这是"真实终端"的核心：程序感知到自己运行在真实终端中，支持交互式行为、
ANSI 颜色、cd/export/alias/后台任务等会话状态保持。

关键设计：
- 单例：全局只维护一个持久 shell 会话，跨工具调用保持状态
- 哨兵：命令后追加 `echo __DONE_<rand>_$?__`，读到哨兵即判定命令结束，
  从而"直接获取完整输出 + 退出码"；超时兜底处理不结束的命令
- 安全审查：执行前校验黑名单（交互式/监视类程序），命中即拒绝
- 实时输出：通过 output_hook 把增量输出推送给控制台/WebUI，实现人类可见
"""

import asyncio
import logging
import os
import pty
import re
import select
import shlex
import signal
import struct
import sys
import threading
import time
import uuid

logger = logging.getLogger(__name__)

# ANSI 转义序列剥离（给模型看的干净文本）
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"                # CSI ... letter
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC ... (BEL 或 ST 结尾)
    r"|\x1b[=>]"                              # 单独的 ESC > / ESC =
)
# 字面转义序列：\xNN \r \n \t（模型端写 \\x03 表示 Ctrl+C）
_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")
# 哨兵行：__TENTACLE_DONE_<rand8>_<exitcode>__
_SENTINEL_RE = re.compile(r"__TENTACLE_DONE_([0-9a-f]{8})_(\d+)__")

# 命令包装器：遇到这些前缀词时继续检查下一个词是否为黑名单命令
_WRAPPERS = {"sudo", "env", "nohup", "time", "command", "xargs", "nice", "setsid", "ts"}

# 返回给模型的最大输出长度，超长中间截断
_MAX_OUTPUT = 8000

# 默认安全黑名单：交互式/监视类程序（命中即拒绝执行）
_DEFAULT_DENY = {
    "top", "htop", "btop", "vim", "vi", "nano",
    "less", "more", "watch", "ssh", "telnet", "ftp",
}

# 保护区写命令：作用于 L0 禁区/不可变清单文件时拒绝（防呆层，见 _protected_write_check）
_PROTECTED_WRITE_CMDS = {
    "rm", "mv", "cp", "tee", "truncate", "chmod", "chown",
    "ln", "unlink", "shred", "install",
}

# 不可变清单文件的 basename（与 tools/file_edit.py 的 _IMMUTABLE_FILES 保持一致；
# 本文件自身也在清单内，此处重复定义是因为清单必须先于编辑器存在才能保护编辑器）
_IMMUTABLE_BASENAMES = {
    "file_edit.py", "str_replace_editor.py", "terminal_manager.py", "self_backup.py",
    "self_rollback.py", "rollback.py",
}


class TerminalManager:
    """真实终端会话管理（单例）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._master_fd = None
            cls._instance._pid = None
            cls._instance._shell = None
            cls._instance._cwd = None
            cls._instance._default_timeout = 30.0
            cls._instance._deny_commands = set(_DEFAULT_DENY)
            cls._instance._output_hook = None
            cls._instance._lock = threading.Lock()
            cls._instance._supported = not sys.platform.startswith("win") and pty is not None
        return cls._instance

    # ---------- 配置 ----------

    def configure(self, shell=None, cwd=None, default_timeout=None, deny_commands=None):
        if shell:
            self._shell = shell
        if cwd:
            self._cwd = cwd
        if default_timeout is not None:
            self._default_timeout = float(default_timeout)
        if deny_commands is not None:
            self._deny_commands = set(deny_commands)
        logger.info(
            f"终端配置: shell={shell or '系统默认'}, cwd={cwd or '当前目录'}, "
            f"default_timeout={self._default_timeout}s"
        )

    def set_output_hook(self, fn):
        """设置实时输出回调 fn(data: str)，输出增量会推送给人类可见界面"""
        self._output_hook = fn

    # ---------- 会话生命周期 ----------

    def ensure_session(self):
        if not self._supported:
            raise OSError("当前平台不支持真实终端（仅 Unix）")
        if self._master_fd is not None and self._pid is not None:
            return
        self._spawn()

    def _spawn(self):
        shell = self._shell or os.environ.get("SHELL") or "/bin/bash"
        cwd = self._cwd or os.getcwd()
        try:
            pid, fd = pty.fork()
        except OSError as e:
            logger.error(f"pty.fork 失败: {e}")
            raise
        if pid == 0:
            # 子进程：切换目录并启动 shell
            try:
                os.chdir(cwd)
            except OSError:
                pass
            try:
                os.execvp(shell, [shell])
            except Exception:
                os._exit(127)
        self._pid = pid
        self._master_fd = fd
        self._set_winsize(120, 40)
        # 等待 shell 初始化并清空欢迎输出/首提示符
        time.sleep(0.3)
        self._drain()
        logger.info(f"终端会话已创建: pid={pid}, shell={shell}, cwd={cwd}")

    def _set_winsize(self, rows, cols):
        try:
            import fcntl
            import termios
            fcntl.ioctl(
                self._master_fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0),
            )
        except Exception:
            pass

    def _drain(self):
        """清空当前所有可读缓冲（不触发 output_hook）"""
        while True:
            try:
                r, _, _ = select.select([self._master_fd], [], [], 0.05)
            except (OSError, ValueError):
                break
            if not r:
                break
            try:
                data = os.read(self._master_fd, 65536)
            except OSError:
                break
            if not data:
                break

    def _alive(self) -> bool:
        if not self._pid:
            return False
        try:
            os.kill(self._pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def close(self):
        """关闭终端会话"""
        with self._lock:
            if self._pid:
                try:
                    os.kill(self._pid, signal.SIGTERM)
                except OSError:
                    pass
            if self._master_fd is not None:
                try:
                    os.close(self._master_fd)
                except OSError:
                    pass
            self._pid = None
            self._master_fd = None
        logger.info("终端会话已关闭")

    def status(self) -> str:
        if not self._supported:
            return "当前平台不支持真实终端（仅 Unix）"
        if self._master_fd is None:
            return "终端会话未创建"
        return (
            f"终端会话 {'活跃' if self._alive() else '已退出'}: "
            f"pid={self._pid}, cwd={self._cwd or os.getcwd()}, "
            f"shell={self._shell or os.environ.get('SHELL', '/bin/bash')}"
        )

    # ---------- 安全审查 ----------

    @staticmethod
    def _is_protected_target(token: str) -> bool:
        """判断一个 shell 词是否指向 L0 禁区/不可变清单文件"""
        t = token.strip().strip("'\"")
        if not t:
            return False
        base = os.path.basename(os.path.normpath(t))
        if base == ".env":
            return True
        if "self_backups" in t:
            return True
        if base in _IMMUTABLE_BASENAMES:
            return True
        return False

    def _protected_reason(self, target: str, via: str) -> str:
        return (
            f"[安全审查] 命令试图通过{via}修改受保护文件 '{target}'"
            "（.env / data/self_backups / 自我修改安全网文件），已拒绝。"
            "此类文件只能手动维护；如需回滚请在 Web 设置页或通过 scripts/rollback.py 操作。"
        )

    def _protected_write_check(self, command: str):
        """检查是否试图用 shell 修改自我修改安全网文件（L0 禁区/不可变清单）。

        定位是防呆层：python -c 等间接路径无法封死，
        真正兜底是自动备份 + self_rollback 回滚 + audit.log 审计。
        """
        segments = re.split(r"[|;&\n]", command)
        for seg in segments:
            try:
                words = shlex.split(seg.strip())
            except ValueError:
                words = seg.strip().split()
            if not words:
                continue

            # 重定向目标：> >> 2> &> 等（shlex 把它们切成独立 token）
            for i, w in enumerate(words):
                if w.endswith(">"):
                    target = words[i + 1] if i + 1 < len(words) else ""
                    if target and self._is_protected_target(target):
                        return self._protected_reason(target, "shell 重定向")

            # sed -i 原地改写
            if os.path.basename(words[0]) == "sed" and any(
                w in ("-i", "--in-place") or w.startswith("--in-place=") for w in words
            ):
                offender = next((w for w in words[1:] if self._is_protected_target(w)), None)
                if offender:
                    return self._protected_reason(offender, "sed -i")

            # rm/mv/cp/truncate/chmod/chown/dd/tee 等：其后参数任一命中即拦（保守策略，宁可误伤）
            for i, w in enumerate(words):
                if os.path.basename(w) in _PROTECTED_WRITE_CMDS:
                    offender = next((x for x in words[i + 1:] if self._is_protected_target(x)), None)
                    if offender:
                        return self._protected_reason(offender, f"'{w}'")
        return None

    def _safety_check(self, command: str):
        """检查命令是否命中黑名单（交互式/监视类程序），返回拒绝原因或 None"""
        segments = re.split(r"[|;&<>\n]", command)
        for seg in segments:
            try:
                words = shlex.split(seg.strip())
            except ValueError:
                words = seg.strip().split()
            if not words:
                continue
            idx = 0
            while idx < len(words):
                base = os.path.basename(words[idx])
                if base in self._deny_commands:
                    return (
                        f"[安全审查] 命令 '{words[idx]}' 被禁止执行（交互式/监视类程序）。"
                        "此类操作请改为手动执行。"
                    )
                if base in _WRAPPERS and idx + 1 < len(words):
                    idx += 1
                    continue
                break
        return None

    # ---------- 底层读取 ----------

    def _read_available(self, timeout: float) -> tuple[str, bool]:
        """读取 PTY 当前可用的输出，最多等待 timeout 秒。

        返回 (输出文本, 是否 EOF)。输出会同时推送给 output_hook（人类可见）。
        EOF 时自动清理会话资源，下次调用可重新 spawn。
        """
        chunks = []
        deadline = time.monotonic() + timeout
        eof = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                r, _, _ = select.select([self._master_fd], [], [], remaining)
            except (OSError, ValueError):
                break
            if not r:
                break
            try:
                data = os.read(self._master_fd, 65536)
            except OSError:
                break
            if not data:
                # 子进程退出（EOF），清理会话
                eof = True
                if self._master_fd is not None:
                    try:
                        os.close(self._master_fd)
                    except OSError:
                        pass
                    self._master_fd = None
                self._pid = None
                break
            text = data.decode("utf-8", errors="replace")
            chunks.append(text)
            if self._output_hook:
                try:
                    self._output_hook(text)
                except Exception:
                    logger.exception("终端输出 hook 异常")
        return "".join(chunks), eof

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return _ANSI_RE.sub("", text)

    @staticmethod
    def _decode_escapes(text: str) -> str:
        """把字面转义序列解释为实际控制字节：\\x03 -> Ctrl+C、\\r -> 回车、\\n / \\t"""
        def _hex(m):
            return chr(int(m.group(1), 16))
        text = _ESCAPE_RE.sub(_hex, text)
        return (
            text.replace("\\r", "\r")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )

    @staticmethod
    def _truncate(text: str, limit: int = _MAX_OUTPUT) -> str:
        if len(text) <= limit:
            return text
        head = text[: int(limit * 0.6)]
        tail = text[-int(limit * 0.4):]
        return f"{head}\n...[输出过长，已截断，共 {len(text)} 字符]...\n{tail}"

    # ---------- 对外接口 ----------

    async def run(self, command: str, timeout=None, cwd=None) -> str:
        """执行一条命令，返回完整输出 + 状态（文本）"""
        return await asyncio.to_thread(self._run_sync, command, timeout, cwd)

    def _run_sync(self, command, timeout, cwd):
        if not command or not command.strip():
            return "[错误] command 不能为空"
        reason = self._safety_check(command)
        if reason:
            logger.warning(f"[安全审查] 拒绝执行: {command}")
            return reason
        reason = self._protected_write_check(command)
        if reason:
            logger.warning(f"[安全审查] 拒绝执行: {command}")
            return reason
        with self._lock:
            try:
                self.ensure_session()
            except OSError as e:
                return f"[错误] 终端不可用: {e}"
            if self._master_fd is None:
                return "[错误] 终端不可用（仅支持 Unix）"

            timeout = float(timeout or self._default_timeout)
            sentinel = uuid.uuid4().hex[:8]
            sentinel_cmd = f"echo __TENTACLE_DONE_{sentinel}_$?__"
            full_cmd = command
            if cwd:
                full_cmd = f"cd {shlex.quote(str(cwd))} && {full_cmd}"
            cmd_line = f"{full_cmd}; {sentinel_cmd}\r"
            logger.info(f"[terminal_run] 执行: {full_cmd} (timeout={timeout}s)")
            try:
                os.write(self._master_fd, cmd_line.encode("utf-8"))
            except OSError as e:
                return f"[错误] 写入终端失败: {e}"

            # 读取直到哨兵或超时
            full_buf = ""
            exit_code = None
            timed_out = False
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                data, eof = self._read_available(min(remaining, 0.5))
                if data:
                    full_buf += data
                if eof:
                    break
                m = _SENTINEL_RE.search(full_buf)
                if m:
                    exit_code = int(m.group(2))
                    break

            # 剔除哨兵命令的回显（终端 echo 显示的是字面 $?）及哨兵行本身
            echo_cmd = re.compile(re.escape(f"; echo __TENTACLE_DONE_{sentinel}_$?__"))
            clean = echo_cmd.sub("", full_buf)
            clean = _SENTINEL_RE.sub("", clean)
            clean = self._strip_ansi(clean).replace("\r\n", "\n").replace("\r", "\n")
            clean = self._truncate(clean.strip())

            if timed_out:
                result = f"仍在运行 | 已等待:{timeout:g}s\n{clean}"
            else:
                code = exit_code if exit_code is not None else "?"
                result = f"退出码:{code}\n{clean}"
            logger.info(f"[terminal_run] 完成: {full_cmd} -> {result.splitlines()[0]}")
            return result

    async def interact(self, text="", newline=False, timeout=None) -> str:
        """与正在运行的前台程序交互：发送按键/文本并读取新输出"""
        return await asyncio.to_thread(self._interact_sync, text, newline, timeout)

    def _interact_sync(self, text, newline, timeout):
        with self._lock:
            try:
                self.ensure_session()
            except OSError as e:
                return f"[错误] 终端不可用: {e}"
            if self._master_fd is None:
                return "[错误] 终端不可用（仅支持 Unix）"
            timeout = float(timeout if timeout is not None else 2.0)
            if text:
                data = self._decode_escapes(str(text))
                if newline:
                    data += "\r"
                logger.info(f"[terminal_interact] 发送: {data!r} (newline={newline})")
                try:
                    os.write(self._master_fd, data.encode("utf-8"))
                except OSError as e:
                    return f"[错误] 写入终端失败: {e}"
            out, eof = self._read_available(timeout)
            clean = _SENTINEL_RE.sub("", out)
            clean = self._strip_ansi(clean).replace("\r\n", "\n").replace("\r", "\n")
            clean = self._truncate(clean.strip())
            if eof:
                return f"{clean}\n终端已退出".lstrip("\n")
            return clean or "暂无新输出"


def get_terminal_manager() -> TerminalManager:
    """全局终端管理器单例"""
    return TerminalManager()
