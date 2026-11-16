"""
统一附件管理（媒体上传策略 + 本地文档存储）

职责：
1. 接收平台层上传的图片字节，校验格式（magic bytes），保存本地副本
2. 经上传策略（FileUploader）上传远端获取 file_id/URI，供消息跨轮引用
3. 管理 files 表（chronicle.db）：local_id / 本地路径 / file_id / 过期时间
4. 惰性刷新：history 喂给模型前，对过期/远端已删/他厂签发的图片重传，
   原地更新 history 引用（local_id 不变，前端渲染不受影响）
5. Session 清理：临时 Files API 对象随 session 切换删除；GCS 稳定对象保留，
   仅在用户主动删除图片时删除（本地副本永久保留）
6. base64 供给：get_image_data_url / image_block_to_data_url 读本地副本转
   data URL，方言层经 Context 注入使用（无 file_id 通道厂商的兜底通道）

厂商无关设计（Phase 3 铺垫）：
- history 图片块规范形如 {"type": "image", "local_id": ..., "file_id": ...}，
  local_id 是厂商无关的稳定标识，file_id 只是与 files.base_url 绑定的缓存优化
  字段；换厂商（base_url 变化）后旧 file_id 视为 stale，自动从本地副本重传
- 各厂商 Files API 差异（purpose 字段、过期语义、响应形状）由 FileUploader
  实现类吸收，新增厂商只需新增策略类并在 config files.uploader 声明

设计要点：
- 单例模式（get_file_manager），DB 操作统一 asyncio.to_thread 包装
- 默认上传策略 DeepSeekFilesUploader 用 aiohttp 手写 multipart：
  expires_after[anchor] 等嵌套表单字段 openai SDK 不支持
- sha256 去重：同一图片重复上传复用记录（不新鲜则先刷新再返回）
"""

import asyncio
import os
import base64
import hashlib
import logging
import shutil
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
import requests
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import Request as GoogleAuthRequest

from attachments.inputs import IMAGE_SUFFIXES, MEDIA_SUFFIXES, generic_mime, safe_filename

logger = logging.getLogger(__name__)

from runtime.paths import state_path

DEFAULT_EXPIRES_AFTER_SECONDS = 30 * 24 * 3600  # 30 天
DEFAULT_REFRESH_MARGIN_SECONDS = 24 * 3600      # 剩余不足 24h 时惰性刷新

MAX_FILE_SIZE = 64 * 1024 * 1024  # 64 MiB（Files API 单文件上限）

_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4", "video/webm": "webm",
    "audio/mp3": "mp3", "audio/m4a": "m4a", "audio/wav": "wav",
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/flac": "flac",
    "audio/aac": "aac", "audio/aiff": "aiff",
}


def _detect_image_mime(data: bytes) -> str | None:
    """通过 magic bytes 判断图片格式（以文件实际内容为准，不看扩展名）"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class FileUploader:
    """
    远端上传策略接口

    FileManager 只依赖本接口；各家 Files API 的端点形状、鉴权、
    purpose/过期字段、响应结构差异全部由实现类吸收。
    """

    name: str = ""
    proxy: str | None = None
    requires_api_key: bool = True
    persistent: bool = False

    async def upload_path(self, path: Path, filename: str, mime: str) -> tuple[str, float]:
        raise ValueError("当前上传策略不支持音视频，请配置 GCS 或 Gemini Files")

    async def upload(self, data: bytes, filename: str, mime: str) -> tuple[str, float]:
        """
        上传图片字节

        Returns:
            (file_id, expires_at)
        """
        raise NotImplementedError

    async def delete(self, file_id: str) -> None:
        """删除远端文件（失败不抛错，由实现自行记录日志，靠远端过期兜底）"""
        raise NotImplementedError


class DeepSeekFilesUploader(FileUploader):
    """
    DeepSeek Files API 上传策略（默认）

    multipart 手写：expires_after[anchor] 等嵌套表单字段 openai SDK 不支持。
    """

    name = "deepseek_files"

    def __init__(self, base_url: str, api_key: str, expires_after: int, proxy: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.proxy = proxy
        self.expires_after = expires_after

    async def upload(self, data: bytes, filename: str, mime: str) -> tuple[str, float]:
        boundary = "----tentacle-files-" + uuid.uuid4().hex

        def _field(name: str, value: str) -> bytes:
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()

        body = b"".join([
            _field("purpose", "user_data"),
            _field("expires_after[anchor]", "created_at"),
            _field("expires_after[seconds]", str(self.expires_after)),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ])

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/files",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                data=body, proxy=self.proxy or None,
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 200:
                    raise RuntimeError(f"Files API 上传失败({resp.status}): {payload}")
                file_id = payload.get("id")
                if not file_id:
                    raise RuntimeError(f"Files API 上传响应缺少 id: {payload}")
                # 远端返回 expires_at（上传时设置了有效期）；缺失则本地推算
                expires_at = payload.get("expires_at") or (time.time() + self.expires_after)
                return file_id, float(expires_at)

    async def delete(self, file_id: str) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    f"{self.base_url}/files/{file_id}",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    proxy=self.proxy or None,
                ) as resp:
                    if resp.status != 200:
                        payload = await resp.json(content_type=None)
                        logger.warning(f"[files] 远端删除失败({resp.status}): {payload}")
                        return
            logger.info(f"[files] 远端已删除: {file_id}")
        except Exception as e:
            logger.warning(f"[files] 远端删除异常: {file_id}: {e}")


class GeminiFilesUploader(FileUploader):
    """Google resumable Files API; file_id stores the returned reusable URI."""

    name = "gemini_files"

    def __init__(self, base_url: str, api_key: str, expires_after: int, proxy: str | None = None):
        self.base_url = base_url.rstrip("/").removesuffix("/v1beta")
        self.api_key = api_key
        self.proxy = proxy

    async def upload(self, data: bytes, filename: str, mime: str) -> tuple[str, float]:
        return await self._upload(data, len(data), filename, mime)

    async def upload_path(self, path: Path, filename: str, mime: str) -> tuple[str, float]:
        with path.open("rb") as source:
            return await self._upload(source, path.stat().st_size, filename, mime)

    async def _upload(self, data, size: int, filename: str, mime: str) -> tuple[str, float]:
        if mime == "image/gif":
            raise ValueError("Gemini 不支持 GIF 图片，请转换为 PNG/JPEG/WebP")
        from datetime import datetime

        async with aiohttp.ClientSession(
            headers={"x-goog-api-key": self.api_key},
            timeout=aiohttp.ClientTimeout(total=120), trust_env=self.proxy is None,
        ) as session:
            async with session.post(
                f"{self.base_url}/upload/v1beta/files",
                headers={"X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                         "X-Goog-Upload-Header-Content-Length": str(size),
                         "X-Goog-Upload-Header-Content-Type": mime},
                json={"file": {"display_name": filename}}, proxy=self.proxy or None,
            ) as response:
                response.raise_for_status()
                upload_url = response.headers["X-Goog-Upload-URL"]
            async with session.post(
                upload_url, data=data, proxy=self.proxy or None,
                headers={"X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize",
                         "Content-Type": mime},
            ) as response:
                response.raise_for_status()
                file = (await response.json())["file"]
            # Videos may be PROCESSING after finalization. Never send them early.
            async with asyncio.timeout(120):
                while file.get("state") == "PROCESSING":
                    await asyncio.sleep(2)
                    async with session.get(
                        f"{self.base_url}/v1beta/{file['name']}", proxy=self.proxy or None,
                    ) as response:
                        response.raise_for_status()
                        file = await response.json()
                if file.get("state") not in (None, "ACTIVE"):
                    raise ValueError("Gemini 文件处理失败")
        expires = datetime.fromisoformat(file["expirationTime"].replace("Z", "+00:00")).timestamp()
        return file["uri"], expires

    async def delete(self, file_id: str) -> None:
        # Existing records may still carry IDs issued by a previous uploader.
        if not file_id.startswith(f"{self.base_url}/v1beta/files/"):
            return
        try:
            async with aiohttp.ClientSession(
                headers={"x-goog-api-key": self.api_key},
                timeout=aiohttp.ClientTimeout(total=30), trust_env=self.proxy is None,
            ) as session:
                async with session.delete(file_id, proxy=self.proxy or None) as response:
                    if response.status != 404:
                        response.raise_for_status()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("[files] Gemini 删除失败: %s", exc)


class GCSFilesUploader(FileUploader):
    """Private Google Cloud Storage objects referenced by stable ``gs://`` URIs."""

    name = "gcs"
    requires_api_key = False
    persistent = True
    _NEVER_EXPIRES = 253402300799.0  # 9999-12-31; keeps generic freshness logic simple.

    def __init__(self, base_url: str, api_key: str, expires_after: int,
                 proxy: str | None = None, *, credentials_env="", project=None):
        parsed = urlparse(base_url)
        if parsed.scheme != "gs" or not parsed.netloc:
            raise ValueError("GCS files.base_url 必须是 gs://BUCKET[/PREFIX]")
        self.base_url = base_url.rstrip("/")
        self.bucket = parsed.netloc
        self.prefix = parsed.path.strip("/")
        self.proxy = proxy
        self.credentials = None
        self.credentials_env = credentials_env
        self.project = project
        refresh_session = requests.Session()
        refresh_session.trust_env = proxy is None
        if proxy:
            refresh_session.proxies.update({"http": proxy, "https": proxy})
        self._refresh_session = refresh_session
        self._refresh_request = GoogleAuthRequest(session=refresh_session)
        self._auth_lock = asyncio.Lock()

    async def _headers(self) -> dict[str, str]:
        async with self._auth_lock:
            if self.credentials is None:
                from config.google_auth import load_credentials
                self.credentials = await asyncio.to_thread(
                    load_credentials, self.credentials_env, self.project)
            if not self.credentials.valid:
                await asyncio.to_thread(self.credentials.refresh, self._refresh_request)
            headers = {}
            self.credentials.apply(headers)
        return headers

    def _object_name(self, data: bytes, mime: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        name = f"{digest}.{_EXT_BY_MIME[mime]}"
        return f"{self.prefix}/{name}" if self.prefix else name

    async def upload(self, data: bytes, filename: str, mime: str) -> tuple[str, float]:
        object_name = self._object_name(data, mime)
        return await self._upload(data, object_name, mime)

    async def upload_path(self, path: Path, filename: str, mime: str) -> tuple[str, float]:
        def digest():
            with path.open("rb") as source:
                return hashlib.file_digest(source, "sha256").hexdigest()
        name = f"{await asyncio.to_thread(digest)}.{_EXT_BY_MIME[mime]}"
        object_name = f"{self.prefix}/{name}" if self.prefix else name
        with path.open("rb") as source:
            return await self._upload(source, object_name, mime)

    async def _upload(self, data, object_name: str, mime: str) -> tuple[str, float]:
        url = (
            f"https://storage.googleapis.com/upload/storage/v1/b/{quote(self.bucket, safe='')}/o"
            f"?uploadType=media&ifGenerationMatch=0&name={quote(object_name, safe='')}"
        )
        headers = await self._headers()
        headers["Content-Type"] = mime
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120), trust_env=self.proxy is None,
        ) as session:
            async with session.post(
                url, data=data, headers=headers, proxy=self.proxy or None,
            ) as response:
                # Content-addressed objects are immutable. A live object with the
                # same SHA is already the desired upload.
                if response.status not in (200, 201, 412):
                    detail = await response.text()
                    raise RuntimeError(f"GCS 上传失败({response.status}): {detail[:500]}")
        return f"gs://{self.bucket}/{object_name}", self._NEVER_EXPIRES

    async def delete(self, file_id: str) -> None:
        prefix = f"gs://{self.bucket}/"
        if not file_id.startswith(prefix):
            return
        object_name = file_id[len(prefix):]
        url = (
            f"https://storage.googleapis.com/storage/v1/b/{quote(self.bucket, safe='')}/o/"
            f"{quote(object_name, safe='')}"
        )
        try:
            headers = await self._headers()
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30), trust_env=self.proxy is None,
            ) as session:
                async with session.delete(
                    url, headers=headers, proxy=self.proxy or None,
                ) as response:
                    if response.status not in (204, 404):
                        detail = await response.text()
                        logger.warning("[files] GCS 删除失败(%s): %s", response.status, detail[:500])
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("[files] GCS 删除异常: %s", exc)


class FileManager:
    """
    文件管理器（单例）

    本地副本永久保留（归档历史渲染依赖）；远端文件按有效期自动过期，
    session 切换时主动清理，history 存续期内靠惰性刷新兜底。
    """

    _instance = None

    # 可用上传策略（新增厂商在此注册，config files.uploader 按名字选择）
    _UPLOADERS = {
        DeepSeekFilesUploader.name: DeepSeekFilesUploader,
        GeminiFilesUploader.name: GeminiFilesUploader,
        GCSFilesUploader.name: GCSFilesUploader,
    }

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_path: str = "data/chronicle.db"):
        if self._initialized:
            return
        self.db_path = Path(db_path)
        self._configured = False
        self._defer_uploads = False
        self._base_url = ""
        self._api_key = ""
        self._expires_after = DEFAULT_EXPIRES_AFTER_SECONDS
        self._refresh_margin = DEFAULT_REFRESH_MARGIN_SECONDS
        self._uploader: FileUploader | None = None
        self._uploaders: dict[str, FileUploader] = {}
        self._target_uploaders = {}
        self._init_db()
        self._initialized = True

    def _init_db(self):
        """files 表建在 chronicle.db（与其他业务表同库，admin 页可浏览）"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    local_id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    file_id TEXT,
                    uploaded_time REAL,
                    expires_at REAL,
                    remote_deleted INTEGER NOT NULL DEFAULT 0,
                    created_time REAL NOT NULL
                )
                """
            )

            # 旧库迁移：file_base_url 记录 file_id 的签发端点，换厂商时触发重传
            cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
            if "file_base_url" not in cols:
                conn.execute("ALTER TABLE files ADD COLUMN file_base_url TEXT")
            if 'file_connection_scope' not in cols:
                conn.execute('ALTER TABLE files ADD COLUMN file_connection_scope TEXT')
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS file_remote_refs (
                    local_id TEXT NOT NULL,
                    uploader TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    remote_id TEXT NOT NULL,
                    uploaded_time REAL NOT NULL,
                    expires_at REAL,
                    remote_deleted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (local_id, uploader, base_url),
                    FOREIGN KEY (local_id) REFERENCES files(local_id)
                )
                """
            )

            cols = {row[1] for row in conn.execute('PRAGMA table_info(file_remote_refs)')}
            if 'connection_scope' not in cols:
                conn.execute('BEGIN')
                conn.execute('ALTER TABLE file_remote_refs RENAME TO file_remote_refs_legacy')
                conn.execute('''CREATE TABLE file_remote_refs (
                    local_id TEXT NOT NULL, uploader TEXT NOT NULL, base_url TEXT NOT NULL,
                    remote_id TEXT NOT NULL, uploaded_time REAL NOT NULL, expires_at REAL,
                    remote_deleted INTEGER NOT NULL DEFAULT 0,
                    connection_scope TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (local_id, uploader, base_url, connection_scope),
                    FOREIGN KEY (local_id) REFERENCES files(local_id))''')
                conn.execute('''INSERT INTO file_remote_refs
                    (local_id,uploader,base_url,remote_id,uploaded_time,expires_at,remote_deleted)
                    SELECT local_id,uploader,base_url,remote_id,uploaded_time,expires_at,remote_deleted
                    FROM file_remote_refs_legacy''')
                conn.execute('DROP TABLE file_remote_refs_legacy')

    def configure(self, config: dict):
        """
        读取 config.yaml 的 files 段

    需要 API key 的策略在密钥缺失时处于未配置状态；GCS 使用 ADC，
    不要求 api_key_env。
        """
        import os

        config = config or {}
        self._defer_uploads = bool(config.get("defer_uploads", False))
        self._base_url = (config.get("base_url") or "https://api.deepseek.com").rstrip("/")
        api_key_env = config.get("api_key_env", "")
        self._api_key = os.getenv(api_key_env, "") if api_key_env else ""
        self._expires_after = int(config.get("expires_after_seconds", DEFAULT_EXPIRES_AFTER_SECONDS))
        self._refresh_margin = int(config.get("refresh_margin_seconds", DEFAULT_REFRESH_MARGIN_SECONDS))
        uploader_name = config.get("uploader", DeepSeekFilesUploader.name)
        self._uploaders = {}
        self._target_uploaders = {}
        default_uploader = None
        uploader_configs = {uploader_name: config} if uploader_name else {}
        uploader_configs.update(config.get("uploaders") or {})
        for name, uploader_config in uploader_configs.items():
            uploader_config = uploader_config or {}
            uploader_cls = self._UPLOADERS.get(name)
            if uploader_cls is None:
                raise ValueError(f"未知的 files.uploader: {name}（可选: {list(self._UPLOADERS)}）")
            env_name = uploader_config.get("api_key_env", "")
            api_key = os.getenv(env_name, "") if env_name else ""
            uploader = uploader_cls(
                (uploader_config.get("base_url") or self._base_url).rstrip("/"),
                api_key,
                int(uploader_config.get("expires_after_seconds", self._expires_after)),
                proxy=uploader_config.get("proxy", config.get("proxy")),
            )
            if name == 'gcs' and not self._defer_uploads:
                from config.google_auth import load_credentials
                try:
                    uploader.credentials = load_credentials()
                except DefaultCredentialsError:
                    logger.warning("[files] 缺少 Google ADC，GCS 上传不可用")
                    continue
            credentials = getattr(uploader, 'credentials', None)
            identity = [str(getattr(credentials, attr, '') or '') for attr in
                        ('service_account_email', 'quota_project_id')]
            uploader.connection_scope = self._uploader_scope(
                name, uploader.base_url, api_key, '\0'.join(identity))
            if name == uploader_name:
                default_uploader = uploader
            if api_key or not uploader.requires_api_key:
                self._uploaders[name] = uploader
        self._uploader = default_uploader
        self._configured = self._defer_uploads or uploader_name in self._uploaders
        if uploader_name and not self._configured:
            logger.warning("[files] 默认远端上传器未配置或缺少凭据")

    @staticmethod
    def _uploader_scope(name, endpoint, key, connection_id=''):
        value = '\0'.join((name, endpoint.rstrip('/'), key, connection_id))
        return hashlib.sha256(value.encode()).hexdigest()

    def _target_uploader(self, name, cfg):
        if name == 'gcs' and cfg is not None:
            endpoint = getattr(cfg, 'gcs_base_url', '')
            credentials_env = getattr(cfg, 'credentials_env', '')
            if endpoint:
                scope = self._uploader_scope(name, endpoint, '', '\0'.join(
                    (cfg.provider_id, credentials_env, cfg.project or '', cfg.proxy or '',
                     os.getenv(credentials_env, '') if credentials_env else '')))
                if scope not in self._target_uploaders:
                    uploader = GCSFilesUploader(endpoint, '', self._expires_after, proxy=cfg.proxy,
                                                credentials_env=credentials_env, project=cfg.project)
                    uploader.connection_scope = scope
                    self._target_uploaders[scope] = uploader
                return self._target_uploaders[scope]
            if credentials_env:
                # 独立身份没有 bucket 时使用本地回退，不能误用全局 bucket。
                return None
        if cfg is None or name == 'gcs':
            uploader = self._uploaders.get(name)
            if isinstance(uploader, GCSFilesUploader) and uploader.credentials is None:
                from config.google_auth import load_credentials
                uploader.credentials = load_credentials()
                identity = [str(getattr(uploader.credentials, attr, '') or '') for attr in
                            ('service_account_email', 'quota_project_id')]
                uploader.connection_scope = self._uploader_scope(
                    name, uploader.base_url, '', '\0'.join(identity))
            return uploader
        from config.providers import compatibility
        expected = {'deepseek_files': 'deepseek', 'gemini_files': 'google'}.get(name)
        if expected != compatibility(cfg.provider).compatibility or not cfg.api_key:
            return None
        scope = self._uploader_scope(name, cfg.base_url, cfg.api_key, cfg.provider_id)
        if scope not in self._target_uploaders:
            uploader = self._UPLOADERS[name](cfg.base_url, cfg.api_key, self._expires_after, proxy=cfg.proxy)
            uploader.connection_scope = scope
            self._target_uploaders[scope] = uploader
        return self._target_uploaders[scope]

    def _uploader_for_ref(self, ref):
        for uploader in [*self._uploaders.values(), *self._target_uploaders.values()]:
            if (uploader.name == ref['uploader'] and uploader.base_url == ref['base_url']
                    and getattr(uploader, 'connection_scope', '') == ref['connection_scope']):
                return uploader
        return None

    @property
    def configured(self) -> bool:
        return self._configured

    # ==================== DB 操作（to_thread 包装） ====================

    def _db_get_by_sha256(self, sha256: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM files WHERE sha256 = ? ORDER BY created_time DESC LIMIT 1", (sha256,)
            ).fetchone()
        return dict(row) if row else None

    def _db_get(self, local_id: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM files WHERE local_id = ?", (local_id,)).fetchone()
        return dict(row) if row else None

    def _db_get_by_file_id(self, file_id: str) -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                '''SELECT DISTINCT files.* FROM files
                   LEFT JOIN file_remote_refs refs ON refs.local_id = files.local_id
                   WHERE files.file_id = ? OR refs.remote_id = ?
                   LIMIT 2''', (file_id, file_id)
            ).fetchall()
        return dict(rows[0]) if len(rows) == 1 else None

    def _db_insert(self, record: dict):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO files (local_id, filename, path, mime, size, sha256,
                                   file_id, uploaded_time, expires_at, remote_deleted, created_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["local_id"], record["filename"], record["path"], record["mime"],
                    record["size"], record["sha256"], record.get("file_id"),
                    record.get("uploaded_time"), record.get("expires_at"),
                    record.get("remote_deleted", 0), record["created_time"],
                ),
            )

    def _db_update_file_id(self, local_id: str, file_id: str, uploaded_time: float, expires_at: float):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE files SET file_id = ?, uploaded_time = ?, expires_at = ?, "
                "file_base_url = ?, file_connection_scope = ?, remote_deleted = 0 WHERE local_id = ?",
                (file_id, uploaded_time, expires_at, self._base_url,
                 getattr(self._uploader, 'connection_scope', ''), local_id),
            )

    def _db_mark_remote_deleted(self, local_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE files SET remote_deleted = 1 WHERE local_id = ?", (local_id,))

    def _db_mark_remote_ref_deleted(self, local_id: str, uploader: str, base_url: str, scope=''):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE file_remote_refs SET remote_deleted = 1 "
                "WHERE local_id = ? AND uploader = ? AND base_url = ? AND connection_scope = ?",
                (local_id, uploader, base_url, scope),
            )

    def _db_upsert_remote_ref(self, local_id: str, uploader: str, base_url: str,
                              remote_id: str, uploaded_time: float, expires_at: float | None, scope=''):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO file_remote_refs
                    (local_id, uploader, base_url, remote_id, uploaded_time, expires_at, remote_deleted, connection_scope)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(local_id, uploader, base_url, connection_scope) DO UPDATE SET
                    remote_id=excluded.remote_id,
                    uploaded_time=excluded.uploaded_time,
                    expires_at=excluded.expires_at,
                    remote_deleted=0
                """,
                (local_id, uploader, base_url, remote_id, uploaded_time, expires_at, scope),
            )

    def _db_get_remote_ref(self, local_id: str, uploader: str, base_url: str, scope='') -> dict | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM file_remote_refs WHERE local_id = ? AND uploader = ? "
                "AND base_url = ? AND connection_scope = ?",
                (local_id, uploader, base_url, scope),
            ).fetchone()
        return dict(row) if row else None

    def _db_list_remote_refs(self, local_id: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM file_remote_refs WHERE local_id = ?", (local_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def _db_delete(self, local_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM file_remote_refs WHERE local_id = ?", (local_id,))
            conn.execute("DELETE FROM files WHERE local_id = ?", (local_id,))

    async def list_files(self) -> list[dict]:
        """列出全部文件记录（admin 页用）"""
        def _query():
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM files ORDER BY created_time DESC").fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_query)

    async def get_file(self, local_id: str) -> dict | None:
        return await asyncio.to_thread(self._db_get, local_id)

    # ==================== 上传 / 删除 ====================

    async def upload_file_path(self, path: Path, filename: str = "") -> dict:
        """One ingress for media and local-only documents, with dated storage."""
        from attachments.media import inspect_media
        path = Path(path)
        if path.stat().st_size > MAX_FILE_SIZE:
            raise ValueError("文件超过大小限制（64 MiB）")
        with path.open("rb") as source:
            header = source.read(32)
        if _detect_image_mime(header):
            return await self.upload_image(await asyncio.to_thread(path.read_bytes), filename)
        suffix = Path(filename or path.name).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            raise ValueError("图片后缀与文件内容不符")
        media = suffix in MEDIA_SUFFIXES
        # Preserve detection for callers that stage a media stream without a name.
        if not filename and not suffix and self._configured:
            try:
                mime = await inspect_media(path)
                media = True
            except ValueError:
                mime = generic_mime(path.name)
        elif media:
            mime = await inspect_media(path)
        else:
            mime = generic_mime(filename or path.name)
        if media and not self._defer_uploads and (not self._configured or self._uploader.name not in ("gcs", "gemini_files")):
            raise ValueError("音视频输入需要 GCS 或 Gemini Files 上传策略")
        def digest():
            with path.open("rb") as source:
                return hashlib.file_digest(source, "sha256").hexdigest()
        sha256 = await asyncio.to_thread(digest)
        existing = await asyncio.to_thread(self._db_get_by_sha256, sha256)
        if existing and existing["mime"] == mime:
            if media and self._is_stale(existing) and not await self._refresh(existing):
                raise RuntimeError("媒体重新上传失败，请重试")
            return self.public_record(await self.get_file(existing["local_id"]))
        local_id = uuid.uuid4().hex
        created = time.time()
        rel_path = self._local_path(local_id, filename, mime, created)
        state_path(rel_path).parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, path, state_path(rel_path))
        record = dict(local_id=local_id, filename=filename or Path(rel_path).name,
                      path=rel_path, mime=mime, size=path.stat().st_size, sha256=sha256,
                      created_time=created)
        await asyncio.to_thread(self._db_insert, record)
        if media:
            await self._upload_remote(record)
        return self.public_record(await self.get_file(local_id))

    @staticmethod
    def _local_path(local_id: str, filename: str, mime: str, created: float) -> str:
        name = safe_filename(filename, f"file.{_EXT_BY_MIME.get(mime, 'bin')}")
        date = datetime.fromtimestamp(created).strftime("%Y-%m-%d")
        return f"data/files/{date}/{local_id}_{name}"

    async def upload_image(self, data: bytes, filename: str = "") -> dict:
        """
        保存图片并上传远端

        Args:
            data: 图片字节（magic bytes 校验 JPEG/PNG/GIF/WebP）
            filename: 原始文件名（仅用于展示）

        Returns:
            {"local_id", "path", "mime", "filename", "file_id"}

        Raises:
            ValueError: 格式不支持 / 超大小限制 / Files API 未配置
            RuntimeError: 远端上传失败
        """
        if not self._configured:
            raise ValueError("Files API 未配置（缺少 API Key）")

        return await self.store_image(data, filename, upload=True)

    async def store_image(self, data: bytes, filename: str = "", *, upload=False) -> dict:
        """Persist through the shared file store; local reads need no remote API.

        Remote references are resolved lazily by the active model's dialect.
        """

        mime = _detect_image_mime(data)
        if mime is None:
            raise ValueError("不支持的图片格式（仅 JPEG/PNG/GIF/WebP）")
        if len(data) > MAX_FILE_SIZE:
            raise ValueError(f"图片超过大小限制（{MAX_FILE_SIZE // 1024 // 1024} MiB）")

        sha256 = hashlib.sha256(data).hexdigest()

        # 去重：同图复用记录；记录不新鲜（快过期/远端已删）先刷新
        existing = await asyncio.to_thread(self._db_get_by_sha256, sha256)
        if existing:
            if upload and self._is_stale(existing):
                await self._refresh(existing)
            return self.public_record(await asyncio.to_thread(self._db_get, existing["local_id"]))

        local_id = uuid.uuid4().hex
        ext = _EXT_BY_MIME[mime]
        safe_name = f"{local_id}.{ext}"
        created = time.time()
        rel_path = self._local_path(local_id, filename or safe_name, mime, created)
        abs_path = state_path(rel_path)

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(abs_path.write_bytes, data)

        record = {
            "local_id": local_id,
            "filename": filename or safe_name,
            "path": rel_path,
            "mime": mime,
            "size": len(data),
            "sha256": sha256,
            "file_id": None,
            "uploaded_time": None,
            "expires_at": None,
            "remote_deleted": 0,
            "created_time": created,
        }

        # 先落库（本地副本已安全），再上传远端；上传失败抛错由调用方提示
        await asyncio.to_thread(self._db_insert, record)
        if upload:
            await self._upload_remote(record)
        return self.public_record(await asyncio.to_thread(self._db_get, local_id))

    def public_record(self, record: dict | None) -> dict:
        if record is None:
            raise RuntimeError("文件记录读取失败")
        return {
            "local_id": record["local_id"],
            "filename": record["filename"],
            "path": record["path"],
            "mime": record["mime"],
            "size": record["size"],
            "file_id": record.get("file_id"),
            "expires_at": record.get("expires_at"),
            "created_time": record.get("created_time"),
        }

    def _is_stale(self, record: dict) -> bool:
        """
        记录是否需要刷新：无 file_id / 远端已删 / 签发端点不符（换厂商） /
        已过期 / 剩余不足刷新阈值

        file_base_url 记录 file_id 的签发端点；为 None（旧库迁移前）或与当前
        files.base_url 不同（换厂商）时一律视为 stale，从本地副本重传。
        """
        if self._defer_uploads:
            return False
        if not record.get("file_id"):
            return True
        if record.get("remote_deleted"):
            return True
        if ((record.get('file_connection_scope') or '') != getattr(self._uploader, 'connection_scope', '')
                or record.get("file_base_url") != self._base_url):
            return True
        expires_at = record.get("expires_at") or 0
        if not expires_at:
            return True
        return time.time() > expires_at - self._refresh_margin

    async def _upload_remote(self, record: dict):
        """经上传策略上传远端，回写 file_id / 过期时间 / 签发端点"""
        if self._defer_uploads:
            return
        abs_path = state_path(record["path"])
        if record["mime"].startswith(("audio/", "video/")):
            file_id, expires_at = await self._uploader.upload_path(abs_path, record["filename"], record["mime"])
        else:
            data = await asyncio.to_thread(abs_path.read_bytes)
            file_id, expires_at = await self._uploader.upload(data, record["filename"], record["mime"])
        await asyncio.to_thread(
            self._db_update_file_id, record["local_id"], file_id, time.time(), expires_at
        )
        await asyncio.to_thread(
            self._db_upsert_remote_ref, record["local_id"], self._uploader.name,
            getattr(self._uploader, "base_url", self._base_url),
            file_id, time.time(), expires_at, getattr(self._uploader, 'connection_scope', ''),
        )
        logger.info(f"[files] 上传成功: {record['filename']} -> {file_id}")

    async def resolve_remote_file_id(
        self, block: dict, uploader_names: tuple[str, ...], *, model_config=None
    ) -> str | None:
        """取得目标方言可用的远端引用；缺失时从本地副本惰性上传。"""
        record = await self._resolve_block_record(block)
        if record is None:
            return None
        for name in uploader_names:
            uploader = self._target_uploader(name, model_config)
            if uploader is None:
                continue
            ref = await asyncio.to_thread(
                self._db_get_remote_ref, record["local_id"], name, uploader.base_url,
                getattr(uploader, 'connection_scope', ''),
            )
            now = time.time()
            if (ref and not ref.get("remote_deleted")
                    and now <= (ref.get("expires_at") or 0) - self._refresh_margin):
                return ref["remote_id"]
            try:
                path = Path(record["path"])
                if not path.is_absolute():
                    path = state_path(path)
                if record["mime"].startswith(("audio/", "video/")):
                    remote_id, expires_at = await uploader.upload_path(path, record["filename"], record["mime"])
                else:
                    data = await asyncio.to_thread(path.read_bytes)
                    remote_id, expires_at = await uploader.upload(data, record["filename"], record["mime"])
                await asyncio.to_thread(
                    self._db_upsert_remote_ref, record["local_id"], name,
                    uploader.base_url, remote_id, now, expires_at, getattr(uploader, 'connection_scope', ''),
                )
                return remote_id
            except Exception as exc:
                logger.warning("[files] %s 惰性上传失败，回退 base64: %s", name, exc)
        return None

    async def _refresh(self, record: dict) -> str | None:
        """
        用本地副本重传远端，更新 file_id / 过期时间

        Returns:
            新 file_id，失败返回 None
        """
        try:
            await self._upload_remote(record)
            return (await asyncio.to_thread(self._db_get, record["local_id"])).get("file_id")
        except Exception as e:
            logger.error(f"[files] 刷新失败 {record['local_id']}: {e}")
            return None

    async def delete_file(self, local_id: str) -> bool:
        """
        删除文件：远端 DELETE + 本地文件 + DB 记录（三删）

        Returns:
            记录存在返回 True
        """
        record = await asyncio.to_thread(self._db_get, local_id)
        if record is None:
            return False

        deleted_ids: set[str] = set()
        for ref in await asyncio.to_thread(self._db_list_remote_refs, local_id):
            uploader = self._uploader_for_ref(ref)
            if uploader is None or ref.get("remote_deleted"):
                continue
            await uploader.delete(ref["remote_id"])
            deleted_ids.add(ref["remote_id"])

        file_id = record.get("file_id")
        if (file_id and file_id not in deleted_ids and not record.get("remote_deleted")
                and self._uploader is not None
                and (record.get('file_connection_scope') or '') == getattr(self._uploader, 'connection_scope', '')
                and record.get('file_base_url') == self._base_url):
            await self._uploader.delete(file_id)

        try:
            state_path(record["path"]).unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"[files] 本地文件删除失败: {record['path']}: {e}")

        await asyncio.to_thread(self._db_delete, local_id)
        logger.info(f"[files] 已删除: {local_id}")
        return True

    # ==================== base64 供给（方言层经 Context 注入） ====================

    async def get_image_data_url(self, local_id: str) -> str | None:
        """
        读取本地副本转 base64 data URL（base64 通道供给）

        Returns:
            data URL 字符串；记录不存在或本地文件丢失返回 None
        """
        record = await asyncio.to_thread(self._db_get, local_id)
        if record is None:
            return None
        path = state_path(record["path"])
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as e:
            logger.warning(f"[files] 本地图片读取失败 {record['path']}: {e}")
            return None
        return f"data:{record['mime']};base64,{base64.b64encode(data).decode('ascii')}"

    async def image_block_to_data_url(self, block: dict) -> str | None:
        """
        图片块 -> base64 data URL（base64 通道统一入口，方言层经 Context 使用）

        兼容旧格式块：local_id 缺失时按 file_id 反查。
        """
        local_id = block.get("local_id")
        if not local_id:
            record = await self._resolve_block_record(block)
            local_id = record["local_id"] if record else None
        if not local_id:
            return None
        return await self.get_image_data_url(local_id)

    # ==================== history 引用维护 ====================

    async def ensure_fresh_file_ids(self, history: list[dict]) -> None:
        """
        惰性刷新：扫描 history 中的图片块，对失效引用重传并原地替换 file_id

        local_id 驱动（厂商无关）：file_id 只是与 files.base_url 绑定的缓存
        字段，过期/远端已删/他厂签发时一律从本地副本重传；旧格式块（仅
        file_id）按 DB 反查补齐 local_id。local_id 不变（前端渲染与归档
        引用解耦于 file_id）。
        """
        if not self._configured:
            return
        if self._defer_uploads or getattr(self._uploader, "persistent", False):
            return

        blocks = self._iter_image_blocks(history)
        if not blocks:
            return

        resolved: list[tuple[dict, dict | None]] = []
        for block in blocks:
            resolved.append((block, await self._resolve_block_record(block)))

        # 同一记录只刷新一次（多块引用同一图片）
        fresh_file_ids: dict[str, str | None] = {}
        for _, record in resolved:
            if record is None or record["local_id"] in fresh_file_ids:
                continue
            if self._is_stale(record):
                fresh_file_ids[record["local_id"]] = await self._refresh(record)
            else:
                fresh_file_ids[record["local_id"]] = record["file_id"]

        # 原地回写：补 local_id + 同步新 file_id（刷新失败保留旧值）
        for block, record in resolved:
            if record is None:
                continue
            block["local_id"] = record["local_id"]
            file_id = fresh_file_ids.get(record["local_id"])
            if file_id:
                block["file_id"] = file_id

    async def cleanup_session_files(self, history: list[dict]) -> None:
        """
        Session 切换清理：DELETE 临时 Files API 对象；稳定 GCS 对象保留

        本地副本与 DB 记录保留（归档渲染依赖；若新 session 因去重复用同一
        记录，ensure_fresh_file_ids 会检测 remote_deleted 并重传，闭环安全）。
        """
        if not self._configured:
            return

        blocks = self._iter_image_blocks(history)
        if not blocks:
            return

        seen: set[str] = set()
        count = 0
        for block in blocks:
            record = await self._resolve_block_record(block)
            if record is None or record["local_id"] in seen:
                continue
            seen.add(record["local_id"])
            cleaned = False
            default_cleaned = False
            for uploader in [*self._uploaders.values(), *self._target_uploaders.values()]:
                name = uploader.name
                if uploader.persistent:
                    continue
                ref = await asyncio.to_thread(
                    self._db_get_remote_ref, record["local_id"], name, uploader.base_url,
                    getattr(uploader, 'connection_scope', ''),
                )
                if ref is None or ref.get("remote_deleted"):
                    continue
                await uploader.delete(ref["remote_id"])
                await asyncio.to_thread(
                    self._db_mark_remote_ref_deleted,
                    record["local_id"], name, uploader.base_url, getattr(uploader, 'connection_scope', ''),
                )
                cleaned = True
                if (uploader is self._uploader
                        and ref["remote_id"] == record.get("file_id")):
                    await asyncio.to_thread(self._db_mark_remote_deleted, record["local_id"])
                    default_cleaned = True

            # 兼容尚未迁移进 file_remote_refs 的旧默认引用。
            if (not default_cleaned and not getattr(self._uploader, "persistent", False)
                    and self._uploader is not None
                    and (record.get('file_connection_scope') or '') == getattr(self._uploader, 'connection_scope', '')
                    and record.get('file_base_url') == self._base_url
                    and not record.get("remote_deleted") and record.get("file_id")):
                await self._uploader.delete(record["file_id"])
                await asyncio.to_thread(self._db_mark_remote_deleted, record["local_id"])
                cleaned = True
            if cleaned:
                count += 1

        if count:
            logger.info(f"[files] session 清理: {count} 个远端文件")

    @staticmethod
    def _iter_image_blocks(history: list[dict]) -> list[dict]:
        """收集 history 中所有图片/音视频块（沿用旧方法名以兼容调用）"""
        blocks: list[dict] = []
        for msg in history:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image", "input_image", "audio", "video"):
                    blocks.append(block)
        return blocks

    async def _resolve_block_record(self, block: dict) -> dict | None:
        """图片块 -> DB 记录（local_id 优先，旧格式块按 file_id 反查）"""
        local_id = block.get("local_id")
        if local_id:
            record = await asyncio.to_thread(self._db_get, local_id)
            if record is not None:
                return record
        file_id = block.get("file_id")
        if file_id:
            return await asyncio.to_thread(self._db_get_by_file_id, file_id)
        return None


# 全局单例访问入口
def get_file_manager() -> FileManager:
    return FileManager()
