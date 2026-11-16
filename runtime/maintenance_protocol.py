"""Small request and status format shared by the app and the host worker."""

import re
from pathlib import Path

SOCKET = Path("/run/tentacle-maintenance/command.sock")
STATUS_ROOT = Path("/usr/local/share/tentacle")
OPERATIONS = "maintenance.json"
REQUEST_LIMIT = 512
ACTIONS = ("check", "update", "restart")
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?\Z")


def validate_action(action, version):
    if action not in ACTIONS:
        raise ValueError("未知维护操作")
    if action == "update":
        if not isinstance(version, str) or len(version) > 40 or not VERSION.fullmatch(version):
            raise ValueError("更新必须指定已检查的正式版本")
    elif version is not None:
        raise ValueError("只有更新操作接受版本参数")


def valid_id(operation_id):
    return isinstance(operation_id, str) and re.fullmatch(r"[0-9a-f]{32}", operation_id) is not None


def worker_identity(pid):
    """Distinguish an interrupted worker from a reused PID or a reboot."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        process = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if process[0] != "Z":
            return f"{boot}:{process[19]}"
    except (OSError, IndexError):
        pass
    return None
