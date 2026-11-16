"""Tentacle 本地插件最小完整示例。"""

from __future__ import annotations


def setup(ctx) -> None:
    prefix = str(ctx.config.get("prefix") or "[example]")
    counter_file = ctx.data_path("count.txt")

    def echo_tool(args: dict) -> dict:
        msg = str(args.get("message") or "")
        count = int(counter_file.read_text(encoding="utf-8")) + 1 if counter_file.exists() else 1
        counter_file.write_text(str(count), encoding="utf-8")
        return {"reply": f"{prefix} {msg}", "count": count}

    ctx.register_tool(
        "echo",
        "示例工具：带计数前缀回显文本",
        {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "要回显的文本"}},
            "required": ["message"],
            "additionalProperties": False,
        },
        echo_tool,
    )
    ctx.register_command("example", "查看示例插件计数", lambda args, _s: f"{prefix} args={args}")
    ctx.on("after_round", lambda payload: ctx.logger.debug("round=%s done", payload.get("round_id")))
