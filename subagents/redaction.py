"""Keep credentials out of coding task messages and results."""

import os
import re


_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----", re.S)
_SECRET_FIELD = re.compile(
    r'''(?im)(["']?(?:api[_-]?key|password|private_key|access_token|refresh_token|token|client_secret|secret|authorization)["']?\s*[:=]\s*)(["'])(.*?)(\2)''',
)
_ENV_SECRET = re.compile(
    r"(?m)^(\s*(?:export\s+)?(?:[A-Z0-9_]*(?:API_KEY|SECRET_ACCESS_KEY|PASSWORD|TOKEN|CLIENT_SECRET))\s*=)[^\r\n]*",
)


class Redactor:
    def __init__(self):
        self.secrets = sorted({value for key, value in os.environ.items()
                               if re.search(r"(?:KEY|TOKEN|PASSWORD|SECRET)$", key) and len(value) >= 6},
                              key=len, reverse=True)

    def text(self, text: str) -> str:
        for value in self.secrets:
            text = text.replace(value, "[认证秘密已隐藏]")
        text = _PRIVATE_KEY.sub("[私钥已隐藏]", text)
        text = _SECRET_FIELD.sub(lambda m: m[1] + m[2] + "[认证秘密已隐藏]" + m[2], text)
        return _ENV_SECRET.sub(lambda m: m[1] + "[认证秘密已隐藏]", text)

    def value(self, value):
        if isinstance(value, dict):
            return {str(key): ("[认证秘密已隐藏]" if re.fullmatch(
                r"(?i)(password|api[_-]?key|private_key|.*token|client_secret|secret|authorization)", str(key),
            ) else self.value(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, bytes):
            return "[二进制数据]"
        return value
