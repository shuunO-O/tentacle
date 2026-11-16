#!/usr/bin/env python3
"""离线补建原文索引，或用新模型和维度重新向量化整个 Ladybug 记忆库。"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.ladybug_store import LadybugStore
from config.manager import ConfigManager
from runtime.paths import state_path
from memory.embeddings import EmbeddingClient
from memory.reembedding import ReembeddingError, rebuild_vectors


async def rebuild(config):
    ConfigManager(config)
    store = LadybugStore()
    try:
        return await store.rebuild_event_sources()
    finally:
        await store.close()


async def reembed(config, source, output):
    manager = ConfigManager(config)
    if source is None:
        source = state_path(manager.get_memory_config().get("ladybug", {}).get("path", "data/memory.lbdb"))
    client = EmbeddingClient()
    client._init()
    try:
        return await rebuild_vectors(source, output, client)
    finally:
        await client.client.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--reembed-all", action="store_true", help="重算摘要、实体和原文向量")
    parser.add_argument("--source", type=Path, help="全量模式的源库，缺省使用配置中的 memory.ladybug.path")
    parser.add_argument("--output", type=Path, help="可选新库路径，省略时在重算成功后切换原库文件")
    parser.add_argument("--apply", action="store_true", help="计算向量并写入数据库")
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("向量维护需要显式 --apply")
    if not args.reembed_all and (args.source is not None or args.output is not None):
        parser.error("--source 和 --output 仅用于 --reembed-all")
    try:
        operation = reembed(args.config, args.source, args.output) if args.reembed_all else rebuild(args.config)
        print(json.dumps({"status": "ok", **asyncio.run(operation)}))
        return 0
    except Exception as error:
        # 第三方错误可能含认证请求信息；CLI只输出类型，不输出消息、原文或连接。
        response = {"status": "error", "error_type": type(error).__name__}
        if isinstance(error, ReembeddingError):
            response["detail"] = str(error)
        print(json.dumps(response, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
