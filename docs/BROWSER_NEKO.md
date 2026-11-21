# Docker Neko 可视化浏览器部署指南

通过 Docker 运行 [`m1k1o/neko`](https://github.com/m1k1o/neko)（Apache-2.0，Chromium + Xvfb + WebRTC），可在一台 Linux 服务器上同时满足：

- **Agent 自动化**：Chromium 的 CDP 调试端口仅绑定宿主机回环 `127.0.0.1:9222`，由 Tentacle 内置 `browser` 插件（`agent-browser`）直接驱动；
- **人类丝滑接管**：通过 Neko 的 WebRTC 视频流（默认 `1280x720@24`、单端口 TCP+UDP MUX）随时接管登录或人机验证，无 DPI 缩放错位；
- **登录态持久化**：挂载宿主机独立 `profile/` 目录，跨重启保留 Cookie 与会话。

---

## 1. 一键安装

在已安装 Tentacle 的 Linux 服务器上执行（如尚未安装 Docker，加上 `--install-docker` 会自动调用 Docker 官方脚本安装并启动服务）：

```bash
# 仅监听本机 127.0.0.1:8080（推荐，挂载到现有 Nginx/Caddy/Tailscale HTTPS 反代下）
sudo tentacle browser install \
  --install-docker \
  --nat-ip <你的公网IP或Tailscale_IP> \
  --live-view-url https://browser.example.com
```

如不想通过反代，也可直接指定监听地址（靠 Neko 自动生成的随机密码保护）：

```bash
sudo tentacle browser install \
  --bind 0.0.0.0 \
  --http-port 8080 \
  --nat-ip <你的公网IP或Tailscale_IP> \
  --live-view-url http://<你的IP>:8080
```

安装脚本（[scripts/browser_neko.py](../scripts/browser_neko.py)）会自动完成：
1. 创建运行目录（默认 `/var/lib/tentacle-browser`）与持久化 `profile/` 目录；
2. 生成随机高强度 Neko 登录密码并写入 `0600` 权限的 `.env`（不会写入 Tentacle `config.yaml`）；
3. 生成开启本机 CDP 且关闭访客模式的 `chromium.conf` 与 `docker-compose.yaml`；
4. 自动将 `neko_*` 参数（以及可选 `live_view_url`）同步到 Tentacle 的 `config.yaml`（并自动清理旧版残留字段）；
5. 在 Linux systemd 主机上自动安装按需拉起的受控 Unix Socket（`tentacle-browser-neko.socket`，仅允许 `tentacle` 应用账号通过 `SO_PEERCRED` 调用白名单启停动作，无需把应用账号加入 `docker` 组，空闲零内存开销）；
6. 拉取镜像、启动容器并探活本机 CDP 端点。

同步配置后，在 WebUI `/plugins` 点击重启应用或执行 `tentacle restart` 即可生效。**完成这一次初始化后，后续可以直接在 WebUI 的 `/plugins` →「浏览器自动化」里修改全部 Neko 参数（监听地址、端口、分辨率、NAT IP/域名、`live_view_url` 等），保存后直接点「同步并启动 / 重启 / 停止」管理容器，无需再进终端手改配置。**

---

## 2. 防火墙与 HTTPS 反代

- **WebRTC 媒体端口（必填）**：在云服务器安全组 / 防火墙放行单个 MUX 端口（默认 `52000/udp` 与 `52000/tcp`）。
- **切勿对外放行 `9222`**：CDP 端口默认仅绑定 `127.0.0.1:9222`，供本机 `agent-browser` 使用。
- **HTTPS 反代**：执行 `tentacle browser proxy-config` 即可查看适配当前端口的 Nginx、Caddy 与 Tailscale Serve 现成配置（包含 WebSocket 升级头）。若 Tentacle WebUI 部署在 HTTPS 下，建议 `live_view_url` 也使用 HTTPS 地址，这样可直接在 Tentacle 侧边栏「可视化浏览器」中内嵌打开。

---

## 3. 常用运维命令

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

> **小内存服务器（如 2GB RAM）提示**：建议在宿主机挂载至少 2GB Swap（脚本检测到 <= 2.5GB 内存时会自动将 `shm_size` 设为 `512m`）；不需要使用浏览器时可执行 `tentacle browser stop` 释放内存。
