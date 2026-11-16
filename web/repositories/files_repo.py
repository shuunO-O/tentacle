"""Web 附件上传、下载与媒体内容读取仓储。"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from attachments.inputs import attachment_kind
from attachments.media import MAX_MEDIA_BYTES, MEDIA_SLOTS
from attachments.outgoing import open_outgoing_file
from runtime.paths import state_path

logger = logging.getLogger(__name__)


async def save_uploaded_file(fm: Any, file: UploadFile) -> dict[str, Any]:
    try:
        async with MEDIA_SLOTS:
            with tempfile.TemporaryDirectory(prefix="tentacle-upload-") as directory:
                path = Path(directory) / "input"
                size = 0
                with path.open("wb") as target:
                    while chunk := await file.read(256 * 1024):
                        size += len(chunk)
                        if size > MAX_MEDIA_BYTES:
                            raise ValueError("文件超过大小限制（64 MiB）")
                        await asyncio.to_thread(target.write, chunk)
                return await fm.upload_file_path(path, filename=file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(f"文件上传失败: {exc}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"上传失败: {exc}") from exc
    finally:
        await file.close()


def stream_outgoing_download(path: str, protected_path: Path) -> StreamingResponse:
    try:
        target, stream, metadata = open_outgoing_file(path, protected_paths=(protected_path,))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="该文件禁止下载或无读取权限") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="文件不存在或已删除") from exc
    except (ValueError, OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="文件路径无效或不是可下载文件") from exc

    def chunks():
        try:
            while chunk := stream.read(64 * 1024):
                yield chunk
        finally:
            stream.close()

    return StreamingResponse(
        chunks(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(target.name, safe=""),
            "Content-Length": str(metadata.st_size),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(stream.close),
    )


async def build_file_content_response(fm: Any, local_id: str, require_file_auth) -> FileResponse:
    record = await fm.get_file(local_id)
    if record is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    kind = attachment_kind(record)
    if kind == "file":
        await require_file_auth()
    abs_path = state_path(record["path"]).resolve()
    files_root = state_path("data/files").resolve()
    if files_root not in abs_path.parents:
        raise HTTPException(status_code=403, detail="非法路径")
    if not abs_path.is_file():
        raise HTTPException(status_code=404, detail="文件已删除")
    if kind == "file":
        return FileResponse(
            abs_path,
            filename=record["filename"],
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    return FileResponse(abs_path, media_type=record["mime"])
