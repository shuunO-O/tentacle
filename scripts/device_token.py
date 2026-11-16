#!/usr/bin/env python3
"""
设备令牌主机脚本（人工 CLI，性质同 rollback.py）

一次性绑定码的唯一产生渠道：绑定码不出自 WebUI（已受信身份在 WebUI 里只能
吊销 / 开关总闸，不能扩权），被窃的设备 cookie 无法给自己续命绑新设备。

所有带外操作都走独立文件，绝不直接改 webui_settings.json——运行中的服务端
把设置缓存在内存里，直改会在下次保存时被整体覆盖：
- data/device_enroll_pending.json  gen-code 写入（哈希码 + TTL），服务端校验通过后删除
- data/device_auth_freeze.json     lockout 写入，unlock 删除；存在期间冻结时刻之前
                                   绑定的设备 cookie 全部失效（服务端实时读盘）
- data/device_access_policy.json   deny-new / allow-new 控制新设备入口（实时读盘）

用法：
    .venv/bin/python scripts/device_token.py gen-code 手机      # 生成一次性绑定码（15 分钟有效）
    .venv/bin/python scripts/device_token.py status             # 查看总闸 / 设备 / 待用码
    .venv/bin/python scripts/device_token.py lockout            # 紧急冻结全部已绑设备
    .venv/bin/python scripts/device_token.py lockout -r "发现异常设备"
    .venv/bin/python scripts/device_token.py unlock             # 解除冻结
    .venv/bin/python scripts/device_token.py deny-new           # 拒绝新设备的全部请求
    .venv/bin/python scripts/device_token.py allow-new          # 重新开放绑定入口（仍需绑定码）
"""

import argparse
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime.paths import state_path
from web.auth_files import atomic_write_json
from web.settings import WebSettings

ENROLL_CODE_TTL_SECONDS = 15 * 60


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _fmt_ts(ts) -> str:
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))


def cmd_gen_code(args) -> int:
    if not state_path('data/webui_settings.json').exists():
        print(f"错误：找不到 {state_path('data/webui_settings.json')}（先在项目根目录运行一次服务端）", file=sys.stderr)
        return 2
    code = secrets.token_hex(8)  # 16 位十六进制，64 位熵
    if state_path('data/device_enroll_pending.json').exists():
        print("提示：已有一枚待用绑定码，将被覆盖。")
    atomic_write_json(state_path('data/device_enroll_pending.json'), {
        "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "expires": time.time() + ENROLL_CODE_TTL_SECONDS,
        "name": args.name,
    })
    print(f"绑定码: {code}")
    print(f"设备标签: {args.name or '（未命名设备）'}")
    print(f"有效期至: {_fmt_ts(time.time() + ENROLL_CODE_TTL_SECONDS)}（一次性，用后即焚）")
    print("在目标设备打开 WebUI，于登录遮罩输入此码即可绑定。")
    return 0


def cmd_status(args) -> int:
    try:
        settings = WebSettings(str(state_path('data/webui_settings.json')))
    except RuntimeError:
        print("错误：设备鉴权设置无法读取或已损坏，请先在主机修复。", file=sys.stderr)
        return 2
    devices = settings.list_devices()
    freeze = settings.freeze_info()
    pending = _read_json(state_path('data/device_enroll_pending.json'))

    print(f"设备验证开关: {'开启' if settings.device_auth else '关闭'}")
    print(f"新设备入口: {'开放（绑定仍需主机码）' if settings.allow_new_devices else '关闭（仅有效已绑定设备可访问）'}")
    if freeze is not None and freeze.get("invalid"):
        print("紧急冻结: 文件无法读取或已损坏，访问被拒绝（请在主机修复）")
    elif freeze is not None:
        print(f"紧急冻结: 生效中（{_fmt_ts(freeze.get('created'))} 起，冻结前绑定的设备全部失效）")
        if freeze.get("reason"):
            print(f"  原因: {freeze['reason']}")
    if pending is not None:
        print(f"待用绑定码: 有（标签 {pending.get('name') or '未命名'}，{_fmt_ts(pending.get('expires'))} 过期）")
    if not devices:
        print("已绑定设备: 无")
        return 0
    print(f"已绑定设备: {len(devices)} 台")
    for rec in devices:
        print(
            f"  {rec.get('name') or '未命名设备'}  绑定 {_fmt_ts(rec.get('created'))}"
            f"  最近活跃 {_fmt_ts(rec.get('last_seen'))}  id {rec['id'][:12]}…"
        )
    return 0


def cmd_lockout(args) -> int:
    atomic_write_json(state_path('data/device_auth_freeze.json'), {"created": time.time(), "reason": args.reason or ""})
    print("已写入紧急冻结：冻结前绑定的全部设备 cookie 立即失效，WebUI 无法解除。")
    print("恢复访问：若新设备入口已关闭先 allow-new -> gen-code 生成新码 -> 干净设备绑定 -> 吊销异常设备 -> unlock。")
    return 0


def cmd_unlock(args) -> int:
    if not state_path('data/device_auth_freeze.json').exists():
        print("当前没有生效的紧急冻结。")
        return 0
    state_path('data/device_auth_freeze.json').unlink()
    print("已解除冻结，原有设备 cookie 恢复有效（记得先在设置页吊销异常设备）。")
    return 0


def cmd_device_policy(args) -> int:
    allow = args.command == "allow-new"
    atomic_write_json(state_path('data/device_access_policy.json'), {"allow_new_devices": allow})
    if allow:
        print("已重新开放新设备入口；设备验证或冻结生效时仍需主机绑定码，未解除冻结。")
    else:
        print("已关闭新设备入口：仅有效已绑定设备可访问；新设备的页面、API、图片与 WebSocket 请求均拒绝。")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="device_token", description="设备令牌主机脚本（人工通道）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("gen-code", help="生成一次性绑定码（15 分钟有效，一码一设备）")
    p_gen.add_argument("name", nargs="?", default="", help="设备标签（可选，如：手机 / 笔记本）")

    sub.add_parser("status", help="查看总闸状态、已绑定设备与待用码（只读）")

    p_lock = sub.add_parser("lockout", help="紧急冻结：冻结前绑定的全部设备 cookie 立即失效")
    p_lock.add_argument("-r", "--reason", default="", help="冻结原因（可选，仅供 status 展示）")

    sub.add_parser("unlock", help="解除紧急冻结")
    sub.add_parser("deny-new", help="拒绝新设备的所有请求（已有设备照常访问）")
    sub.add_parser("allow-new", help="重新开放新设备入口（绑定仍需主机码）")

    args = parser.parse_args(argv)
    handlers = {
        "gen-code": cmd_gen_code,
        "status": cmd_status,
        "lockout": cmd_lockout,
        "unlock": cmd_unlock,
        "deny-new": cmd_device_policy,
        "allow-new": cmd_device_policy,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
