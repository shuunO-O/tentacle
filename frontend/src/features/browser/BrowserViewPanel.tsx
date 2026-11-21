import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  ExternalLink,
  Globe,
  MessageSquare,
  Plug,
  RefreshCw,
} from "lucide-react";
import { apiFetch } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { useLayoutStore } from "../workbench/layoutStore";

interface BrowserLiveViewStatus {
  configured: boolean;
  live_view_url: string;
  url_error?: string | null;
  cdp_port?: number;
  cdp_configured: boolean;
  cdp_reachable: boolean;
  browser_version?: string;
  mode?: "neko";
}

function isSafeHttpUrl(raw: string): boolean {
  try {
    const parsed = new URL(raw);
    return (parsed.protocol === "http:" || parsed.protocol === "https:") && !parsed.username && !parsed.password;
  } catch {
    return false;
  }
}

export function BrowserViewPanel() {
  const openPanel = useLayoutStore((s) => s.openPanel);
  const summary = useLayoutStore((s) => s.browserLiveView);

  const [status, setStatus] = useState<BrowserLiveViewStatus | null>(() =>
    summary
      ? {
          configured: Boolean(summary.live_view_url),
          live_view_url: summary.live_view_url,
          cdp_configured: Boolean(summary.cdp_configured),
          cdp_reachable: false,
          mode: summary.mode,
        }
      : null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [frameKey, setFrameKey] = useState(0);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<BrowserLiveViewStatus>("/api/plugins/ext/browser/live-view");
      setStatus(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取浏览器状态失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const rawUrl = (status?.live_view_url || summary?.live_view_url || "").trim();
  const safeUrl = rawUrl && isSafeHttpUrl(rawUrl) ? rawUrl : "";
  const isMixedContent =
    typeof window !== "undefined" &&
    window.location.protocol === "https:" &&
    safeUrl.startsWith("http://");

  const openExternal = () => {
    if (!safeUrl) return;
    window.open(safeUrl, "_blank", "noopener,noreferrer");
  };

  return (
    <div data-testid="browser-view-panel" className="flex h-full flex-col bg-bg text-fg">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border-soft bg-surface/75 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <Globe className="h-4 w-4 shrink-0 text-accent" />
          <span className="truncate text-xs font-semibold text-fg">可视化浏览器</span>
          {status?.cdp_reachable ? (
            <Badge variant="ok">CDP 在线</Badge>
          ) : (
            <Badge variant="warn">{loading ? "检测中…" : "CDP 离线"}</Badge>
          )}
          {status?.browser_version && (
            <span className="hidden truncate font-mono text-[11px] text-fg-dim sm:inline">
              {status.browser_version}
            </span>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setFrameKey((k) => k + 1);
              void loadStatus();
            }}
            disabled={loading}
            title="刷新画面与 CDP 连接状态"
          >
            <RefreshCw className={`mr-1 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            刷新
          </Button>
          {safeUrl && (
            <Button
              size="sm"
              variant="secondary"
              onClick={openExternal}
              title="在新标签页全屏打开可视化浏览器"
            >
              <ExternalLink className="mr-1 h-3.5 w-3.5" />
              新标签打开
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => openPanel("chat")}
            title="切回主对话页面"
          >
            <MessageSquare className="mr-1 h-3.5 w-3.5" />
            回到会话
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => openPanel("plugins")}
            title="前往插件配置"
          >
            <Plug className="mr-1 h-3.5 w-3.5" />
            插件配置
          </Button>
        </div>
      </div>

      <div className="relative min-h-0 flex-1 bg-bg">
        {!safeUrl ? (
          <div className="flex h-full items-center justify-center p-6">
            <div className="max-w-md space-y-3 rounded-2xl border border-border bg-surface p-5 text-center">
              <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-xl bg-accent-soft text-accent">
                <Globe className="h-5 w-5" />
              </div>
              <div className="space-y-1">
                <p className="text-sm font-medium text-fg">尚未配置可视化浏览器地址</p>
                <p className="text-xs leading-relaxed text-fg-dim">
                  {error ||
                    status?.url_error ||
                    "请在插件管理中展开「浏览器自动化」配置项并填写 live_view_url（例如已部署的 Docker Neko HTTPS 地址），保存并重启后即可在此内嵌查看或新标签全屏操作。"}
                </p>
              </div>
              <div className="flex items-center justify-center gap-2">
                <Button size="sm" variant="primary" onClick={() => openPanel("plugins")}>
                  前往插件配置
                </Button>
                <Button size="sm" variant="ghost" onClick={() => openPanel("chat")}>
                  回到会话
                </Button>
              </div>
            </div>
          </div>
        ) : isMixedContent ? (
          <div className="flex h-full items-center justify-center p-6">
            <div className="max-w-md space-y-3 rounded-2xl border border-warn/40 bg-surface p-5 text-center">
              <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-xl bg-warn/15 text-warn">
                <AlertTriangle className="h-5 w-5" />
              </div>
              <div className="space-y-1">
                <p className="text-sm font-medium text-fg">当前 HTTPS 页面无法直接内嵌 HTTP 地址</p>
                <p className="text-xs leading-relaxed text-fg-dim">
                  浏览器安全策略会阻止在 HTTPS 页面中内嵌明文 HTTP iframe。请点击下方在新标签页打开，或为 Neko 配置 HTTPS 反代后再填入 <code className="font-mono">https://</code> 地址。
                </p>
              </div>
              <div className="flex items-center justify-center gap-2">
                <Button size="sm" variant="primary" onClick={openExternal}>
                  <ExternalLink className="mr-1 h-3.5 w-3.5" />
                  新标签打开
                </Button>
                <Button size="sm" variant="ghost" onClick={() => openPanel("chat")}>
                  回到会话
                </Button>
              </div>
            </div>
          </div>
        ) : (
          <iframe
            key={`${safeUrl}:${frameKey}`}
            data-testid="browser-live-iframe"
            title="可视化浏览器"
            src={safeUrl}
            allow="clipboard-read; clipboard-write; fullscreen; autoplay"
            referrerPolicy="no-referrer"
            className="h-full w-full border-0 bg-bg"
          />
        )}
      </div>
    </div>
  );
}
