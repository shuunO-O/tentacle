"""Shared attachment names, extension classification and bounded text expansion."""

import mimetypes
from pathlib import Path
import re

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MEDIA_SUFFIXES = {".mp4", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".aiff"}
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log",
    ".py", ".js", ".ts", ".html", ".css", ".xml", ".toml", ".ini", ".cfg", ".sh", ".sql",
    ".rst", ".adoc", ".tex", ".bib", ".jsonl", ".ndjson", ".jsonc", ".ipynb",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".java", ".kt", ".kts", ".go", ".rs",
    ".rb", ".php", ".swift", ".m", ".mm", ".r", ".lua", ".pl", ".pm", ".ex", ".exs",
    ".jsx", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".scss", ".sass", ".less",
    ".ps1", ".psm1", ".bat", ".cmd", ".bash", ".zsh", ".fish", ".dockerfile",
    ".conf", ".properties", ".gradle", ".cmake", ".make", ".mk", ".proto", ".graphql",
    ".htm", ".xhtml", ".srt", ".vtt", ".ass", ".ics", ".vcf", ".diff", ".patch",
}
AUTO_TEXT_BYTES = 64 * 1024
AUTO_TEXT_CHARS = 32000
AUTO_ROUND_CHARS = 64000


def safe_filename(value: str, fallback="file") -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f\[\]]", "_", name).strip(" .")
    if not name:
        return fallback
    suffix = Path(name).suffix[:16]
    stem = name[:-len(suffix)] if suffix else name
    return stem.encode("utf-8")[:120].decode("utf-8", errors="ignore") + suffix.encode("utf-8")[:32].decode("utf-8", errors="ignore")


def generic_mime(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return {".json": "application/json", ".xml": "application/xml"}.get(suffix, "text/plain")
    # Unknown extensions remain ordinary local files, even when mimetypes guesses
    # an unsupported media type (e.g. SVG or AVI).
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return "application/octet-stream" if mime.startswith(("image/", "audio/", "video/")) else mime


def attachment_kind(record: dict) -> str:
    mime = record.get("mime") or "image/jpeg"  # legacy image records
    kind = mime.split("/", 1)[0]
    return kind if kind in {"image", "audio", "video"} else "file"


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        text = data.decode("utf-32")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030")
    if "\x00" in text:
        raise ValueError("文件不是可读取文本")
    return text


def attachment_context(record: dict, root: Path, remaining: int) -> tuple[str, int]:
    """Only a relative [file:path] and optionally the entire small text body.

    This snapshot becomes normal user-message text and stays in session.history;
    later requests reuse that history, including after a process restart.
    """
    path = Path(record["path"])
    if path.is_absolute():
        path = path.relative_to(root)
    if ".." in path.parts or "]" in str(path) or any(ord(c) < 32 for c in str(path)):
        raise ValueError("附件路径无效")
    marker = f"[file:{path.as_posix()}]"
    if Path(record.get("filename") or path.name).suffix.lower() not in TEXT_SUFFIXES:
        return marker, remaining
    target = (root / path).resolve()
    if not target.is_relative_to((root / "data/files").resolve()):
        raise ValueError("附件路径不在文件存储目录内")
    with target.open("rb") as source:
        data = source.read(AUTO_TEXT_BYTES + 1)
    if len(data) > AUTO_TEXT_BYTES:
        return marker, remaining
    try:
        text = decode_text(data)
    except (UnicodeError, ValueError):
        return marker, remaining
    if len(text) > min(AUTO_TEXT_CHARS, remaining):
        return marker, remaining
    return marker + "\n" + text, remaining - len(text)
