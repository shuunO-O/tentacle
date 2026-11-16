#!/usr/bin/env bash
# Tentacle 一键启动脚本
#
# 用法：
#   ./start.sh          前台启动 WebUI（监听地址见 config.yaml）
#   ./start.sh web      同上
#   tentacle            通过 systemd 启动后台服务（云服务器）
#   tentacle restart    配置修改后重启后台服务
#
# 云端生产由 systemd 管理；已有服务运行时不要另起实例。

set -e
: "${TENTACLE_HOME:?请先设置独立的 Tentacle 数据根绝对路径}"
cd "$(dirname "$0")"

MODE="${1:-web}"

if [ "$MODE" != "web" ]; then
  echo "未知模式: $MODE（仅支持 web）"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "错误：未找到 .venv/bin/python"
  echo "请先创建虚拟环境并安装依赖："
  echo "  python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt"
  exit 1
fi

if [ ! -f "web/static/index.html" ]; then
  echo "错误：未找到 web/static/index.html（前端尚未构建）"
  echo "请先构建前端："
  echo "  npm --prefix frontend ci && npm --prefix frontend run build"
  exit 1
fi

echo "启动 Tentacle WebUI…"
exec .venv/bin/python run_web.py
