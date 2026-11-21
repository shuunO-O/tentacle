#!/bin/sh
set -eu

usage() {
    printf '用法: tentacle [start|restart|stop|status|logs|version|update|revert|check-updates|update-status|browser|help]\n'
    printf '  tentacle check-updates              立即检查正式版本，不安装\n'
    printf '  tentacle update-status              离线查看最近检查结果\n'
    printf '  tentacle version                    查看当前生产版本\n'
    printf '  tentacle update [vX.Y[.Z]]           从正式 GitHub Release 预览更新\n'
    printf '  tentacle update vX.Y --apply         确认后准备完整版本、切换并验证\n'
    printf '  tentacle update --recover            恢复中断的更新\n'
    printf '  tentacle revert [vX.Y]               预览回到上一次成功版本\n'
    printf '  tentacle revert --apply              确认后切换至上一个完整版本并验证\n'
    printf '  tentacle browser <install|start|stop|restart|status|logs|credentials|proxy-config|uninstall>\n'
    printf '                                      管理 Docker Neko 可视化浏览器（WebRTC + 本机 CDP）\n'
    exit "${1:-2}"
}

action="${1:-start}"
if [ "$#" -gt 0 ]; then
    shift
fi

updater=/usr/local/libexec/tentacle-update/update.py
service=tentacle.service
case "$action" in
    start|restart|stop|status|logs)
        if [ -f "$updater" ]; then
            service=$(/usr/bin/python3 -E -s "$updater" host-service)
        fi
        ;;
esac

case "$action" in
    check-updates|update-status)
        checker=/usr/local/libexec/tentacle-update/check_updates.py
        if [ ! -f "$checker" ]; then
            printf '尚未安装更新检查器，请升级主机维护工具。\n' >&2
            exit 1
        fi
        if [ "$action" = update-status ]; then
            exec /usr/bin/python3 -E -s "$checker" --status "$@"
        elif [ "$(id -u)" -eq 0 ]; then
            exec /usr/bin/python3 -E -s "$checker" "$@"
        else
            exec sudo /usr/bin/python3 -E -s "$checker" "$@"
        fi
        ;;
    update|revert|version)
        if [ ! -f "$updater" ]; then
            printf '尚未安装更新入口，请运行 sudo python3 scripts/install_update.py\n' >&2
            exit 1
        fi
        # --help 不需要读取受限生产目录。
        if [ "$action" = version ] || [ "${1:-}" = '--help' ] || [ "${1:-}" = '-h' ]; then
            exec /usr/bin/python3 -E -s "$updater" "$action" "$@"
        fi
        if [ "$(id -u)" -eq 0 ]; then
            exec /usr/bin/python3 -E -s "$updater" "$action" "$@"
        else
            exec sudo /usr/bin/python3 -E -s "$updater" "$action" "$@"
        fi
        ;;
    start|restart|stop)
        [ "$#" -eq 0 ] || usage
        if [ "$(id -u)" -eq 0 ]; then
            systemctl "$action" "$service"
        else
            sudo systemctl "$action" "$service"
        fi
        systemctl --no-pager --full status "$service"
        ;;
    status)
        [ "$#" -eq 0 ] || usage
        systemctl --no-pager --full status "$service"
        ;;
    logs)
        [ "$#" -eq 0 ] || usage
        if [ "$(id -u)" -eq 0 ]; then
            journalctl -u "$service" -n 80 --no-pager
        else
            sudo journalctl -u "$service" -n 80 --no-pager
        fi
        ;;
    browser)
        script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
        helper=""
        for cand in "${TENTACLE_BROWSER_HELPER:-}" "$script_dir/browser_neko.py" /opt/tentacle/current/scripts/browser_neko.py; do
            if [ -n "$cand" ] && [ -f "$cand" ]; then
                helper="$cand"
                break
            fi
        done
        if [ -z "$helper" ]; then
            printf '未找到 browser_neko.py，请升级 Tentacle 或直接执行 python3 scripts/browser_neko.py。\n' >&2
            exit 1
        fi
        if [ "$#" -eq 0 ] || [ "${1:-}" = '--help' ] || [ "${1:-}" = '-h' ]; then
            exec /usr/bin/python3 "$helper" --help
        fi
        if [ "$(id -u)" -eq 0 ]; then
            exec /usr/bin/python3 "$helper" "$@"
        else
            exec sudo /usr/bin/python3 "$helper" "$@"
        fi
        ;;
    help|-h|--help)
        [ "$#" -eq 0 ] || usage
        usage 0
        ;;
    *) usage ;;
esac
