"""插件自定义沙箱 WebUI 文档装配与静态资源安全解析。

将插件 `ui/` 目录内的 HTML 入口及其引用的本地 CSS/JS/图片安全内联为自包含沙箱文档，
使宿主前端可以通过受 `DeviceAccessGate` 与 `auth_dep` 保护的 API 获取并渲染到
`<iframe sandbox="allow-scripts ...">`，避免暴露主站同源凭据或绕过设备门禁。
"""

from __future__ import annotations

import base64
import html
import mimetypes
from pathlib import Path
import re
from urllib.parse import urlsplit

from plugins.manifest import resolve_plugin_subpath
from plugins.permissions import PluginManifestError


MAX_UI_FILE_BYTES = 4 * 1024 * 1024
MAX_UI_BUNDLE_BYTES = 10 * 1024 * 1024

SANDBOX_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline' blob: data:; "
    "style-src 'unsafe-inline' data:; "
    "img-src data: blob:; "
    "font-src data: blob:; "
    "media-src data: blob:; "
    "worker-src blob:; "
    "connect-src 'none'; "
    "frame-src 'none'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none';"
)

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_LINK_TAG_RE = re.compile(r"<link\b(?P<attrs>[^>]*?)/?>", re.IGNORECASE | re.DOTALL)
_SCRIPT_TAG_RE = re.compile(
    r"<script\b(?P<attrs>[^>]*?)>(?P<body>.*?)</script\s*>",
    re.IGNORECASE | re.DOTALL,
)
_IMG_TAG_RE = re.compile(r"<img\b(?P<attrs>[^>]*?)/?>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(
    r"""(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*(?:=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<uq>[^\s"'=<>`]+)))?""",
    re.DOTALL,
)
_CSS_URL_RE = re.compile(r"""url\(\s*(?P<quote>['"]?)(?P<url>[^)'"]+?)(?P=quote)\s*\)""", re.IGNORECASE)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HTML_OPEN_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_CLOSING_SCRIPT_RE = re.compile(r"</script", re.IGNORECASE)
_CLOSING_STYLE_RE = re.compile(r"</style", re.IGNORECASE)


def _is_local_ref(ref: str | None) -> bool:
    if not ref:
        return False
    cleaned = ref.strip()
    if not cleaned or cleaned.startswith(("#", "//")):
        return False
    return not bool(_SCHEME_RE.match(cleaned))


def _parse_attrs(attr_text: str) -> dict[str, str | None]:
    attrs: dict[str, str | None] = {}
    for m in _ATTR_RE.finditer(attr_text):
        name = m.group("name").lower()
        val = m.group("dq")
        if val is None:
            val = m.group("sq")
        if val is None:
            val = m.group("uq")
        attrs[name] = html.unescape(val) if val is not None else None
    return attrs


def _resolve_relative_ui_asset(ui_dir: Path, base_dir: Path, raw_ref: str) -> Path:
    parts = urlsplit(raw_ref.strip())
    raw_path = parts.path.strip()
    if not raw_path or "\x00" in raw_path:
        raise PluginManifestError(f"无效的插件 UI 资源路径: {raw_ref!r}")
    root_resolved = ui_dir.resolve()
    normalized = raw_path.replace("\\", "/")
    if normalized.startswith("/"):
        candidate = (root_resolved / normalized.lstrip("/")).resolve()
    else:
        candidate = (base_dir.resolve() / normalized).resolve()
    if not candidate.is_relative_to(root_resolved) or candidate == root_resolved:
        raise PluginManifestError(f"插件 UI 资源路径越界: {raw_ref!r}")
    if not candidate.is_file():
        raise PluginManifestError(f"插件 UI 资源不存在: {raw_ref!r}")
    return candidate


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    overrides = {
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".webp": "image/webp",
    }
    if suffix in overrides:
        return overrides[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _read_bounded_bytes(path: Path, budget: list[int]) -> bytes:
    size = path.stat().st_size
    if size > MAX_UI_FILE_BYTES:
        raise PluginManifestError(f"插件 UI 资源超过单文件上限 ({MAX_UI_FILE_BYTES} bytes): {path.name}")
    if budget[0] + size > MAX_UI_BUNDLE_BYTES:
        raise PluginManifestError(f"插件 UI 装配总大小超过上限 ({MAX_UI_BUNDLE_BYTES} bytes)")
    data = path.read_bytes()
    budget[0] += len(data)
    return data


def _to_data_uri(path: Path, budget: list[int]) -> str:
    data = _read_bounded_bytes(path, budget)
    mime = _guess_mime(path)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _inline_css_urls(css_text: str, ui_dir: Path, css_dir: Path, budget: list[int]) -> str:
    def _replace(match: re.Match[str]) -> str:
        raw_url = match.group("url").strip()
        if not _is_local_ref(raw_url):
            return match.group(0)
        asset_path = _resolve_relative_ui_asset(ui_dir, css_dir, raw_url)
        data_uri = _to_data_uri(asset_path, budget)
        return f'url("{data_uri}")'

    return _CSS_URL_RE.sub(_replace, css_text)


def _inject_head_prelude(doc_html: str, prelude: str) -> str:
    head_match = _HEAD_OPEN_RE.search(doc_html)
    if head_match:
        idx = head_match.end()
        return doc_html[:idx] + "\n" + prelude + "\n" + doc_html[idx:]
    html_match = _HTML_OPEN_RE.search(doc_html)
    if html_match:
        idx = html_match.end()
        return doc_html[:idx] + "\n<head>\n" + prelude + "\n</head>\n" + doc_html[idx:]
    return f"<!DOCTYPE html>\n<html>\n<head>\n{prelude}\n</head>\n<body>\n{doc_html}\n</body>\n</html>"


def resolve_ui_asset_file(ui_dir: Path, rel_path: str) -> Path:
    """校验并返回插件 UI 目录内的单个静态资源文件路径。"""
    target = resolve_plugin_subpath(ui_dir, rel_path, allow_dir=False, allow_file=True)
    if target.stat().st_size > MAX_UI_FILE_BYTES:
        raise PluginManifestError(f"插件 UI 资源超过单文件上限 ({MAX_UI_FILE_BYTES} bytes): {rel_path}")
    return target


def build_plugin_ui_document(ui_dir: Path, entry: str) -> str:
    """读取并装配插件沙箱 HTML 文档（内联本地 CSS/JS/图片并注入严格 CSP）。"""
    entry_path = resolve_plugin_subpath(ui_dir, entry, allow_dir=False, allow_file=True)
    budget = [0]
    raw_bytes = _read_bounded_bytes(entry_path, budget)
    doc = raw_bytes.decode("utf-8")
    entry_dir = entry_path.parent

    def _replace_link(match: re.Match[str]) -> str:
        attrs = _parse_attrs(match.group("attrs"))
        rel = (attrs.get("rel") or "").lower().split()
        href = attrs.get("href")
        if "stylesheet" in rel and _is_local_ref(href):
            assert href is not None
            css_path = _resolve_relative_ui_asset(ui_dir, entry_dir, href)
            css_text = _read_bounded_bytes(css_path, budget).decode("utf-8")
            css_text = _inline_css_urls(css_text, ui_dir, css_path.parent, budget)
            safe_css = _CLOSING_STYLE_RE.sub(r"<\/style", css_text)
            return f'<style data-plugin-asset="{html.escape(href, quote=True)}">\n{safe_css}\n</style>'
        return match.group(0)

    def _replace_script(match: re.Match[str]) -> str:
        attrs = _parse_attrs(match.group("attrs"))
        src = attrs.get("src")
        if not _is_local_ref(src):
            return match.group(0)
        assert src is not None
        js_path = _resolve_relative_ui_asset(ui_dir, entry_dir, src)
        js_text = _read_bounded_bytes(js_path, budget).decode("utf-8")
        safe_js = _CLOSING_SCRIPT_RE.sub(r"<\/script", js_text)
        script_type = attrs.get("type")
        type_attr = f' type="{html.escape(script_type, quote=True)}"' if script_type else ""
        return f'<script{type_attr} data-plugin-asset="{html.escape(src, quote=True)}">\n{safe_js}\n</script>'

    def _replace_img(match: re.Match[str]) -> str:
        full_tag = match.group(0)
        attrs_raw = match.group("attrs")
        attrs = _parse_attrs(attrs_raw)
        src = attrs.get("src")
        if not _is_local_ref(src):
            return full_tag
        assert src is not None
        img_path = _resolve_relative_ui_asset(ui_dir, entry_dir, src)
        data_uri = _to_data_uri(img_path, budget)
        replaced_attrs = re.sub(
            r"""(\bsrc\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+)""",
            lambda m: f'{m.group(1)}"{data_uri}"',
            attrs_raw,
            count=1,
            flags=re.IGNORECASE,
        )
        closing = "/>" if full_tag.rstrip().endswith("/>") else ">"
        return f"<img{replaced_attrs}{closing}"

    doc = _LINK_TAG_RE.sub(_replace_link, doc)
    doc = _SCRIPT_TAG_RE.sub(_replace_script, doc)
    doc = _IMG_TAG_RE.sub(_replace_img, doc)

    csp_meta = f'<meta http-equiv="Content-Security-Policy" content="{SANDBOX_CSP}">'
    return _inject_head_prelude(doc, csp_meta)
