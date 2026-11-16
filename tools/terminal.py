"""
真实终端工具

模型通过工具操作一个真实持久 shell 会话（PTY），效果等同人类直接操作命令行：
- cd/export/alias/后台任务等状态跨调用保持
- 命令输出实时可见（控制台/WebUI 终端面板）
- 安全审查拦截交互式/监视类程序

工具设计：
- terminal_run: 执行命令，返回完整输出 + 退出码；命令不结束则超时返回并提示
- terminal_interact: 与正在运行的前台程序交互（发送按键/Ctrl+C，或仅拉取新输出）
"""

from runtime.terminal_manager import get_terminal_manager


# 全局单例
manager = get_terminal_manager()


async def terminal_run(args: dict) -> str:
    """
    在真实终端中执行一条命令，返回完整输出。

    Args:
        args:
            - command: 要执行的命令（必填）
            - timeout: 超时秒数，默认 30；超时后命令仍在运行
            - cwd: 临时切换工作目录执行（不改变会话后续目录）

    Returns:
        状态 + 完整输出文本
    """
    return await manager.run(
        command=args.get("command", ""),
        timeout=args.get("timeout"),
        cwd=args.get("cwd"),
    )


async def terminal_interact(args: dict) -> str:
    """
    与正在运行的前台程序交互，并读取新输出。

    发送按键后再等待读取新输出；text 为空则仅读取新输出。
    Ctrl+C 用 \\x03，Enter 用 \\r 或 newline=true。

    Args:
        args:
            - text: 要发送的按键/文本（可选，空则只拉取新输出）
            - newline: 发送后是否追加回车，默认 false
            - timeout: 等待新输出的秒数，默认 2

    Returns:
        新输出文本
    """
    return await manager.interact(
        text=args.get("text", ""),
        newline=bool(args.get("newline", False)),
        timeout=args.get("timeout"),
    )


def register_tools(registry):
    """由启动加载器显式注册；导入本模块不注册工具。"""
    registry.register(
        name="terminal_run",
        description=(
            "在持久终端会话中执行 shell 命令（cd/环境变量跨调用保持），返回输出与退出码（过长会截断）。"
            "超时后命令仍在运行，可用 terminal_interact 拉取输出或发送 \\x03 中断。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令。",
                },
                "timeout": {
                    "type": "number",
                    "description": "超时秒数，默认 30。命令不结束（如 sleep、后台任务）会超时返回，仍在运行。",
                },
                "cwd": {
                    "type": "string",
                    "description": "临时切换工作目录执行该命令（不影响会话后续目录）。",
                },
            },
            "required": ["command"],
        },
        func=terminal_run,
        is_async=True,
    )
    registry.register(
        name="terminal_interact",
        description=(
            "与正在运行的终端前台程序交互，并读取新输出。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要发送的按键或文本，留空则只拉取新输出。Ctrl+C 用 \\x03，回车用 \\r 或设 newline=true。",
                },
                "newline": {
                    "type": "boolean",
                    "description": "发送 text 后是否追加回车，默认 false。",
                },
                "timeout": {
                    "type": "number",
                    "description": "等待新输出的秒数，默认 2。",
                },
            },
            "required": [],
        },
        func=terminal_interact,
        is_async=True,
    )
