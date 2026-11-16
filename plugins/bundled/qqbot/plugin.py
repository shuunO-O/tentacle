"""内置 QQ 官方机器人插件：经 channels:register 与 background:task 接入 C2C 私聊通道。"""

from __future__ import annotations

from .adapter import QQAdapter


def setup(ctx) -> None:
    cfg = ctx.config
    app_id_env = str(cfg.get("app_id_env") or "QQ_APP_ID").strip()
    secret_env = str(cfg.get("client_secret_env") or "QQ_CLIENT_SECRET").strip()
    app_id = (ctx.get_env(app_id_env) or "").strip()
    client_secret = (ctx.get_env(secret_env) or "").strip()
    if not app_id or not client_secret:
        raise RuntimeError(f"缺少必要环境变量: {app_id_env} / {secret_env}")

    async def _emit(text: str, *, platform: str = "qq", user_id: str | None = None, images=None):
        return await ctx.emit_user_message(text, platform=platform, user_id=user_id, images=images)

    adapter = QQAdapter(
        app_id=app_id,
        client_secret=client_secret,
        allow_from=cfg.get("allow_from") or [],
        max_chunk=int(cfg.get("max_chunk", 4000) or 4000),
        default_to=str(cfg.get("default_to") or ""),
        proxy=str(cfg.get("proxy") or ""),
        split_messages=bool(cfg.get("split_messages", True)),
        split_max_segments=int(cfg.get("split_max_segments", 3) or 3),
        chat_file=ctx.data_path("chat.json"),
        emit_message=_emit,
    )
    ctx.register_channel(
        "qq",
        sender=adapter._deliver,
        description="QQ 官方机器人 C2C 私聊",
    )
    ctx.create_background_task(adapter.run(), name="qqbot")
    ctx.qqbot_adapter = adapter


async def teardown(ctx) -> None:
    adapter = getattr(ctx, "qqbot_adapter", None)
    if adapter is not None:
        await adapter.stop()
