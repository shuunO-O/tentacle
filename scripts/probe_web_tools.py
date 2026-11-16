"""显式授权后用公开资料探测网页工具；不启动应用、不打印密钥或完整响应。"""

import argparse
import asyncio
import io
import json
import os
from pathlib import Path
import pwd
import sys
import time

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.web_sources as web_sources
from tools.web_fetch import web_fetch
from tools.web_search import web_search


def load_key(path):
    # 只解析两个已知拼写的行，不加载其他生产变量。
    lines = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            name = line.strip().removeprefix("export ").split("=", 1)[0].strip()
            if name in ("PARALELL_API_KEY", "PARALLEL_API_KEY"):
                lines.append(line)
    values = dotenv_values(stream=io.StringIO("".join(lines)), interpolate=False)
    for name in ("PARALELL_API_KEY", "PARALLEL_API_KEY"):
        if values.get(name):
            return name, values[name]
    raise ValueError("生产文件中未找到非空的 Parallel API Key")


async def probe():
    cases = [
        ("web_search", web_search, {
            "queries": ["Python official documentation asyncio TaskGroup"],
            "objective": "Find official Python documentation describing asyncio.TaskGroup.",
            "max_results": 3,
        }),
        ("web_fetch", web_fetch, {
            "urls": ["https://docs.python.org/3/library/asyncio-task.html"],
            "objective": "Explain asyncio.TaskGroup using official documentation.",
        }),
    ]
    success = True
    for name, func, args in cases:
        started = time.monotonic()
        result = json.loads(await func(args))
        rows = result.get("results", [])
        summary = {
            "tool": name, "elapsed_seconds": round(time.monotonic() - started, 2),
            "result_count": len(rows), "usage": result.get("usage"),
            "warnings": result.get("warnings"), "errors": result.get("errors"),
            "truncated": result.get("truncated", False),
            "sources": [{"url": row.get("url"), "title": row.get("title"),
                         "excerpt_chars": sum(len(s) for s in row.get("excerpts", [])),
                         "full_content_chars": len(row.get("full_content") or "")}
                        for row in rows],
        }
        if "error" in result:
            summary.update({key: result[key] for key in ("error", "status", "retryable") if key in result})
        valid = bool(rows) and any(row.get("excerpts") or row.get("full_content") for row in rows)
        summary["ok"] = valid and "error" not in result
        success = success and summary["ok"]
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        name, key = load_key(args.env_file)
    except (OSError, ValueError):
        print(json.dumps({"error": "无法读取目标文件中的 Parallel API Key"}, ensure_ascii=False))
        return 1
    # sudo 只用于读取受限文件；网络测试降权为原登录用户，密钥只在内存中。
    if os.geteuid() == 0:
        account = pwd.getpwnam(os.environ.get("SUDO_USER", "ubuntu"))
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    os.environ[name] = key
    web_sources._config = lambda: {"provider": "parallel", "api_key_env": name}
    print(json.dumps({"credential_env_name": name, "credential_present": True,
                      "effective_uid": os.geteuid()}), flush=True)
    return asyncio.run(probe())


if __name__ == "__main__":
    raise SystemExit(main())
