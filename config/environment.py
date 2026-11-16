"""Write application credentials to the local dotenv file without exporting values."""

from hashlib import sha256
from io import StringIO
from pathlib import Path

from dotenv.parser import parse_stream

from config.providers import ConfigurationError


WEB_ENV_PREFIX = "TENTACLE_WEB_CONFIG_"
MAX_DOTENV_BYTES = 1024 * 1024
updated_env_keys: set[str] = set()


def environment_name(path: str) -> str:
    return WEB_ENV_PREFIX + sha256(path.encode("utf-8")).hexdigest().upper()


def dotenv_update(path: Path, name: str, value: str) -> bytes:
    if (not isinstance(value, str) or not value.strip() or len(value) > 8192
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
            or "${" in value):
        raise ConfigurationError("请输入有效的单行凭据值")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigurationError(".env 必须是普通文件")
    try:
        with path.open("rb") as stream:
            original = stream.read(MAX_DOTENV_BYTES + 1)
        if len(original) > MAX_DOTENV_BYTES:
            raise ConfigurationError(".env 超过 1 MiB，请在主机整理后再保存")
        text = original.decode("utf-8")
    except FileNotFoundError:
        text = ""
    except UnicodeDecodeError:
        raise ConfigurationError(".env 必须使用 UTF-8 编码") from None
    lines = []
    for entry in parse_stream(StringIO(text)):
        if entry.error:
            raise ConfigurationError(".env 包含无效语句，请在主机整理后再保存")
        if entry.key != name:
            lines.append(entry.original.string)
    result = "".join(lines)
    if result and not result.endswith("\n"):
        result += "\n"
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    updated = (result + f"{name}='{escaped}'\n").encode("utf-8")
    if len(updated) > MAX_DOTENV_BYTES:
        raise ConfigurationError("保存后 .env 将超过 1 MiB，请在主机整理后再保存")
    return updated
