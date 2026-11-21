import { useCallback, useEffect, useState } from "react";
import { Globe, Play, RefreshCw, RotateCw, Square, Terminal } from "lucide-react";
import { apiFetch } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { toast } from "../../components/ui/Toast";
import { useLayoutStore } from "../workbench/layoutStore";

export interface BrowserNekoStatus {
  ok: boolean;
  available: boolean;
  control_mode?: string;
  docker_available?: boolean;
  installed?: boolean;
  container_name?: string;
  container_state?: string;
  running?: boolean;
  cdp_port?: number;
  cdp_url?: string;
  cdp_reachable?: boolean;
  browser_version?: string;
  http_listen?: string;
  mux_port?: number;
  nat_ip?: string;
  screen?: string;
  install_dir?: string;
  live_view_url?: string;
  proxy_config?: string;
  setup_hint?: string;
}

export function BrowserNekoControl({ hasUnsavedChanges = false }: { hasUnsavedChanges?: boolean }) {
  const [status, setStatus] = useState<BrowserNekoStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [showProxy, setShowProxy] = useState(false);

  const loadStatus = useCallback(async (opts?: { silent?: boolean }) => {
    setLoading(true);
    try {
      const data = await apiFetch<BrowserNekoStatus>("/api/plugins/ext/browser/neko/status");
      setStatus(data);
    } catch (err) {
      if (!opts?.silent) {
        toast(err instanceof Error ? err.message : "读取 Neko 状态失败", "error");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus({ silent: true });
  }, [loadStatus]);

  const runAction = async (action: "start" | "stop" | "restart") => {
    setBusyAction(action);
    try {
      const data = await apiFetch<BrowserNekoStatus>("/api/plugins/ext/browser/neko/action", {
        method: "POST",
        body: JSON.stringify({ action }),
      });
      setStatus(data);
      const label = action === "start" ? "已按最新配置启动 Neko" : action === "restart" ? "已按最新配置重启 Neko" : "已停止 Neko 容器";
      toast(label, "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "执行 Neko 操作失败", "error");
    } finally {
      setBusyAction(null);
    }
  };

  const canControl = Boolean(status?.available);
  const isBusy = busyAction !== null;

  return (
    <div
      data-testid="browser-neko-control"
      className="space-y-3 rounded-xl border border-border-soft bg-bg/60 p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold text-fg">Docker Neko 容器管理</span>
          {loading && !status ? (
            <Badge variant="default">检测中…</Badge>
          ) : status?.running ? (
            <Badge variant="ok">运行中</Badge>
          ) : (
            <Badge variant="default">{status?.container_state === "missing" ? "未创建" : "已停止"}</Badge>
          )}
          {status && (
            status.cdp_reachable ? (
              <Badge variant="ok">CDP 在线</Badge>
            ) : (
              <Badge variant="warn">CDP 离线</Badge>
            )
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            size="sm"
            variant="primary"
            disabled={!canControl || isBusy}
            onClick={() => void runAction("start")}
            title="根据当前已保存的插件配置同步 docker-compose.yaml 并启动 Neko"
          >
            <Play className="mr-1 h-3 w-3" />
            {busyAction === "start" ? "启动中…" : "同步并启动"}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={!canControl || isBusy}
            onClick={() => void runAction("restart")}
          >
            <RotateCw className={`mr-1 h-3 w-3 ${busyAction === "restart" ? "animate-spin" : ""}`} />
            {busyAction === "restart" ? "重启中…" : "重启"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={!canControl || isBusy || !status?.running}
            onClick={() => void runAction("stop")}
          >
            <Square className="mr-1 h-3 w-3" />
            {busyAction === "stop" ? "停止中…" : "停止"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={loading || isBusy}
            onClick={() => void loadStatus()}
            title="刷新容器与 CDP 状态"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          </Button>
          {status?.live_view_url && (
            <Button
              size="sm"
              variant="secondary"
              onClick={() => useLayoutStore.getState().openPanel("browser")}
              title="前往可视化浏览器面板查看并接管画面"
            >
              <Globe className="mr-1 h-3 w-3" />
              打开可视化界面
            </Button>
          )}
          {status?.proxy_config && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setShowProxy((v) => !v)}
            >
              <Terminal className="mr-1 h-3 w-3" />
              {showProxy ? "收起反代配置" : "反代配置"}
            </Button>
          )}
        </div>
      </div>

      {hasUnsavedChanges && (
        <p className="text-[11px] text-warn">
          检测到配置表单有未保存修改：请先点击下方「保存配置」，再点击「同步并启动」或「重启」使新参数生效。
        </p>
      )}

      {status && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[11px] text-fg-dim">
          {status.http_listen && <span>Web: {status.http_listen}</span>}
          {status.cdp_port && <span>CDP: 127.0.0.1:{status.cdp_port}</span>}
          {status.mux_port && <span>WebRTC MUX: {status.mux_port}/udp+tcp</span>}
          {status.screen && <span>分辨率: {status.screen}</span>}
          {status.browser_version && <span>{status.browser_version}</span>}
        </div>
      )}

      {status && !status.available && status.setup_hint && (
        <div className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[11px] leading-relaxed text-fg-dim">
          <p>{status.setup_hint}</p>
          <p className="mt-1 font-mono text-fg">sudo tentacle browser install --install-docker</p>
        </div>
      )}

      <p className="text-[11px] text-fg-faint">
        Neko 登录密码仅保存在宿主机 0600 权限的 .env 中，不在网页回显；可在服务器终端执行 <code className="font-mono">tentacle browser credentials</code> 查看。
      </p>

      {showProxy && status?.proxy_config && (
        <pre className="max-h-56 overflow-auto rounded-lg border border-border-soft bg-code-bg p-2.5 font-mono text-[11px] leading-relaxed text-fg-dim">
          {status.proxy_config}
        </pre>
      )}
    </div>
  );
}
