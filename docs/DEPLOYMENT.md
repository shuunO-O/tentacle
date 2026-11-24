# Linux 部署与更新

Tentacle 使用 Python 3.13 和 systemd，Ubuntu 24.04 为首个验收目标。生产只运行一个 `tentacle.service`，Web、心跳、Scheduler 和可选 QQ 共享同一进程。Ladybug 由 Python 依赖内嵌运行。

| 目录 | 用途 |
| --- | --- |
| `/opt/tentacle/releases/vX.Y` | root 管理的应用、公开资源和该版本独立的 `.venv` |
| `/opt/tentacle/current` | root 管理的当前版本链接 |
| `/opt/tentacle/python` | 自动安装时由 root 管理的基础解释器 |
| `/home/tentacle/config` | `config.yaml` 与应用凭据 `.env` |
| `/home/tentacle/{data,prompt,skills,workspace,logs,.cache,tmp}` | 应用用户的持久数据和运行目录 |
| `/etc/tentacle` | root 独占的安装记录、更新配置及可选只读 GitHub token |
| `/var/lib/tentacle-releases` | root 管理的当前版本和中断恢复记录 |
| `/usr/local/bin/tentacle` | systemd 和版本管理入口 |

## 首次安装

从管理员控制的目录取得已通过 CI 的干净 Git 检出。公开仓库直接运行安装命令即可，无需配置 GitHub token；仅在对接私有 Fork 仓库或需要提高 GitHub API 限流配额时，才需额外传入 `--token-file /path/to/token`（文件属主为 root、权限 `0600`、仅具 `Contents:read` 权限）。以下命令由管理员在检出目录运行：

```sh
sudo python3 scripts/install_linux.py --apply
```

省略 `--apply` 可预览目录和账号。安装器校验正式 Release、创建 `tentacle` 系统账号、安装 Python 和独立依赖、启动应用并启用定时检查。指定 `--version vX.Y` 可固定正式版本。已有合适的 root 管理解释器时可加 `--python /absolute/path/to/python3.13`。已准备系统依赖时可加 `--skip-system-packages`。离线首装可用 `--bundle` 指定从可信渠道取得的完整正式附件目录，安装 Python 和依赖仍需可用的包来源。

安装器仅补齐缺失的 Home 文件。默认监听 `127.0.0.1:8420`，默认关闭心跳、QQ 机器人插件（`qqbot`）、鸿蒙客户端插件（`harmony`）和子 Agent。首次配置可通过 SSH 转发 `8420` 到本机，或在前置 HTTPS 反向代理（Nginx / Caddy）就绪后打开 WebUI 配置模型、登录密码与设备鉴权。Web 将应用密钥值保存到 `/home/tentacle/config/.env`，文件权限为 `0600`。启用心跳前先确认模型配置。自定义 `--port` 需与迁入配置的监听端口一致。

### 远程访问与设备鉴权

面向公网访问时，保持 `tentacle.service` 仅监听本机回环地址，由前置 Nginx / Caddy 提供 HTTPS 反向代理，并同时启用 Web 登录密码与**设备鉴权**；如无需公网暴露，也可搭配 **Tailscale** 等私有组网直接访问。

设备鉴权的一次性绑定码仅能从主机带外生成（WebUI 只能开关总闸或吊销设备，不能扩权发码），可通过 `tentacle device` 管理：

```sh
tentacle device status                  # 查看设备验证开关、新设备入口、冻结状态与已绑定设备
tentacle device gen-code 手机           # 生成一次性绑定码（15 分钟有效，用后即焚）
tentacle device deny-new                # 常用设备绑定完成后关闭新设备入口（未绑定设备一律 403）
tentacle device allow-new               # 需要绑定新设备时临时重新开放入口（仍需绑定码）
tentacle device lockout -r "异常排查"   # 紧急冻结：冻结前绑定的全部设备立即失效
tentacle device unlock                  # 吊销异常设备后解除冻结
```

首装中断时，修复日志中指出的问题，使用原提交和相同参数重跑。可用 `sudo journalctl -u tentacle.service -n 80 --no-pager` 和 Home 下的应用日志检查启动失败。失败时安装器尝试停止候选服务，保留已构建的版本和 Home 供重试。

## 更新与回退

新版安装完成后，「应用配置 → 服务与维护」会显示当前版本、最近一次检查结果和维护进度。启用 Web 密码或设备鉴权后，可以检查更新、安装刚检查过的兼容正式版或重启服务。主对话也可通过 `maintenance` 工具执行这些操作。如果在用户配置里覆盖过主对话的工具列表，请将 `maintenance` 加到 `roles.chat.tools.enabled`。

Agent 发起的重启或升级会等发起回合正常结束并完成归档，再由独立的主机维护服务执行。回合失败、取消或等待用户输入时，预约会取消。Web 在服务恢复后可继续查看执行结果。回退和中断恢复通过下方 CLI 操作。

已安装 v1.7 的主机需要从新版对应的、已通过 CI 的干净 Git 检出执行 `sudo python3 scripts/install_update.py`。这一步会安装并启用维护 socket，现有服务可继续运行。之后通过 CLI 安装新版应用，Web 和 Agent 入口才会生效。需要暂停委托维护时，可以执行 `sudo systemctl disable --now tentacle-maintenance.socket`。

```sh
tentacle version
tentacle update-status
sudo tentacle check-updates
sudo tentacle update
sudo tentacle update vX.Y --apply
sudo tentacle revert --apply
sudo tentacle update --recover
```

定时检查器每六小时检查正式版并写公开摘要。更新器校验 Release 后，先构建新的版本目录和依赖，再短暂停服、切换 `current`、启动并验证进程和本地 Web 鉴权状态。依赖构建失败时旧服务继续运行。切换或健康检查失败时尝试恢复先前版本。若有未完成的切换，修复服务问题后运行 `sudo tentacle update --recover`，该命令使用本机版本和记录，可以离线运行。遇到记录与链接不一致时先核对 `readlink /opt/tentacle/current`、`/var/lib/tentacle-releases/current.json` 和 `.update-pending.json`，按记录排查，保留现场以便重试。

`revert` 使用上一个成功版本的代码及依赖。版本之间共享 Home，回退保留已经写入的数据和外部任务的结果。数据兼容代号在 `runtime/data_epoch.txt`。配置或持久格式出现无法向后读取的变更时应提升代号，并随 Release 提供停服数据迁移与恢复流程。更新器会在停服前拒绝跨代号切换。

主机的更新校验只核对 GitHub 源码树/CI 证据真实性与敏感目录安全边界；在同一维护状态协议下，已通过校验的正式 Release 激活并健康就绪后，更新器还会把随包发布的宿主维护工具与 `/usr/local/bin/tentacle` 备份后同步刷新。

## 维护命令使用的路径

从任意目录执行应用自带 CLI 时，显式传入 `TENTACLE_HOME`。以下命令只检查配置，应用级维护操作应以应用账号运行。

```sh
sudo -u tentacle env TENTACLE_HOME=/home/tentacle \
  /opt/tentacle/current/.venv/bin/python /opt/tentacle/current/scripts/configure.py check
```

Ladybug 库默认位于 `/home/tentacle/data/memory.lbdb`。独立维护该库前先停止宿主。浏览器、QQ 机器人插件、DSH 编程台、推送和音视频工具仍按各自的使用文档设置。
