"""
WebUI 设置模块

持久化到 data/webui_settings.json：
- show_reasoning: 是否显示模型思考过程（reasoning）
- split_messages: 是否把 agent 输出按空行分段成多条气泡（仅前端展示，不改归档）
- password_hash / password_salt: 可选访问密码（sha256 加盐哈希）
- device_auth: 设备验证开关；device_tokens: 已绑定设备（sha256 -> 元数据）

双层鉴权相互独立：密码会话 token 保存在内存，服务重启后失效需重新登录；
设备令牌经 HttpOnly cookie 下发（服务端只存 sha256），重启不失效，直到被吊销。

一次性绑定码只由主机脚本 scripts/device_token.py gen-code 产生，写入
data/device_enroll_pending.json（本模块只读 + 用后删除，避免与运行实例的
内存态互相覆盖）；紧急冻结同理走 data/device_auth_freeze.json。
新设备入口走 data/device_access_policy.json（实时读盘）；WebUI 只能关闭，
重新开放仅由主机脚本 allow-new 完成，不与设置内存态互相覆盖。

（注：内部草稿机制已移除，原 show_content 字段已废弃并清理。）
"""

import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from web.auth_files import atomic_write_json
from runtime.timezone import timezone_name

logger = logging.getLogger(__name__)


def configured_agent_name() -> str:
    """配置里的助手称呼；未配置时留空，由调用方决定兜底名。"""
    try:
        from config.manager import ConfigManager

        app = (ConfigManager().snapshot() or {}).get("app") or {}
        return str(app.get("agent_name") or "").strip()
    except Exception:
        logger.warning("读取 app.agent_name 失败，回退默认显示名", exc_info=True)
        return ""


DEFAULT_SETTINGS = {
    "show_reasoning": False,
    "split_messages": True,
    "password_hash": "",
    "password_salt": "",
    "device_auth": False,
    "device_tokens": {},
}

TOKEN_TTL_SECONDS = 12 * 3600

DEVICE_COOKIE = "tentacle_device"
DEVICE_COOKIE_MAX_AGE = 10 * 365 * 24 * 3600
DEVICE_LAST_SEEN_THROTTLE = 60  # last_seen 落盘节流（秒），防止高频请求反复写文件
ENROLL_CODE_TTL_SECONDS = 15 * 60


class SettingsWriteError(RuntimeError):
    """鉴权状态未成功落盘，不能向调用者确认成功。"""


class WebSettings:
    """WebUI 设置 + 内存 token 管理（单例）"""

    def __init__(self, path: str = "data/webui_settings.json"):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict = self._load()
        self._tokens: dict[str, float] = {}
        self._auth_revision = 0

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("设置文件不是 JSON 对象")
            data.pop("show_content", None)
            data.pop("agent_name", None)  # 名字统一来自 app.agent_name，旧显示覆盖不再使用。
            data.setdefault("device_tokens", {})
            merged = {**DEFAULT_SETTINGS, **data}
            self._validate_auth_data(merged)
            return merged
        except FileNotFoundError:
            # 只有首次启动（文件不存在）可以使用无鉴权的默认值。
            return {**DEFAULT_SETTINGS, "device_tokens": {}}
        except (OSError, ValueError, TypeError) as error:
            raise RuntimeError("WebUI 鉴权设置无法读取或已损坏，拒绝使用无鉴权默认值") from error

    @staticmethod
    def _timestamp(value) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)

    @staticmethod
    def _digest(value) -> bool:
        return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)

    @classmethod
    def _validate_auth_data(cls, data):
        if not isinstance(data["device_auth"], bool):
            raise ValueError("device_auth 必须是布尔值")
        password_hash, salt = data["password_hash"], data["password_salt"]
        if not isinstance(password_hash, str) or not isinstance(salt, str):
            raise ValueError("密码设置格式无效")
        if bool(password_hash) != bool(salt) or (password_hash and not cls._digest(password_hash)):
            raise ValueError("密码设置不完整")
        tokens = data["device_tokens"]
        if not isinstance(tokens, dict):
            raise ValueError("设备表格式无效")
        for tid, rec in tokens.items():
            if (not cls._digest(tid) or not isinstance(rec, dict)
                    or not cls._timestamp(rec.get("created"))
                    or not cls._timestamp(rec.get("last_seen", 0))
                    or not isinstance(rec.get("name", ""), str)):
                raise ValueError("设备记录格式无效")

    def save(self):
        with self._lock:
            try:
                atomic_write_json(self.path, self._data)
            except (OSError, ValueError, TypeError) as error:
                logger.error("保存 WebUI 设置失败", exc_info=True)
                raise SettingsWriteError("WebUI 设置保存失败") from error

    @contextmanager
    def _change(self):
        """写入失败时恢复内存态，防止重启后恢复已吊销令牌或旧开关。"""
        with self._lock:
            data, tokens, revision = deepcopy(self._data), self._tokens.copy(), self._auth_revision
            try:
                yield
                self.save()
            except Exception:
                self._data, self._tokens, self._auth_revision = data, tokens, revision
                raise

    @property
    def auth_revision(self) -> int:
        return self._auth_revision

    @property
    def agent_name(self) -> str:
        return configured_agent_name() or "Tentacle"

    @property
    def show_reasoning(self) -> bool:
        return bool(self._data.get("show_reasoning"))

    @property
    def split_messages(self) -> bool:
        return bool(self._data.get("split_messages"))

    @property
    def has_password(self) -> bool:
        return bool(self._data.get("password_hash"))

    def get_public(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "show_reasoning": self.show_reasoning,
            "split_messages": self.split_messages,
            "has_password": self.has_password,
            "timezone": timezone_name(),
        }

    def update(self, show_reasoning=None, split_messages=None):
        with self._change():
            return self._update(show_reasoning, split_messages)

    def _update(self, show_reasoning, split_messages):
        changed = False
        if show_reasoning is not None and bool(show_reasoning) != self.show_reasoning:
            self._data["show_reasoning"] = bool(show_reasoning)
            changed = True
        if split_messages is not None and bool(split_messages) != self.split_messages:
            self._data["split_messages"] = bool(split_messages)
            changed = True
        return changed

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

    def verify(self, password: str) -> bool:
        if not self.has_password:
            return True
        salt = self._data.get("password_salt", "")
        return hmac.compare_digest(
            self._hash(password, salt), self._data.get("password_hash", "")
        )

    def set_password(self, password: str | None):
        """设置或清除密码；清空 token 使所有会话失效。"""
        with self._change():
            self._set_password(password)

    def _set_password(self, password):
        if not password:
            self._data["password_hash"] = ""
            self._data["password_salt"] = ""
        else:
            salt = secrets.token_hex(16)
            self._data["password_salt"] = salt
            self._data["password_hash"] = self._hash(password, salt)
        self._tokens.clear()
        self._auth_revision += 1

    def issue_token(self) -> str:
        token = secrets.token_hex(32)
        self._tokens[token] = time.time() + TOKEN_TTL_SECONDS
        return token

    def valid_token(self, token: str | None) -> bool:
        if not self.has_password:
            return True
        if not token:
            return False
        exp = self._tokens.get(token)
        if exp is None:
            return False
        if time.time() > exp:
            self._tokens.pop(token, None)
            return False
        return True

    # ---------- 设备令牌（与密码层独立；管理原则：WebUI 只降权不扩权） ----------

    def _device_pending_path(self) -> Path:
        """一次性绑定码文件：scripts/device_token.py gen-code 写入，此处只读 + 用后删除"""
        return self.path.parent / "device_enroll_pending.json"

    def _freeze_path(self) -> Path:
        """CLI 紧急冻结文件：存在即冻结生效（实时读盘，WebUI 无法改写）"""
        return self.path.parent / "device_auth_freeze.json"

    def _device_policy_path(self) -> Path:
        return self.path.parent / "device_access_policy.json"

    @property
    def allow_new_devices(self) -> bool:
        """不存在策略文件时兼容旧流程；文件损坏或不可读时禁止新设备。"""
        try:
            policy = json.loads(self._device_policy_path().read_text(encoding="utf-8"))
            if not isinstance(policy, dict) or not isinstance(policy.get("allow_new_devices"), bool):
                return False
            return policy["allow_new_devices"]
        except FileNotFoundError:
            return True
        except (OSError, ValueError):
            return False

    def deny_new_devices(self):
        """只关闭入口；主机重新开放时替换独立策略文件，无需重启。"""
        with self._lock:
            try:
                atomic_write_json(self._device_policy_path(), {"allow_new_devices": False})
            except (OSError, ValueError, TypeError) as error:
                raise SettingsWriteError("新设备入口设置保存失败") from error

    @property
    def device_auth(self) -> bool:
        return bool(self._data.get("device_auth"))

    def freeze_info(self) -> dict | None:
        try:
            freeze = json.loads(self._freeze_path().read_text(encoding="utf-8"))
            if not isinstance(freeze, dict) or not self._timestamp(freeze.get("created")):
                raise ValueError("冻结文件格式无效")
            return freeze
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            # 无法确定冻结边界时，任何设备都不能凭旧 cookie 通过。
            return {"invalid": True}

    @property
    def requires_device(self) -> bool:
        """紧急冻结独立于设备验证开关，不能从 WebUI 绕过。"""
        return not self.allow_new_devices or self.device_auth or self.freeze_info() is not None

    def valid_device(self, token: str | None) -> dict | None:
        with self._lock:
            return self._valid_device(token)

    def _valid_device(self, token: str | None) -> dict | None:
        """
        校验设备 cookie token，返回设备记录（含 id）。

        冻结生效时，冻结时刻之前绑定的设备一律拒绝——冻结后凭新绑定码
        登记的设备不受影响，保证主机总闸不把主人自己也锁死在外面。
        """
        if not token:
            return None
        tid = hashlib.sha256(token.encode("utf-8")).hexdigest()
        rec = (self._data.get("device_tokens") or {}).get(tid)
        if rec is None:
            return None
        freeze = self.freeze_info()
        if freeze is not None and (freeze.get("invalid") or rec["created"] <= freeze["created"]):
            return None
        now = time.time()
        if now - float(rec.get("last_seen", 0)) >= DEVICE_LAST_SEEN_THROTTLE:
            try:
                with self._change():
                    rec["last_seen"] = now
            except SettingsWriteError:
                return None
        return {
            "id": tid,
            "name": rec.get("name", ""),
            "created": rec.get("created"),
            "last_seen": rec.get("last_seen"),
        }

    def create_device(self, name: str = "") -> str:
        """内部登记原语；HTTP 登记必须经 enroll_device 验证主机绑定码。"""
        with self._change():
            return self._create_device(name)

    def _create_device(self, name):
        token = secrets.token_hex(32)
        tid = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        (self._data.setdefault("device_tokens", {}))[tid] = {
            "name": (name or "").strip() or "未命名设备",
            "created": now,
            "last_seen": now,
        }
        return token

    def enroll_device(self, code: str, name: str = "") -> str | None:
        """凭一次性绑定码绑定设备；成功返回明文 token，失败返回 None（码已焚毁）"""
        with self._lock:
            if not self.allow_new_devices:
                return None
            pending = self._read_json_file(self._device_pending_path())
            if not self._consume_pending_code(code):
                return None
            return self.create_device(name or (pending or {}).get("name", ""))

    def pending_code_info(self) -> dict | None:
        """待用绑定码的元信息（不消费），供 status 脚本与设置页提示"""
        pending = self._read_json_file(self._device_pending_path())
        if not pending:
            return None
        return {"name": pending.get("name", ""), "expires": pending.get("expires")}

    def _consume_pending_code(self, code: str) -> bool:
        """校验并消费一次性绑定码；只有码正确才焚毁文件（错误尝试不销毁）"""
        pending = self._read_json_file(self._device_pending_path())
        if not pending:
            return False
        if not self._digest(pending.get("code_sha256")) or not self._timestamp(pending.get("expires")):
            return False
        digest = hashlib.sha256((code or "").strip().encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, pending.get("code_sha256", "")):
            return False
        expired = time.time() > float(pending.get("expires", 0))
        self._device_pending_path().unlink(missing_ok=True)  # 过期码一并焚毁，避免残留歧义
        return not expired

    def list_devices(self) -> list[dict]:
        items = [
            {
                "id": tid,
                "name": rec.get("name", ""),
                "created": rec.get("created"),
                "last_seen": rec.get("last_seen"),
            }
            for tid, rec in (self._data.get("device_tokens") or {}).items()
        ]
        items.sort(key=lambda d: d.get("created") or 0, reverse=True)
        return items

    def revoke_device(self, device_id: str) -> bool:
        with self._change():
            tokens = self._data.get("device_tokens") or {}
            if device_id not in tokens:
                return False
            del tokens[device_id]
            return True

    def set_device_auth(self, enabled: bool):
        with self._change():
            if self.freeze_info() is not None:
                raise ValueError("紧急冻结期间无法切换设备验证，请在主机解除冻结")
            self._data["device_auth"] = bool(enabled)

    @staticmethod
    def _read_json_file(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None


_web_settings_instance: WebSettings | None = None


def get_web_settings(path: str = "data/webui_settings.json") -> WebSettings:
    """获取 WebSettings 单例实例"""
    global _web_settings_instance
    if _web_settings_instance is None:
        _web_settings_instance = WebSettings(path)
    return _web_settings_instance
