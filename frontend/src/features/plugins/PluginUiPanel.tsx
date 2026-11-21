import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Plug, RefreshCw } from "lucide-react";
import { apiFetch } from "../../api/client";
import { wsClient } from "../../api/ws";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { toast } from "../../components/ui/Toast";
import { useThemeStore } from "../../stores/themeStore";
import { useLayoutStore } from "../workbench/layoutStore";
import {
  collectHostThemeSnapshot,
  executePluginAssetFetch,
  executePluginExtFetch,
  injectBridgeIntoHtml,
  PLUGIN_UI_CHANNEL,
  type PluginBridgeAssetRequest,
  type PluginBridgeFetchRequest,
} from "./pluginUiBridge";

interface PluginUiDocumentPayload {
  plugin_id: string;
  name: string;
  version: string;
  title: string;
  description: string;
  placement: "sidebar" | "plugins_only";
  has_routes: boolean;
  html: string;
}

function makeBridgeId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `bridge_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

export function PluginUiPanel({ pluginId }: { pluginId: string }) {
  const resolvedTheme = useThemeStore((s) => s.resolved);
  const openPanel = useLayoutStore((s) => s.openPanel);
  const askChatPrompt = useLayoutStore((s) => s.askChatPrompt);

  const [doc, setDoc] = useState<PluginUiDocumentPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const bridgeId = useMemo(
    () => makeBridgeId(),
    // 每次切换插件或手动重载生成新的 bridgeId，隔离旧消息。
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [pluginId, reloadNonce],
  );

  const loadDocument = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const payload = await apiFetch<PluginUiDocumentPayload>(
        `/api/plugins/${encodeURIComponent(pluginId)}/ui/document`,
      );
      setDoc(payload);
    } catch (err) {
      setDoc(null);
      setError(err instanceof Error ? err.message : "加载插件页面失败");
    } finally {
      setLoading(false);
    }
  }, [pluginId]);

  useEffect(() => {
    void loadDocument();
  }, [loadDocument, reloadNonce]);

  const initialThemeSnapshot = useMemo(
    () => collectHostThemeSnapshot(useThemeStore.getState().resolved),
    // 仅在文档或 bridge 重建时取初始快照；后续主题变化通过 postMessage 实时同步。
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [doc?.html, bridgeId],
  );

  const srcDoc = useMemo(() => {
    if (!doc) return "";
    return injectBridgeIntoHtml(doc.html, {
      pluginId,
      bridgeId,
      themeSnapshot: initialThemeSnapshot,
    });
  }, [doc, pluginId, bridgeId, initialThemeSnapshot]);

  // 实时将宿主深浅主题及 CSS 变量同步进沙箱 iframe。
  useEffect(() => {
    const win = iframeRef.current?.contentWindow;
    if (!win || !doc) return;
    const snap = collectHostThemeSnapshot(resolvedTheme);
    win.postMessage(
      {
        channel: PLUGIN_UI_CHANNEL,
        bridgeId,
        kind: "theme",
        theme: snap.theme,
        cssVars: snap.cssVars,
      },
      "*",
    );
  }, [resolvedTheme, bridgeId, doc]);

  // 将该插件后端通过 ctx.web.emit_ui_event(...) 推送的 WS 事件转发给沙箱 iframe。
  useEffect(() => {
    if (!doc) return;
    return wsClient.on("plugin.ui_event", (envelope) => {
      const data = envelope.data as { plugin_id?: unknown; event?: unknown; payload?: unknown } | null;
      if (!data || data.plugin_id !== pluginId) return;
      const win = iframeRef.current?.contentWindow;
      if (!win) return;
      win.postMessage(
        {
          channel: PLUGIN_UI_CHANNEL,
          bridgeId,
          kind: "event",
          event: String(data.event ?? ""),
          payload: data.payload,
        },
        "*",
      );
    });
  }, [pluginId, bridgeId, doc]);

  // 接收并处理来自当前沙箱 iframe 的受限 RPC / 通知请求。
  useEffect(() => {
    let disposed = false;

    async function handleMessage(ev: MessageEvent) {
      const frameWindow = iframeRef.current?.contentWindow;
      if (!frameWindow || ev.source !== frameWindow) return;
      const msg = ev.data as {
        channel?: string;
        bridgeId?: string;
        kind?: string;
        id?: string;
        action?: string;
        payload?: Record<string, unknown>;
      } | null;
      if (!msg || msg.channel !== PLUGIN_UI_CHANNEL || msg.bridgeId !== bridgeId) return;

      if (msg.kind === "notify") {
        if (msg.action === "ui.toast") {
          const message = String(msg.payload?.message ?? "").slice(0, 200).trim();
          const variant = String(msg.payload?.variant ?? "info");
          if (message) {
            if (variant === "success" || variant === "ok") toast(message, "ok");
            else if (variant === "error") toast(message, "error");
            else toast(message, "info");
          }
        } else if (msg.action === "ui.askChat") {
          const prompt = String(msg.payload?.prompt ?? "").slice(0, 4000).trim();
          if (prompt) {
            askChatPrompt(prompt);
          }
        } else if (msg.action === "ui.openChat") {
          openPanel("chat");
        }
        return;
      }

      if (msg.kind !== "request" || !msg.id) return;
      const reqId = msg.id;

      const reply = (ok: boolean, result?: unknown, errText?: string) => {
        if (disposed) return;
        const win = iframeRef.current?.contentWindow;
        if (!win) return;
        win.postMessage(
          {
            channel: PLUGIN_UI_CHANNEL,
            bridgeId,
            kind: "response",
            id: reqId,
            ok,
            result,
            error: errText,
          },
          "*",
        );
      };

      try {
        if (msg.action === "api.fetch") {
          const res = await executePluginExtFetch(
            pluginId,
            (msg.payload ?? {}) as unknown as PluginBridgeFetchRequest,
          );
          reply(true, res);
        } else if (msg.action === "ui.asset") {
          const asset = await executePluginAssetFetch(
            pluginId,
            (msg.payload ?? {}) as unknown as PluginBridgeAssetRequest,
          );
          reply(true, asset);
        } else {
          reply(false, undefined, `不支持的桥接动作: ${String(msg.action ?? "")}`);
        }
      } catch (err) {
        reply(false, undefined, err instanceof Error ? err.message : "请求处理失败");
      }
    }

    window.addEventListener("message", handleMessage);
    return () => {
      disposed = true;
      window.removeEventListener("message", handleMessage);
    };
  }, [pluginId, bridgeId, askChatPrompt, openPanel]);

  return (
    <div
      data-testid="plugin-ui-panel"
      data-plugin-id={pluginId}
      className="flex h-full flex-col bg-bg text-fg"
    >
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border-soft bg-surface/75 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-xs font-semibold text-fg">
            {doc?.title || pluginId}
          </span>
          {doc?.version && <Badge variant="default">v{doc.version}</Badge>}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setReloadNonce((n) => n + 1)}
            disabled={loading}
            title="重新加载插件页面"
          >
            <RefreshCw className={`mr-1 h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            刷新
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => openPanel("plugins")}
            title="前往插件管理页"
          >
            <Plug className="mr-1 h-3.5 w-3.5" />
            插件管理
          </Button>
        </div>
      </div>

      <div className="relative min-h-0 flex-1 bg-bg">
        {loading && !doc && (
          <div className="flex h-full items-center justify-center text-xs text-fg-dim">
            正在加载插件沙箱页面…
          </div>
        )}

        {error && !loading && (
          <div className="flex h-full items-center justify-center p-6">
            <div className="max-w-md space-y-3 rounded-2xl border border-danger/30 bg-surface p-5 text-center">
              <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-xl bg-danger-soft text-danger">
                <AlertTriangle className="h-5 w-5" />
              </div>
              <div className="space-y-1">
                <p className="text-sm font-medium text-fg">无法打开插件页面</p>
                <p className="text-xs text-fg-dim break-words">{error}</p>
              </div>
              <div className="flex items-center justify-center gap-2">
                <Button size="sm" variant="secondary" onClick={() => setReloadNonce((n) => n + 1)}>
                  重试
                </Button>
                <Button size="sm" variant="ghost" onClick={() => openPanel("plugins")}>
                  前往插件管理
                </Button>
              </div>
            </div>
          </div>
        )}

        {doc && !error && (
          <iframe
            key={bridgeId}
            ref={iframeRef}
            data-testid="plugin-ui-iframe"
            title={doc.title || pluginId}
            sandbox="allow-scripts allow-forms allow-downloads"
            referrerPolicy="no-referrer"
            srcDoc={srcDoc}
            className="h-full w-full border-0 bg-bg"
          />
        )}
      </div>
    </div>
  );
}
