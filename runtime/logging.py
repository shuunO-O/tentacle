"""
日志模块

职责：
1. 从配置文件读取日志配置
2. 配置 root logger，统一输出到文件
3. 使用 RotatingFileHandler 实现日志轮转

注意：所有日志统一输出到文件，不输出到标准输出。
"""

import logging
from logging.handlers import RotatingFileHandler

from config.files import ConfigFiles
from runtime.paths import state_path
from runtime.timezone import DEFAULT_TIMEZONE, configure_timezone, local_datetime


class ProgramTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        current = local_datetime(record.created)
        if datefmt:
            return current.strftime(datefmt)
        return f"{current.strftime('%Y-%m-%d %H:%M:%S')},{int(record.msecs):03d}"


def setup_logging(config_path: str | None = None):
    """
    初始化日志系统

    从配置文件读取日志配置，配置 root logger。

    Args:
        config_path: 用户配置文件路径，默认使用当前布局的用户配置与版本默认值
    """
    files = ConfigFiles(config_path)
    if not files.path.exists() and not files.defaults_path.exists():
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        return

    config = files.read()[2]

    configure_timezone((config.get("app") or {}).get("timezone", DEFAULT_TIMEZONE))

    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    fmt = log_config.get("format", "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    formatter = ProgramTimeFormatter(fmt)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    root_logger.handlers.clear()

    # 所有日志统一输出到文件，不输出到标准输出
    file_config = log_config.get("file", {})
    if file_config.get("enabled", False):
        log_path = state_path(file_config.get("path", "logs/agent.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)

        max_bytes = file_config.get("max_bytes", 10 * 1024 * 1024)
        backup_count = file_config.get("backup_count", 5)

        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

