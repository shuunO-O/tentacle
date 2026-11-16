"""文件上传、列表、受控下载与媒体访问路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, UploadFile

from web.deps import AppContext
from web.repositories.files_repo import (
    build_file_content_response,
    save_uploaded_file,
    stream_outgoing_download,
)
from web.schemas import FileListResponse, FileUploadResponse, OkResponse


def files_router(ctx: AppContext, auth_dep) -> APIRouter:
    router = APIRouter(prefix="/api/files")

    @router.post(
        "/upload",
        dependencies=[auth_dep],
        response_model=FileUploadResponse,
        response_model_exclude_unset=True,
    )
    async def files_upload(file: UploadFile):
        """暂存有界附件；由 FileManager 区分媒体与本地文档。"""
        record = await save_uploaded_file(ctx.get_file_manager(), file)
        return {"ok": True, "file": record}

    @router.get(
        "",
        dependencies=[auth_dep],
        response_model=FileListResponse,
        response_model_exclude_unset=True,
    )
    async def files_list():
        """列出全部文件记录。"""
        records = await ctx.get_file_manager().list_files()
        return {"files": records}

    @router.get("/download", dependencies=[auth_dep])
    def files_download(path: str):
        """鉴权后的附件下载；与 QQ 出站共用同一路径安全策略。"""
        return stream_outgoing_download(path, ctx.settings.path)

    @router.get("/{local_id}/content")
    async def files_content(local_id: str, request: Request):
        """
        媒体本体供浏览器渲染；普通文件要求完整 REST 鉴权并强制下载。
        path 只允许 data/files/ 下的本地文件。
        """
        if not ctx.device_ok(request):
            raise HTTPException(status_code=401, detail="设备未绑定")
        return await build_file_content_response(
            ctx.get_file_manager(),
            local_id,
            lambda: ctx.authenticate(request),
        )

    @router.delete(
        "/{local_id}",
        dependencies=[auth_dep],
        response_model=OkResponse,
        response_model_exclude_unset=True,
    )
    async def files_delete(local_id: str):
        """删除附件记录及文件本体。"""
        ok = await ctx.get_file_manager().delete_file(local_id)
        if not ok:
            raise HTTPException(status_code=404, detail="文件不存在")
        return {"ok": True}

    return router
