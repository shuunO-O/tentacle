# 远程 Chrome（可选）

此功能让 Tentacle 在服务器运行浏览器控制工具，在自己的 Windows Chrome 上打开页面。Windows 电脑、Chrome 和 Tailscale 必须在线。请只通过 tailnet 暴露调试端口，并限制只有服务器可访问；不要映射到公网。

## Windows 电脑

安装 Tailscale，并为 Chrome 使用单独的 profile 和回环调试端口。PowerShell 示例：

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --remote-debugging-address=127.0.0.1 `
  "--user-data-dir=$env:LOCALAPPDATA\Tentacle\ChromeProfile"
```

通过 Tailscale Serve 或等效的 tailnet 内部转发，让服务器连接该端口。Chrome 136 及以后需指定非默认 profile 才能启用远程调试。详见 [Chrome 说明](https://developer.chrome.com/blog/remote-debugging-port) 与 [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)。

## Tentacle 配置

在 WebUI `/plugins` 的 `browser` 插件配置中填写，或在 `config.yaml` 的 `plugins.items.browser.config` 段填写 Windows 电脑的 Tailscale 地址和实际转发端口：

```yaml
plugins:
  items:
    browser:
      enabled: true
      trusted: true
      config:
        cdp_discovery_url: http://WINDOWS_TAILSCALE_HOST:PORT/json/version
        cdp_discovery_timeout: 8
        session: tentacle
        timeout: 60
```

`cdp_discovery_url` 非空会启用远程模式；为空时使用本地浏览器模式。远程连接失败会报错，不会改连服务器上的 Chrome。登录状态来自 Windows 独立 profile；服务器端控制浏览器可以访问该 profile 已登录的网站，应只授权可信的模型和工具使用。

连接详情见 `plugins/bundled/browser/plugin.py`，浏览器插件配置入口见 WebUI `/plugins`（或[应用配置说明](CONFIGURATION.md)）。
