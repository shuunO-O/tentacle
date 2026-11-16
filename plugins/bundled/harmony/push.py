"""Huawei Push Kit v3; credentials use environment values or an environment-named JSON file."""

from __future__ import annotations

import base64
from collections.abc import Callable
import json
import logging
import os
import re
import time

import httpx

from runtime.paths import state_path


logger = logging.getLogger(__name__)
DEFAULT_AUDIENCE = "https://oauth-login.cloud.huawei.com/oauth2/v3/token"
CREDENTIAL_FIELDS = {
    "project_id": "HUAWEI_PUSH_PROJECT_ID",
    "key_id": "HUAWEI_PUSH_KEY_ID",
    "sub_account": "HUAWEI_PUSH_SUB_ACCOUNT",
    "private_key": "HUAWEI_PUSH_PRIVATE_KEY",
    "token_uri": "HUAWEI_PUSH_AUDIENCE",
}


class HarmonyPush:
    """华为 Push v3（实况窗与测试通知）客户端，不在日志/异常中暴露密钥或消息正文。"""

    def __init__(self, env_getter: Callable[[str, str | None], str | None] | None = None):
        self._env = env_getter or (lambda key, default=None: os.environ.get(key) or default)
        self._credentials: dict[str, str] = {}
        self._configuration_error = ""
        filename = (self._env("HUAWEI_PUSH_CREDENTIALS_FILE", "") or "").strip()
        if not filename:
            self._credentials = {
                field: (self._env(name, "") or "")
                for field, name in CREDENTIAL_FIELDS.items()
            }
            self._credentials["token_uri"] = self._credentials["token_uri"] or DEFAULT_AUDIENCE
            return
        try:
            self._credentials = self._read_credentials(filename)
        except ValueError as error:
            # Never include filenames, JSON contents, private keys, or exception chains.
            self._configuration_error = str(error)
            logger.warning("[push] %s", self._configuration_error)

    @staticmethod
    def _read_credentials(filename: str) -> dict[str, str]:
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        try:
            path = state_path(filename)
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            raise ValueError("推送服务账号文件无法读取或不是有效 JSON，请检查 HUAWEI_PUSH_CREDENTIALS_FILE") from None
        if not isinstance(data, dict):
            raise ValueError("推送服务账号文件必须是 JSON 对象")
        required = ("project_id", "key_id", "sub_account", "private_key")
        if not all(isinstance(data.get(name), str) and data[name].strip() for name in required):
            raise ValueError("推送服务账号字段不完整，需要 project_id、key_id、sub_account、private_key")
        credentials = {name: data[name] for name in required}
        credentials["token_uri"] = data.get("token_uri", DEFAULT_AUDIENCE)
        if not isinstance(credentials["token_uri"], str) or credentials["token_uri"] != DEFAULT_AUDIENCE:
            raise ValueError("推送服务账号 token_uri 无效")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", credentials["project_id"]):
            raise ValueError("推送项目ID格式错误")
        try:
            key = serialization.load_pem_private_key(
                credentials["private_key"].replace("\\n", "\n").encode(), password=None
            )
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ValueError("推送服务账号私钥格式无效") from None
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("推送服务账号需要至少 2048 位 RSA 私钥")
        return credentials

    @property
    def configured(self) -> bool:
        return not self._configuration_error and all(self._credentials.get(name) for name in CREDENTIAL_FIELDS)

    def jwt(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        def encode(value) -> bytes:
            raw = json.dumps(value, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        if not self.configured:
            raise ValueError(self._configuration_error or "推送服务账号配置不完整")
        credentials = self._credentials
        now = int(time.time())
        header = encode({"alg": "PS256", "typ": "JWT", "kid": credentials["key_id"]})
        payload = encode({"iss": credentials["sub_account"], "aud": credentials["token_uri"], "iat": now, "exp": now + 3600})
        key = serialization.load_pem_private_key(credentials["private_key"].replace("\\n", "\n").encode(), password=None)
        unsigned = header + b"." + payload
        signature = key.sign(unsigned, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        return (unsigned + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()

    def body(self, row: dict) -> dict:
        completed = row["status"] == "completed"
        title = "回复完成" if completed else "本轮已结束"
        test_flag = (self._env("HUAWEI_PUSH_TEST_MESSAGE", "true") or "true").lower() != "false"
        return {
            "payload": {
                "activityId": row["activity"],
                "operation": 2,
                "event": row["event"],
                "version": 2,
                "activityData": {
                    "notificationData": {
                        "type": 3,
                        "contentTitle": title,
                        "contentText": [{"text": "点击查看回复" if completed else "请打开会话查看状态"}],
                        "keepTime": 60,
                        "clickAction": {"actionType": 0},
                        "richProgress": {"progress": 100, "color": "#FFD97757", "type": 0},
                    },
                    "capsuleData": {
                        "type": 1,
                        "status": -1,
                        "icon": "phone_status.svg",
                        "title": title,
                        "bgColor": "#FF332B21",
                    },
                },
            },
            "target": {"token": [row["token"]]},
            "pushOptions": {"testMessage": test_flag, "ttl": 3600},
        }

    async def _send(self, push_type: str, body: dict, label: str) -> None:
        if self._configuration_error:
            raise ValueError(self._configuration_error)
        project = self._credentials.get("project_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project):
            raise ValueError("推送项目ID格式错误")
        # No conversation text or secrets are logged, including remote error bodies.
        async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
            response = await client.post(
                f"https://push-api.cloud.huawei.com/v3/{project}/messages:send",
                headers={"Authorization": "Bearer " + self.jwt(), "push-type": push_type},
                json=body,
            )
        try:
            code = str(response.json().get("code", ""))
        except (ValueError, AttributeError):
            code = ""
        if response.status_code != 200 or code != "80000000":
            safe_code = code if re.fullmatch(r"[0-9]{1,12}", code) else "unknown"
            raise RuntimeError(f"{label}推送未被平台接受（HTTP {response.status_code}，code {safe_code}）")

    async def finish(self, row: dict) -> None:
        await self._send("7", self.body(row), "实况窗")

    async def test_notification(self, token: str) -> None:
        """One generic, explicitly marked test notification; no conversation content."""
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise ValueError("缺少有效 Push Token，请在客户端获取并绑定")
        await self._send(
            "0",
            {
                "payload": {
                    "notification": {
                        "category": "MARKETING",
                        "title": "Tentacle 推送测试",
                        "body": "普通通知推送已到达。",
                        "clickAction": {"actionType": 0},
                        "foregroundShow": True,
                    }
                },
                "target": {"token": [token]},
                "pushOptions": {"testMessage": True, "ttl": 60},
            },
            "普通通知",
        )
