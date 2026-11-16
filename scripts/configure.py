#!/usr/bin/env python3
"""离线配置管理；不加载 .env、不启动应用、不执行模型请求。"""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.store import ConfigurationError, ConfigStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="显式指定独立的用户配置文件")
    parser.add_argument("action", choices=("show", "check", "set", "restore", "history", "create", "delete"))
    parser.add_argument("--set", action="append", default=[], metavar="PATH=JSON", dest="values")
    parser.add_argument("--revision", help="show/预览返回的文件版本；实际写入必填")
    parser.add_argument("--backup", help="恢复的备份标识，仅恢复页面可管理的字段")
    parser.add_argument("--kind", choices=("providers", "models", "roles"))
    parser.add_argument("--id", dest="entity_id")
    parser.add_argument("--value", help="新条目的 JSON 配置对象")
    parser.add_argument("--apply", action="store_true", help="写文件（运行实例仍需在 Web 点击应用或由主机重启）")
    args = parser.parse_args()
    try:
        store = ConfigStore(args.config)
        revision, _, raw = store.read()
        if args.action == "show":
            result = store.public()
        elif args.action == "history":
            result = {"history": store.history()}
        elif args.action == "check":
            from config.store import fields_for, value_at
            # 只校验文件中实际声明的受管字段；不要求未使用角色的密钥存在。
            store.validate(raw, [p for p in fields_for(raw) if value_at(raw, p, "__absent__") != "__absent__"])
            result = {"valid": True, "revision": revision, "scope": "managed fields; no credentials or network checked"}
        else:
            if args.apply and not args.revision:
                raise ConfigurationError("写入必须提供预览得到的 --revision")
            if args.action in {"create", "delete"}:
                operation = {"op": args.action, "kind": args.kind, "id": args.entity_id}
                if args.action == "create":
                    operation["value"] = json.loads(args.value or "null")
                result = store.structure([operation], args.revision or revision, apply=args.apply)
            elif args.action == "restore":
                if not args.backup:
                    raise ConfigurationError("恢复需要 --backup")
                result = store.restore(args.backup, args.revision or revision, apply=args.apply)
            else:
                changes = {}
                for entry in args.values:
                    path, separator, text = entry.partition("=")
                    if not separator or path in changes:
                        raise ConfigurationError("--set 必须为不重复的 PATH=JSON")
                    changes[path] = json.loads(text)
                result = (store.save if args.apply else store.preview)(changes, args.revision or revision)
            result["runtime"] = "仅修改文件；运行中的应用需从 Web 应用动态配置，或由主机重启。"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError):
        # JSON/OS 异常可能包含输入文本或主机路径。
        print("配置操作失败：检查字段、JSON、版本与文件权限；未执行运行时应用。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
