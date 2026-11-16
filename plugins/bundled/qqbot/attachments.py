"""Bounded QQ CDN downloads feeding the same FileManager as Web uploads."""

import asyncio
import mimetypes
from pathlib import Path
import tempfile
from urllib.parse import urljoin, urlsplit

import aiohttp

from attachments.inputs import safe_filename
from attachments.manager import MAX_FILE_SIZE
from attachments.media import MEDIA_SLOTS

_CDN_DOMAINS = ("qq.com", "qq.com.cn", "qpic.cn", "qlogo.cn", "gtimg.cn", "myqcloud.com")


def checked_url(value: str) -> str:
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
        or not any(host == domain or host.endswith("." + domain) for domain in _CDN_DOMAINS)
    ):
        raise ValueError("QQ 附件地址不是受支持的官方 CDN")
    return value


async def receive_attachments(attachments: list, http, manager, *, proxy=None) -> list[dict]:
    if not isinstance(attachments, list) or len(attachments) > 10:
        raise ValueError("单轮最多 10 个附件")
    records = []
    for item in attachments:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            raise ValueError("QQ 附件缺少下载地址")
        url = checked_url(item["url"])
        filename = safe_filename(item.get("filename") or "file")
        if not Path(filename).suffix:
            filename += mimetypes.guess_extension(str(item.get("content_type") or "").split(";")[0]) or ".bin"
        if isinstance(item.get("size"), int) and item["size"] > MAX_FILE_SIZE:
            raise ValueError("QQ 附件超过 64 MiB 上限")
        async with MEDIA_SLOTS:
            with tempfile.TemporaryDirectory(prefix="tentacle-qq-file-") as directory:
                path = Path(directory) / "input"
                try:
                    async with asyncio.timeout(60):
                        for _ in range(4):
                            async with http.get(
                                url,
                                allow_redirects=False,
                                timeout=aiohttp.ClientTimeout(total=60),
                                proxy=proxy,
                            ) as response:
                                if response.status in {301, 302, 303, 307, 308}:
                                    url = checked_url(urljoin(url, response.headers.get("Location", "")))
                                    continue
                                if response.status != 200:
                                    raise ValueError(f"QQ 附件下载失败（HTTP {response.status}）")
                                if response.content_length is not None and response.content_length > MAX_FILE_SIZE:
                                    raise ValueError("QQ 附件超过 64 MiB 上限")
                                size = 0
                                with path.open("wb") as target:
                                    async for chunk in response.content.iter_chunked(256 * 1024):
                                        size += len(chunk)
                                        if size > MAX_FILE_SIZE:
                                            raise ValueError("QQ 附件超过 64 MiB 上限")
                                        await asyncio.to_thread(target.write, chunk)
                                break
                        else:
                            raise ValueError("QQ 附件重定向次数过多")
                except (aiohttp.ClientError, TimeoutError):
                    raise ValueError("QQ 附件下载失败或超时，请重新发送") from None
                records.append(await manager.upload_file_path(path, filename=filename))
    return records
