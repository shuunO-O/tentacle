import { lazy, Suspense } from "react";
import { ChatPanel } from "../chat/ChatPanel";
import { pluginIdFromPanelId, type PanelId } from "./panels";

const MemoryGraphPanel = lazy(() =>
  import("../memory-graph/MemoryGraphPanel").then((m) => ({ default: m.MemoryGraphPanel })),
);
const BrowserViewPanel = lazy(() =>
  import("../browser/BrowserViewPanel").then((m) => ({ default: m.BrowserViewPanel })),
);
const SubagentsPanel = lazy(() =>
  import("../subagents/SubagentsPanel").then((m) => ({ default: m.SubagentsPanel })),
);
const DedupPanel = lazy(() =>
  import("../dedup/DedupPanel").then((m) => ({ default: m.DedupPanel })),
);
const AdminPanel = lazy(() =>
  import("../admin/AdminPanel").then((m) => ({ default: m.AdminPanel })),
);
const ConfigurationPanel = lazy(() =>
  import("../config/ConfigurationPanel").then((m) => ({ default: m.ConfigurationPanel })),
);
const PluginsPanel = lazy(() =>
  import("../plugins/PluginsPanel").then((m) => ({ default: m.PluginsPanel })),
);
const PluginUiPanel = lazy(() =>
  import("../plugins/PluginUiPanel").then((m) => ({ default: m.PluginUiPanel })),
);
const UsagePanel = lazy(() =>
  import("../usage/UsagePanel").then((m) => ({ default: m.UsagePanel })),
);
const StatusPanel = lazy(() =>
  import("../status/StatusPanel").then((m) => ({ default: m.StatusPanel })),
);
const TerminalPanel = lazy(() =>
  import("../terminal/TerminalPanel").then((m) => ({ default: m.TerminalPanel })),
);
const SettingsPanel = lazy(() =>
  import("../settings/SettingsPanel").then((m) => ({ default: m.SettingsPanel })),
);

function PanelFallback() {
  return (
    <div className="flex h-full items-center justify-center bg-bg text-xs text-fg-dim">
      正在加载工作台面板…
    </div>
  );
}

export function PanelView({ panelId }: { panelId: PanelId }) {
  if (panelId === "chat") {
    return <ChatPanel />;
  }

  const pluginUiId = pluginIdFromPanelId(panelId);

  return (
    <Suspense fallback={<PanelFallback />}>
      {pluginUiId && <PluginUiPanel pluginId={pluginUiId} />}
      {panelId === "browser" && <BrowserViewPanel />}
      {panelId === "graph" && <MemoryGraphPanel />}
      {panelId === "subagents" && <SubagentsPanel />}
      {panelId === "dedup" && <DedupPanel />}
      {panelId === "admin" && <AdminPanel />}
      {panelId === "config" && <ConfigurationPanel />}
      {panelId === "plugins" && <PluginsPanel />}
      {panelId === "usage" && <UsagePanel />}
      {panelId === "status" && <StatusPanel />}
      {panelId === "terminal" && <TerminalPanel />}
      {panelId === "settings" && <SettingsPanel />}
    </Suspense>
  );
}
