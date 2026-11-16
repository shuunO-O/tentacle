"""Outgoing [file:path] markers and shared local-file access policy.

No uploads, database writes or file copies. Relative paths use TENTACLE_HOME.
"""

import fnmatch
import os
from pathlib import Path
import re
import stat

from runtime.paths import state_path

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_INLINE = re.compile(r"(?<!`)(`+)(?!`)[^\r\n]*?(?<!`)\1(?!`)|(?<!\\)\[file:([^\]\r\n]+)\]")
_SECRET_DIRS = {".ssh", ".aws", ".azure", ".kube", ".git", "gcloud", "credentials", "secrets", "backup_staging"}
_SECRET_NAMES = (
    ".env*", ".netrc*", ".git-credentials*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*credential*.json*",
    "*service-account*.json*", "*service_account*.json*", "google-sa.json*", "github-token*",
    "webui_settings.json*", "device_enroll_pending.json*", "device_auth_freeze.json*",
)


def split_file_markers(text: str) -> list[tuple[str, str]]:
    """Return ordered text/file parts; fenced/inline code stays literal.

    Parse the complete message before platform text segmentation. Unclosed markers
    remain text so streaming callers can safely rescan their accumulated buffer.
    """
    parts = []
    offset = last = 0
    fence = ""
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line.rstrip("\r\n"))
        if match:
            marker, rest = match.groups()
            if not fence:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not rest.strip():
                fence = ""
        elif not fence:
            for item in _INLINE.finditer(line):
                path = item[2]
                if path is None or not path.strip():
                    continue
                start, end = offset + item.start(), offset + item.end()
                if start > last:
                    parts.append(("text", text[last:start]))
                parts.append(("file", path.strip()))
                last = end
        offset += len(line)
    if last < len(text):
        parts.append(("text", text[last:]))
    return parts


def check_file_access(path: Path, protected_paths=()):
    parts = [part.lower() for part in path.parts]
    if (set(parts) & _SECRET_DIRS
            or any(fnmatch.fnmatch(part, pattern) for part in parts for pattern in _SECRET_NAMES)
            or path.is_relative_to("/proc") or path.is_relative_to("/sys") or path.is_relative_to("/dev")
            or ("gh" in parts and path.name.lower() == "hosts.yml")):
        raise PermissionError("认证材料或系统特殊路径禁止发送")
    protected = [*protected_paths]
    from config.google_auth import credential_paths
    protected.extend(credential_paths())
    if credential := os.environ.get("HUAWEI_PUSH_CREDENTIALS_FILE", "").strip():
        credential_path = Path(credential).expanduser()
        protected.append(state_path(credential_path))
    if any(path == Path(item).expanduser().resolve() for item in protected):
        raise PermissionError("认证材料禁止发送")


def open_outgoing_file(value: str, *, protected_paths=()):
    """Return (resolved path, open binary stream, stat) for an allowed regular file.

    Check both the requested and resolved paths. Open resolved components without
    following symlinks, so replacement after validation cannot redirect the read.
    """
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("文件路径无效")
    requested = Path(value.strip()).expanduser()
    if not requested.is_absolute():
        requested = state_path(requested)
    check_file_access(requested, protected_paths)
    target = requested.resolve(strict=True)
    check_file_access(target, protected_paths)
    if os.name == "nt":
        from attachments.windows_files import open_regular_file
        stream = open_regular_file(target)
        return target, stream, os.fstat(stream.fileno())
    directory = os.open(target.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in target.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise PermissionError("仅支持普通文件，不支持目录、特殊文件或硬链接")
        return target, os.fdopen(fd, "rb"), metadata
    except BaseException:
        os.close(fd)
        raise
