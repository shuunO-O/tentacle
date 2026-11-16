"""设置页定期备份、一次性迁移导出与两步导入恢复路由。"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from runtime.home_backup import (
    MAX_IMPORT_UPLOAD_BYTES,
    PENDING_APPLY_REL,
    BackupConflictError,
    BackupError,
    HomeBackupManager,
    PassphraseRequiredError,
    _mkdir_private,
)
from web.deps import AppContext
from web.schemas import (
    BackupApplyBody,
    BackupCreateResponse,
    BackupImportApplyResponse,
    BackupImportInspectResponse,
    BackupNowBody,
    BackupPolicyBody,
    BackupPolicyUpdateResponse,
    BackupStatusResponse,
    BackupWorkspaceTreeResponse,
    OkResponse,
)

logger = logging.getLogger(__name__)


def _infer_state_root(ctx: AppContext) -> Path:
    db_path = Path(ctx.db_path).resolve()
    if db_path.parent.name == "data":
        return db_path.parent.parent
    return db_path.parent


def _resolve_backup_manager(ctx: AppContext) -> HomeBackupManager:
    if ctx.backup_manager is not None:
        return ctx.backup_manager
    mgr = HomeBackupManager(_infer_state_root(ctx))
    ctx.backup_manager = mgr
    return mgr


def _resolve_restart(ctx: AppContext):
    return ctx.restart_controller or getattr(ctx.configuration_service, "restart", None)


def _file_chunks(path: Path, chunk_size: int = 64 * 1024):
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            yield chunk


def backups_router(ctx: AppContext, auth_dep) -> APIRouter:
    router = APIRouter(prefix="/api/backups", dependencies=[auth_dep])

    def has_web_auth() -> bool:
        return bool(ctx.settings.has_password or ctx.settings.requires_device)

    @router.get(
        "",
        response_model=BackupStatusResponse,
        response_model_exclude_unset=True,
    )
    async def backup_status():
        mgr = _resolve_backup_manager(ctx)
        restart = _resolve_restart(ctx)
        snap = mgr.status_snapshot()
        snap["env_export_allowed"] = has_web_auth()
        snap["restart_available"] = restart is not None
        snap["instance_id"] = getattr(restart, "instance_id", None) if restart is not None else None
        snap["restarting"] = bool(
            getattr(ctx.agent_session, "_restarting", False)
            or (restart is not None and getattr(restart, "state", "idle") != "idle")
        )
        return snap

    @router.put(
        "/policy",
        response_model=BackupPolicyUpdateResponse,
        response_model_exclude_unset=True,
    )
    async def update_backup_policy(body: BackupPolicyBody):
        mgr = _resolve_backup_manager(ctx)
        try:
            policy = mgr.update_policy(
                enabled=body.enabled,
                interval_hours=body.interval_hours,
                retention_count=body.retention_count,
                include_files=body.include_files,
            )
            return {"ok": True, "policy": policy, "next_run_at": mgr.next_run_at(policy)}
        except BackupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.get(
        "/workspace",
        response_model=BackupWorkspaceTreeResponse,
        response_model_exclude_unset=True,
    )
    async def list_backup_workspace(path: str = ""):
        mgr = _resolve_backup_manager(ctx)
        try:
            return await asyncio.to_thread(mgr.list_workspace, path)
        except BackupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @router.post(
        "/now",
        response_model=BackupCreateResponse,
        response_model_exclude_unset=True,
    )
    async def create_backup_now(body: BackupNowBody | None = None):
        mgr = _resolve_backup_manager(ctx)
        try:
            item = await mgr.create_backup(
                kind="manual",
                include_files=body.include_files if body is not None else None,
                scopes=body.scopes if body is not None else None,
                workspace_paths=body.workspace_paths if body is not None else None,
            )
            return {"ok": True, "backup": item}
        except BackupConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BackupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @router.get("/{backup_id}/download")
    def download_backup(backup_id: str):
        mgr = _resolve_backup_manager(ctx)
        try:
            arc_path = mgr.get_backup_archive_path(backup_id)
        except BackupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

        size = arc_path.stat().st_size
        return StreamingResponse(
            _file_chunks(arc_path),
            media_type="application/gzip",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(arc_path.name, safe=""),
                "Content-Length": str(size),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete(
        "/{backup_id}",
        response_model=OkResponse,
        response_model_exclude_unset=True,
    )
    async def delete_backup(backup_id: str):
        mgr = _resolve_backup_manager(ctx)
        try:
            await mgr.delete_backup(backup_id)
            return {"ok": True}
        except BackupConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BackupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @router.post("/export")
    async def export_migration_archive(request: Request):
        """生成一次性迁移下载包。包含 .env 时要求已启用鉴权且强制使用用户口令加密，绝不留存服务器磁盘。"""
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(status_code=415, detail="请使用 JSON 提交导出参数")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 131072:
                raise HTTPException(status_code=413, detail="导出参数过大")
            raw.extend(chunk)
        try:
            body = json.loads(raw or b"{}")
            allowed = {"include_files", "include_env", "passphrase", "scopes", "workspace_paths"}
            if not isinstance(body, dict) or not set(body).issubset(allowed):
                raise ValueError
            include_files = body.get("include_files", True)
            include_env = body.get("include_env", False)
            passphrase = body.get("passphrase")
            scopes = body.get("scopes")
            workspace_paths = body.get("workspace_paths")
            if type(include_files) is not bool or type(include_env) is not bool:
                raise ValueError
            if passphrase is not None and not isinstance(passphrase, str):
                raise ValueError
            if scopes is not None and (not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes)):
                raise ValueError
            if workspace_paths is not None and (
                not isinstance(workspace_paths, list) or not all(isinstance(s, str) for s in workspace_paths)
            ):
                raise ValueError
        except (ValueError, RecursionError):
            # 不在错误详情里回显口令或输入原文
            raise HTTPException(status_code=422, detail="导出参数格式无效") from None

        if include_env and not has_web_auth():
            raise HTTPException(status_code=403, detail="导出包含 .env 凭据前，请先启用 Web 访问密码或设备验证")

        mgr = _resolve_backup_manager(ctx)
        try:
            tmp, out_path, download_name = await mgr.create_ephemeral_export(
                include_files=include_files,
                include_env=include_env,
                passphrase=passphrase,
                scopes=scopes,
                workspace_paths=workspace_paths,
            )
        except BackupConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BackupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        size = out_path.stat().st_size

        def stream_and_close():
            try:
                yield from _file_chunks(out_path)
            finally:
                tmp.cleanup()

        return StreamingResponse(
            stream_and_close(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(download_name, safe=""),
                "Content-Length": str(size),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(tmp.cleanup),
        )

    @router.post(
        "/import/inspect",
        response_model=BackupImportInspectResponse,
        response_model_exclude_unset=True,
    )
    async def inspect_backup_import(
        file: UploadFile | None = File(default=None),
        backup_id: str | None = Form(default=None),
        passphrase: str | None = Form(default=None),
    ):
        if bool(file and file.filename) == bool(backup_id and backup_id.strip()):
            if file is not None:
                await file.close()
            raise HTTPException(status_code=422, detail="请上传迁移包或指定一条历史备份（二选一）")

        mgr = _resolve_backup_manager(ctx)
        upload_tmp: tempfile.TemporaryDirectory | None = None
        try:
            archive_path: Path | None = None
            if file is not None and file.filename:
                _mkdir_private(mgr.staging_dir)
                upload_tmp = tempfile.TemporaryDirectory(prefix=".upload-", dir=str(mgr.staging_dir))
                archive_path = Path(upload_tmp.name) / "upload.archive"
                total = 0
                with archive_path.open("wb") as dst:
                    while chunk := await file.read(256 * 1024):
                        total += len(chunk)
                        if total > MAX_IMPORT_UPLOAD_BYTES:
                            raise HTTPException(status_code=413, detail="上传的备份包超出 1 GiB 大小上限")
                        await asyncio.to_thread(dst.write, chunk)
            return await mgr.inspect_import(
                archive_path=archive_path,
                backup_id=backup_id.strip() if backup_id else None,
                passphrase=passphrase,
            )
        except PassphraseRequiredError as exc:
            return JSONResponse(
                status_code=422,
                content={"detail": str(exc), "code": "passphrase_required"},
            )
        except BackupConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except BackupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        finally:
            if file is not None:
                await file.close()
            if upload_tmp is not None:
                upload_tmp.cleanup()

    @router.post(
        "/import/apply",
        status_code=202,
        response_model=BackupImportApplyResponse,
        response_model_exclude_unset=True,
    )
    async def apply_backup_import(body: BackupApplyBody, background: BackgroundTasks):
        mgr = _resolve_backup_manager(ctx)
        restart = _resolve_restart(ctx)
        if restart is None:
            raise HTTPException(status_code=503, detail="当前实例未启用自动重启，请改用命令行导入迁移包")
        if getattr(ctx.agent_session, "_restarting", False) or getattr(restart, "state", "idle") != "idle":
            raise HTTPException(status_code=409, detail="应用正在准备重启")

        try:
            pending = await mgr.prepare_apply_import(
                body.import_token,
                apply_files=body.apply_files,
                apply_env=body.apply_env,
                apply_workspace=body.apply_workspace,
                create_pre_backup=body.create_pre_backup,
            )
        except BackupConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except BackupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

        if not restart.reserve():
            (mgr.state_root / PENDING_APPLY_REL).unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail="应用正在准备重启")

        background.add_task(restart.request)
        return {
            "ok": True,
            "state": getattr(restart, "state", "waiting"),
            "instance_id": getattr(restart, "instance_id", None),
            "pre_backup_id": pending.get("pre_backup_id") or "",
            "apply_env": bool(pending.get("apply_env", False)),
            "apply_files": bool(pending.get("apply_files", True)),
            "apply_workspace": bool(pending.get("apply_workspace", True)),
        }

    @router.delete(
        "/last-restore",
        response_model=OkResponse,
        response_model_exclude_unset=True,
    )
    async def ack_last_restore():
        _resolve_backup_manager(ctx).ack_last_restore()
        return {"ok": True}

    return router
