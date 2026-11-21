# Docker Neko 可视化浏览器部署指南

通过 Docker 运行 [`m1k1o/neko`](https://github.com/m1k1o/neko)（Apache-2.0，Chromium + Xvfb + WebRTC），可在一台 Linux 服务器上同时满足：

- **Agent 自动化**：Chromium 的 CDP 调试端口仅绑定宿主机回环 `127.0.0.1:9222`，由 Tentacle 内置 `browser` 插件（`agent-browser` CLI）自动发现并驱动，无需在宿主机安装本地 Chrome；
- **人类丝滑接管**：通过 Neko 的 WebRTC 视频流（默认 `1280x720@24`、单端口 TCP+UDP MUX）随时接管登录或人机验证，无 DPI 缩放错位；
- **登录态持久化**：挂载宿主机独立 `profile/` 目录，跨重启保留 Cookie 与会话。

---

## 1. 前置准备与一键安装

1. 如尚未安装 `agent-browser` CLI，在宿主机安装（无需安装本地 Chrome）：

```bash
npm i -g agent-browser
```

2. 在 Linux 服务器上执行一键部署（尚未安装 Docker 时加上 `--install-docker` 会自动调用 Docker 官方脚本安装并启动服务）：

```bash
# 仅监听本机 127.0.0.1:8080（推荐，挂载到现有 Nginx/Caddy/Tailscale HTTPS 反代下）
sudo tentacle browser install \
  --install-docker \
  --live-view-url https://browser.example.com
```

> **`--nat-ip` 说明（选填）**：用于告知浏览器客户端连接 WebRTC 媒体流的服务器对外地址。
> - **留空**（默认）：Neko 启动时自动探测服务器公网 IP。
> - **填域名（推荐，适合动态 IP / 换 IP 场景）**：如 `--nat-ip browser.example.com`，Neko 会在每次连接时自动解析最新 DNS，IP 变更时只需更新 DNS 记录即可。
> - **多地址**：支持逗号分隔，如 `--nat-ip browser.example.com,100.64.0.1`。

如不想通过反代，也可直接指定监听地址（靠 Neko 自动生成的随机密码保护）：

```bash
sudo tentacle browser install \
  --bind 0.0.0.0 \
  --http-port 8080 \
  --live-view-url http://<你的IP或域名>:8080
```

安装脚本（[scripts/browser_neko.py](../scripts/browser_neko.py)）会自动完成：
1. 创建运行目录（默认 `/var/lib/tentacle-browser`）与持久化 `profile/` 目录；
2. 生成随机高强度 Neko 登录密码并写入 `0600` 权限的 `.env`（绝不写入 Tentacle `config.yaml`，也不在 WebUI 回显）；
3. 生成开启本机 CDP 且关闭访客模式的 `chromium.conf` 与 `docker-compose.yaml`；
4. 自动将 `neko_*` 参数（以及可选 `live_view_url`）同步到 Tentacle 的 `config.yaml`，并自动清理旧版残留字段；
5. 在 Linux systemd 主机上自动安装按需拉起的受控 Unix Socket（`tentacle-browser-neko.socket`，仅允许 `tentacle` 应用账号通过 `SO_PEERCRED` 调用白名单启停动作，无需把应用账号加入 `docker` 组，空闲零内存开销）；
6. 拉取镜像、启动容器并探活本机 CDP 端点。

---

## 2. 在 WebUI 中配置与可视化接管

完成上述初始化后，日常配置与启停都可直接在 WebUI 完成：

- **修改参数与启停容器**：进入 `/plugins` 展开 **「浏览器自动化」** 卡片，可直接修改 `live_view_url`、监听地址、端口、分辨率、NAT IP/域名、`shm_size` 以及 `agent-browser` 安全项（`allow_eval`、`allowed_domains`、`max_output`、`timeout` 等）；点击 **「保存配置」** 后，在卡片顶部点击 **「同步并启动 / 重启 / 停止」** 即可按最新配置管理 Neko 容器，也可展开 **「反代配置」** 查看适配当前端口的反代片段。
- **打开可视化界面**：
  - 在 `/plugins` 填写 `live_view_url` 并保存后，**无需重启应用**，左侧栏 **「插件页面」** 分组下会立即出现 **「可视化浏览器」**（`/browser`）入口；清空 `live_view_url` 则自动隐藏。
  - 也可在 `browser` 插件卡片顶部直接点击 **「打开可视化界面」** 跳转，或在 `/browser` 页面点击 **「新标签打开」** 全屏接管。

---

## 3. 防火墙与 HTTPS 反代

- **WebRTC 媒体端口（必填）**：在云服务器安全组 / 防火墙放行单个 MUX 端口（默认 `52000/udp` 与 `52000/tcp`）。
- **切勿对外放行 `9222`**：CDP 端口始终仅绑定 `127.0.0.1:9222`，供本机 `agent-browser` 使用。
- **HTTPS 反代**：执行 `tentacle browser proxy-config` 或在 `/plugins` 卡片点击 **「反代配置」** 即可查看 Nginx、Caddy 与 Tailscale Serve 现成配置（包含 WebSocket 升级头）。若 Tentacle WebUI 部署在 HTTPS 下，建议 `live_view_url` 也使用 HTTPS 地址，以便在 `/browser` 中直接内嵌加载（避免浏览器 Mixed Content 拦截）。

---

## 4. 常用运维命令

```bash
tentacle browser status         # 查看容器状态与本机 CDP 探活结果
tentacle browser stop           # 暂时停止容器释放内存（保留 profile 登录态）
tentacle browser start          # 重新启动容器
tentacle browser restart        # 重启容器
tentacle browser logs -f        # 实时查看容器日志
tentacle browser credentials    # 查看自动生成的 Neko 管理员/普通账号密码
tentacle browser proxy-config   # 打印 Nginx/Caddy/Tailscale 反代配置片段
tentacle browser uninstall      # 停止并移除容器（保留 profile/，加 --purge 彻底清空）
```

> **小内存服务器（如 2GB RAM）提示**：建议在宿主机挂载至少 2GB Swap（脚本检测到 <= 2.5GB 内存时会自动将 `shm_size` 设为 `512m`）；不需要使用浏览器时可在 WebUI 点击「停止」或执行 `tentacle browser stop` 释放内存。
