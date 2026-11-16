"""程序日历时区，独立于服务器 TZ；启动配置，运行期间不动态切换。"""

import time
from datetime import date, datetime, time as day_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"
_timezone_name = DEFAULT_TIMEZONE


def timezone_for(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("程序时区必须是有效的 IANA 名称，例如 Asia/Shanghai")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"无效的程序时区：{name}，请使用 IANA 时区名称") from exc


def configure_timezone(name: str = DEFAULT_TIMEZONE):
    timezone_for(name)
    global _timezone_name
    _timezone_name = name


def timezone_name() -> str:
    return _timezone_name


def get_timezone() -> ZoneInfo:
    return timezone_for(_timezone_name)


def local_datetime(timestamp: float | None = None) -> datetime:
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, get_timezone())


def day_boundaries(day: date) -> tuple[float, float]:
    """程序日历日的闭区间；按次日零点计算，兼容夏令时的 23/25 小时日。"""
    start = datetime.combine(day, day_time.min, tzinfo=get_timezone()).timestamp()
    next_start = datetime.combine(day + timedelta(days=1), day_time.min, tzinfo=get_timezone()).timestamp()
    return start, next_start - 0.000001


def localize(value: datetime) -> datetime:
    """显式偏移原样保留；未标时区的输入按程序时区解释，拒绝夏令时缺失时刻。"""
    if value.tzinfo is not None:
        return value
    aware = value.replace(tzinfo=get_timezone())
    if local_datetime(aware.timestamp()).replace(tzinfo=None) != value:
        raise ValueError("该本地时刻因夏令时切换而不存在，请指定有效时间或显式 UTC 偏移")
    return aware
