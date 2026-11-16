"""Configurable HTTP media jobs client. No application state, models or local fallback."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid

import aiohttp

logger = logging.getLogger(__name__)
MAX_BYTES = 64 * 1024 * 1024
MAX_JSON = 64 * 1024
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_ERRORS = {
    "worker_busy",
    "dependencies_missing",
    "insufficient_space",
    "idempotency_conflict",
    "not_ready",
    "job_not_found",
    "job_expired",
    "upstream_rejected",
    "format_unavailable",
    "media_too_large",
    "media_too_long",
    "download_timeout",
    "download_failed",
    "worker_restarted",
}


class MediaDownloadError(ValueError):
    """Only controlled messages may enter notices, model input or application logs."""


@dataclass(frozen=True)
class DownloadSettings:
    backend: str = "local"
    base_url: str = ""
    profile: str = "bilibili-480p-v1"
    timeout_seconds: float = 360
    proxy: str = ""
    api_key_env: str = ""


def _endpoint(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 for c in value):
        raise ValueError("媒体下载地址须为不含认证信息的 HTTP(S) 地址")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.port != 0
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("媒体下载地址须为不含认证信息的 HTTP(S) 地址")
    return value.rstrip("/")


def download_settings(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(DownloadSettings.__dataclass_fields__):
        raise ValueError("media_inputs.download 包含不支持的配置字段")
    settings = DownloadSettings(**value)
    if settings.backend not in ("local", "http_jobs"):
        raise ValueError("媒体下载后端须为 local 或 http_jobs")
    if not isinstance(settings.base_url, str):
        raise ValueError("媒体下载 base_url 须为 HTTP(S) 地址或空字符串")
    if settings.base_url:
        _endpoint(settings.base_url)
    if settings.backend == "http_jobs" and not settings.base_url:
        raise ValueError("http_jobs 媒体下载后端需要 base_url")
    if not isinstance(settings.profile, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", settings.profile):
        raise ValueError("媒体下载 profile 须为 1 到 128 个字母、数字或 ._-")
    if (
        type(settings.timeout_seconds) not in (int, float)
        or not math.isfinite(settings.timeout_seconds)
        or not 1 <= settings.timeout_seconds <= 360
    ):
        raise ValueError("媒体下载总预算须为 1 到 360 秒")
    if not isinstance(settings.proxy, str):
        raise ValueError("媒体下载 proxy 须为 HTTP(S) 地址或空字符串")
    if settings.proxy:
        _endpoint(settings.proxy)
    if not isinstance(settings.api_key_env, str) or (
        settings.api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", settings.api_key_env)
    ):
        raise ValueError("媒体下载 api_key_env 须为环境变量名")
    return settings


def _failure(status, body=None):
    code = body.get("error") if isinstance(body, dict) else None
    if not isinstance(code, str) or code not in _ERRORS:
        code = {
            429: "worker_busy",
            503: "worker_unavailable",
            507: "insufficient_space",
            404: "job_not_found",
            410: "job_expired",
            409: "job_conflict",
        }.get(status, "protocol_error")
    upstream = body.get("upstream_status") if isinstance(body, dict) else None
    suffix = f"，上游 HTTP {upstream}" if type(upstream) is int and 100 <= upstream <= 599 else ""
    return MediaDownloadError(f"远程视频下载失败（{code}{suffix}）")


def _job(value, expected=None):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("job_id"), str)
        or not _JOB_ID.fullmatch(value["job_id"])
        or (expected and value["job_id"] != expected)
        or value.get("state") not in ("downloading", "ready", "failed", "cancelled")
    ):
        raise MediaDownloadError("远程视频任务响应无效")
    return value["job_id"]


def _ready(value):
    media, source = value.get("media"), value.get("source")
    if not isinstance(media, dict) or not isinstance(source, dict):
        raise MediaDownloadError("远程视频缺少媒体或来源信息")
    duration = media.get("duration_seconds")
    if (
        media.get("mime") != "video/mp4"
        or type(media.get("size_bytes")) is not int
        or not 0 < media["size_bytes"] <= MAX_BYTES
        or type(duration) not in (int, float)
        or not math.isfinite(duration)
        or not 0 < duration <= 600
        or type(media.get("height")) is not int
        or not 0 < media["height"] <= 480
        or type(media.get("width")) is not int
        or not 0 < media["width"] <= 16384
        or not isinstance(media.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", media["sha256"])
        or not isinstance(media.get("filename"), str)
        or not 0 < len(media["filename"]) <= 255
        or not isinstance(source.get("provider"), str)
        or not source["provider"]
        or not isinstance(source.get("video_id"), str)
        or not source["video_id"]
        or type(source.get("page")) is not int
        or source["page"] < 1
    ):
        raise MediaDownloadError("远程视频元数据无效或超过大小、时长、分辨率限制")
    try:
        expiry = datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
        if expiry.utcoffset() != timezone.utc.utcoffset(None) or expiry <= datetime.now(timezone.utc):
            raise ValueError()
    except (KeyError, TypeError, AttributeError, ValueError):
        raise MediaDownloadError("远程视频已过期或有效期无效") from None
    return media


class HttpJobsDownloader:
    """Protocol 1 job service; one job, bounded polling and at most two full transfers."""

    def __init__(self, settings):
        self.settings = settings
        self.base = _endpoint(settings.base_url)

    async def _request(self, session, method, path, *, expected=(200,), **kwargs):
        timeout = aiohttp.ClientTimeout(total=10, connect=5, sock_connect=5)
        async with session.request(
            method,
            self.base + path,
            timeout=timeout,
            allow_redirects=False,
            proxy=self.settings.proxy or None,
            **kwargs,
        ) as response:
            data = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                data.extend(chunk)
                if len(data) > MAX_JSON:
                    raise MediaDownloadError("远程视频控制响应超过大小上限")
            try:
                value = json.loads(data) if data else None
            except (ValueError, UnicodeError):
                raise MediaDownloadError("远程视频控制响应不是有效 JSON") from None
            if response.status not in expected:
                raise _failure(response.status, value)
            return value

    async def _transfer(self, session, job_id, media, destination):
        partial = destination.with_suffix(".part")
        timeout = aiohttp.ClientTimeout(total=160, connect=5, sock_connect=5, sock_read=10)
        try:
            for attempt in range(2):
                try:
                    async with session.get(
                        self.base + f"/jobs/{job_id}/content",
                        timeout=timeout,
                        allow_redirects=False,
                        proxy=self.settings.proxy or None,
                    ) as response:
                        if response.status != 200:
                            raise _failure(response.status)
                        length = response.headers.get("Content-Length", "")
                        if (
                            not re.fullmatch(r"[0-9]{1,8}", length)
                            or int(length) != media["size_bytes"]
                            or response.headers.get("Content-Encoding", "identity").lower() != "identity"
                        ):
                            raise MediaDownloadError("远程视频 Content-Length 或编码与清单不符")
                        size, digest = 0, hashlib.sha256()
                        with partial.open("wb") as target:
                            async for chunk in response.content.iter_chunked(256 * 1024):
                                size += len(chunk)
                                if size > media["size_bytes"] or size > MAX_BYTES:
                                    raise MediaDownloadError("远程视频传输超过大小上限")
                                digest.update(chunk)
                                await asyncio.to_thread(target.write, chunk)
                        if size != media["size_bytes"]:
                            raise aiohttp.ClientPayloadError("truncated content")
                        if digest.hexdigest() != media["sha256"]:
                            raise MediaDownloadError("远程视频 SHA256 校验不符")
                    partial.replace(destination)
                    return
                except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError):
                    if attempt:
                        raise MediaDownloadError("远程视频传输中断，同一任务重取仍失败") from None
        finally:
            partial.unlink(missing_ok=True)

    @asynccontextmanager
    async def download(self, url, destination):
        destination = Path(destination)
        body = json.dumps({"url": url, "profile": self.settings.profile}).encode()
        if len(body) > 8192:
            raise MediaDownloadError("视频下载请求超过 8 KiB")
        headers = {"Accept-Encoding": "identity"}
        if self.settings.api_key_env:
            token = os.getenv(self.settings.api_key_env, "")
            if not token or len(token) > 8192 or "\r" in token or "\n" in token:
                raise MediaDownloadError("远程视频认证环境变量未配置或格式无效")
            headers["Authorization"] = "Bearer " + token
        job_id = None
        async with aiohttp.ClientSession(trust_env=False, auto_decompress=False, headers=headers) as session:
            try:
                try:
                    async with asyncio.timeout(self.settings.timeout_seconds):
                        health = await self._request(session, "GET", "/health")
                        if (
                            not isinstance(health, dict)
                            or type(health.get("protocol")) is not int
                            or health["protocol"] != 1
                        ):
                            raise MediaDownloadError("远程视频服务协议不兼容")
                        if health.get("ready") is not True:
                            raise MediaDownloadError("远程视频服务尚未就绪")
                        if health.get("busy") is not False:
                            raise _failure(429)
                        key = uuid.uuid4().hex
                        # A lost creation reply is retried with the identical key/body,
                        # never as a second download job.
                        for attempt in range(2):
                            try:
                                value = await self._request(
                                    session,
                                    "POST",
                                    "/jobs",
                                    expected=(200, 202),
                                    data=body,
                                    headers={"Content-Type": "application/json", "Idempotency-Key": key},
                                )
                                break
                            except (aiohttp.ClientConnectionError, TimeoutError):
                                if attempt:
                                    raise
                        job_id = _job(value)
                        while value["state"] == "downloading":
                            delay = value.get("poll_after_seconds", 2)
                            if type(delay) not in (int, float) or not math.isfinite(delay):
                                raise MediaDownloadError("远程视频轮询间隔无效")
                            await asyncio.sleep(min(5, max(1, delay)))
                            value = await self._request(session, "GET", f"/jobs/{job_id}")
                            _job(value, job_id)
                        if value["state"] == "failed":
                            raise _failure(200, value)
                        if value["state"] != "ready":
                            raise MediaDownloadError("远程视频任务已取消")
                        await self._transfer(session, job_id, _ready(value), destination)
                except TimeoutError:
                    raise MediaDownloadError("远程视频等待与领取超过总预算") from None
                except aiohttp.ClientError:
                    raise MediaDownloadError("远程视频服务连接失败；未回退本机下载") from None
                yield destination
            finally:
                if job_id:
                    try:
                        await self._request(session, "DELETE", f"/jobs/{job_id}", expected=(204,))
                    except (aiohttp.ClientError, TimeoutError, MediaDownloadError):
                        logger.warning("[media] remote job cleanup failed; worker TTL will reclaim it")
