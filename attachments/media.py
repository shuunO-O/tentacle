"""Bounded audio/video inspection and single-video Bilibili ingestion.

No model calls. External programs run without a shell; cancellation kills their
process tree. Media stays on disk, and remote uploads use the existing FileManager.
"""

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

import aiohttp
from yarl import URL

from attachments.downloads import HttpJobsDownloader, download_settings
from attachments.inputs import attachment_kind

MAX_MEDIA_BYTES = 64 * 1024 * 1024
MAX_MEDIA_SECONDS = 600
MEDIA_SLOTS = asyncio.Semaphore(1)
_URL = re.compile(r"https?://[^\s<>\"'，。！？；（）【】]+", re.I)
_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}


def media_kind(record):
    return attachment_kind(record)


def supports_media(cfg, kind):
    return getattr(cfg, "api_style", None) in ("interactions", "gemini_generate") and bool(getattr(cfg, kind, False))


async def run_program(args, *, timeout=30, max_disk_bytes=None, directory=None):
    """Bound output and disk usage; reap ffmpeg children on timeout/cancellation."""
    with tempfile.TemporaryFile() as output:
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=output, stderr=asyncio.subprocess.DEVNULL,
                **({"start_new_session": True} if os.name != "nt" else {}),
            )
        except FileNotFoundError as exc:
            raise ValueError(f"缺少媒体依赖：{args[0]}，请按 docs/MEDIA_INPUTS.md 安装") from exc
        waiter = asyncio.create_task(process.wait())
        try:
            async with asyncio.timeout(timeout):
                while process.returncode is None:
                    if max_disk_bytes and directory:
                        size = sum(p.stat().st_size for p in Path(directory).rglob("*") if p.is_file())
                        if size > max_disk_bytes:
                            raise ValueError("视频下载超过临时空间限制")
                    try:
                        await asyncio.wait_for(asyncio.shield(waiter), timeout=0.25)
                    except TimeoutError:
                        continue
                if process.returncode:
                    raise ValueError("媒体处理失败；请检查依赖及链接是否公开可访问（不支持登录/会员视频）")
                output.seek(0)
                result = output.read(256 * 1024 + 1)
                if len(result) > 256 * 1024:
                    raise ValueError("媒体元数据过大")
                return result
        except TimeoutError as exc:
            raise ValueError("媒体处理超过时间上限，请使用更短的音视频") from exc
        finally:
            if process.returncode is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(process.pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                    )
                    await killer.wait()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await waiter


async def inspect_media(path):
    path = Path(path)
    if not 0 < path.stat().st_size <= MAX_MEDIA_BYTES:
        raise ValueError("音视频文件必须在 64 MiB 以内")
    raw = await run_program([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,mp3,wav,ogg,flac,aac,aiff",
        "-threads", "1",
        "-show_entries", "format=format_name,duration:stream=codec_type,duration:stream_disposition=attached_pic",
        "-of", "json", str(path.resolve()),
    ])
    info = json.loads(raw)
    streams = info.get("streams", [])
    video = any(s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic") for s in streams)
    if not video and not any(s.get("codec_type") == "audio" for s in streams):
        raise ValueError("文件中没有可识别的音视频轨道")
    duration = float(info.get("format", {}).get("duration") or max(
        (float(s.get("duration") or 0) for s in streams), default=0))
    if not math.isfinite(duration) or not 0 < duration <= MAX_MEDIA_SECONDS:
        raise ValueError("音视频时长必须在 10 分钟以内，且能读取时长")
    formats = set(info.get("format", {}).get("format_name", "").split(","))
    if formats & {"mov", "mp4", "m4a"}:
        mime = "video/mp4" if video else "audio/m4a"
    elif formats & {"matroska", "webm"}:
        # Gemini accepts WebM, not arbitrary Matroska codecs/containers.
        with path.open("rb") as source:
            if b"webm" not in source.read(4096):
                raise ValueError("请将 MKV 转换为 MP4 或 WebM 后上传")
        mime = "video/webm" if video else "audio/webm"
    elif not video:
        mime = next((mime for fmt, mime in {
            "mp3": "audio/mp3", "wav": "audio/wav", "ogg": "audio/ogg",
            "flac": "audio/flac", "aac": "audio/aac", "aiff": "audio/aiff",
        }.items() if fmt in formats), None)
    else:
        mime = None
    if mime is None:
        raise ValueError("不支持的音视频格式，请使用 MP4/WebM 或 MP3/WAV/M4A/OGG/FLAC/AAC/AIFF")
    return mime


def bilibili_links(text):
    links = []
    for match in _URL.finditer(text):
        url = match.group().rstrip(").,;!?]}")
        try:
            parsed = urlsplit(url)
            if parsed.hostname in _HOSTS and not parsed.username and parsed.port in (None, 443, 80):
                if url not in links:
                    links.append(url)
        except ValueError:
            continue
    return links


def validate_bilibili_url(url):
    """Lexical check only; remote backends resolve short links on their own host."""
    try:
        parsed = urlsplit(url)
        valid = (
            isinstance(url, str)
            and len(url) <= 4096
            and parsed.scheme in ("http", "https")
            and parsed.hostname in _HOSTS
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 80, 443)
        )
        if valid and parsed.hostname == "b23.tv":
            valid = bool(re.fullmatch(r"/[0-9A-Za-z]+/?", parsed.path))
        elif valid:
            page = parse_qs(parsed.query).get("p", ["1"])[0]
            valid = (
                bool(re.fullmatch(r"/video/(BV[0-9A-Za-z]+|av[0-9]+)/?", parsed.path))
                and page.isdigit()
                and 1 <= int(page) <= 10000
            )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("仅支持哔哩哔哩单个公开视频及有效分 P 链接")


async def canonical_bilibili_url(url):
    # Resolve short links ourselves, validating every hop before requesting it.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as session:
        for _ in range(5):
            parsed = urlsplit(url)
            if (parsed.hostname not in _HOSTS or parsed.username or parsed.password
                    or parsed.port not in (None, 443, 80) or parsed.scheme not in ("http", "https")):
                raise ValueError("仅支持哔哩哔哩公开视频链接")
            if parsed.hostname != "b23.tv":
                match = re.fullmatch(r"/video/(BV[0-9A-Za-z]+|av[0-9]+)/?", parsed.path)
                if not match:
                    raise ValueError("请分享单个 B 站视频链接（支持 BV/av 和 b23.tv 短链）")
                page = parse_qs(parsed.query).get("p", ["1"])[0]
                if not page.isdigit() or not 1 <= int(page) <= 10000:
                    raise ValueError("视频分 P 参数无效")
                return f"https://www.bilibili.com/video/{match[1]}?p={int(page)}"
            if not re.fullmatch(r"/[0-9A-Za-z]+/?", parsed.path):
                raise ValueError("无效的 b23.tv 短链")
            async with session.get(url, allow_redirects=False) as response:
                if response.status not in (301, 302, 303, 307, 308):
                    raise ValueError("B 站短链解析失败")
                url = str(response.url.join(URL(response.headers.get("Location", ""))))
    raise ValueError("B 站短链跳转次数过多")


@asynccontextmanager
async def _download_local(url, path, settings):
    if importlib.util.find_spec("yt_dlp") is None or not all(shutil.which(name) for name in ("ffmpeg", "ffprobe")):
        raise ValueError("缺少 yt-dlp/ffmpeg/ffprobe，请按 docs/MEDIA_INPUTS.md 安装媒体依赖")
    url = await canonical_bilibili_url(url)
    await run_program(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--playlist-items",
            "1",
            "--no-cache-dir",
            "--no-progress",
            "--quiet",
            "--socket-timeout",
            "15",
            "--retries",
            "0",
            "--fragment-retries",
            "0",
            "--concurrent-fragments",
            "1",
            "--limit-rate",
            "2M",
            "--max-filesize",
            str(MAX_MEDIA_BYTES),
            "--match-filters",
            f"duration <= {MAX_MEDIA_SECONDS}",
            "--format",
            "bv[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]",
            "--merge-output-format",
            "mp4",
            "--output",
            str(path.with_suffix(".%(ext)s")),
            "--",
            url,
        ],
        timeout=180,
        directory=path.parent,
        max_disk_bytes=MAX_MEDIA_BYTES * 2,
    )
    yield path


@asynccontextmanager
async def _download_http_jobs(url, path, settings):
    # The remote job remains leased until the existing upload path has finished.
    async with HttpJobsDownloader(settings).download(url, path):
        yield path


# Backends share a URL -> fixed local file boundary. Neither owns application
# history, storage or model calls; future protocols can add an adapter here.
_DOWNLOADERS = {"local": _download_local, "http_jobs": _download_http_jobs}


async def fetch_bilibili(url, file_manager, config=None):
    if not file_manager.configured:
        raise ValueError("Files API 未配置，无法上传视频")
    validate_bilibili_url(url)
    settings = download_settings(config)
    downloader = _DOWNLOADERS[settings.backend]
    async with MEDIA_SLOTS:
        with tempfile.TemporaryDirectory(prefix="tentacle-video-") as directory:
            path = Path(directory) / "video.mp4"
            async with downloader(url, path, settings):
                if not path.is_file():
                    raise ValueError("视频未下载，可能超过 64 MiB/10 分钟限制或没有可用格式")
                return await file_manager.upload_file_path(path, filename="哔哩哔哩视频.mp4")
