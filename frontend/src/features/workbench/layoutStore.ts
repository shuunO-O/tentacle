import { create } from "zustand";
import { apiFetch } from "../../api/client";
import {
  wsClient,
  type WsDedupProgressData,
  type WsMaintenanceUpdatedData,
  type WsSubagentUpdatedData,
} from "../../api/ws";
import { getPanelDef, panelFromPathname, type PanelId } from "./panels";

export type DrawerTab = "terminal" | "activity";

export interface DomainStatusSnapshot {
  runningSubagents: number;
  codingEnabled: boolean;
  dedupRunning: boolean;
  updateAvailable: boolean;
  latestEventNote: string | null;
}

function syncSubagentSnapshot(opts: {
  tasks?: Array<{ status?: unknown } | null | undefined>;
  codingEnabled?: boolean;
  eventNote?: string;
}): void {
  useLayoutStore.setState((s) => ({
    domainStatus: {
      ...s.domainStatus,
      runningSubagents: opts.tasks
        ? opts.tasks.filter(
            (t) => t && ["queued", "running", "stopping"].includes(String(t.status || "")),
          ).length
        : s.domainStatus.runningSubagents,
      codingEnabled:
        typeof opts.codingEnabled === "boolean"
          ? opts.codingEnabled
          : s.domainStatus.codingEnabled,
      latestEventNote: opts.eventNote
        ? `子代理事件: ${opts.eventNote}`
        : s.domainStatus.latestEventNote,
    },
  }));
}

const SIDEBAR_STORAGE_KEY = "tentacle-sidebar-collapsed";

function readInitialSidebarCollapsed(): boolean {
  if (typeof window === "undefined") return false;
  try {
    return window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function writeSidebarCollapsed(collapsed: boolean): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? "1" : "0");
  } catch {
    // Ignore storage errors in restricted contexts.
  }
}

interface LayoutState {
  primaryPanel: PanelId;
  secondaryPanel: PanelId | null;
  focusedPane: "primary" | "secondary";
  sidebarCollapsed: boolean;
  drawerOpen: boolean;
  drawerTab: DrawerTab;
  commandPaletteOpen: boolean;
  mobileNavOpen: boolean;

  pendingChatPrompt: string | null;
  graphFocusConceptId: string | null;
  domainStatus: DomainStatusSnapshot;

  openPanel: (id: PanelId, opts?: { inSplit?: boolean; skipPushState?: boolean }) => void;
  toggleSidebar: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSplitWith: (id?: PanelId) => void;
  closeSplit: () => void;
  swapPanes: () => void;
  setFocusedPane: (pane: "primary" | "secondary") => void;
  toggleDrawer: (tab?: DrawerTab) => void;
  setCommandPaletteOpen: (open: boolean) => void;
  setMobileNavOpen: (open: boolean) => void;
  askChatPrompt: (text: string) => void;
  consumeChatPrompt: () => string | null;
  focusConceptInGraph: (conceptId: string, opts?: { split?: boolean }) => void;
  clearGraphFocusConcept: () => void;
}

function syncBrowserUrl(panel: PanelId, skipPushState?: boolean): void {
  if (typeof window === "undefined" || skipPushState) return;
  const targetRoute = getPanelDef(panel).route;
  if (window.location.pathname !== targetRoute) {
    try {
      window.history.pushState({ panel }, "", targetRoute);
    } catch {
      // Ignore restricted history API in sandboxed frames.
    }
  }
}

const initialPanel: PanelId =
  typeof window !== "undefined" ? panelFromPathname(window.location.pathname) : "chat";

export const useLayoutStore = create<LayoutState>((set, get) => ({
  primaryPanel: initialPanel,
  secondaryPanel: null,
  focusedPane: "primary",
  sidebarCollapsed: readInitialSidebarCollapsed(),
  drawerOpen: false,
  drawerTab: "terminal",
  commandPaletteOpen: false,
  mobileNavOpen: false,

  pendingChatPrompt: null,
  graphFocusConceptId: null,
  domainStatus: {
    runningSubagents: 0,
    codingEnabled: false,
    dedupRunning: false,
    updateAvailable: false,
    latestEventNote: null,
  },

  openPanel: (id, opts = {}) => {
    const state = get();

    if (opts.inSplit) {
      if (state.primaryPanel === id) {
        return;
      }
      set({
        secondaryPanel: id,
        focusedPane: "secondary",
        mobileNavOpen: false,
      });
      return;
    }

    syncBrowserUrl(id, opts.skipPushState);
    const nextSecondary =
      state.secondaryPanel === id
        ? state.primaryPanel !== id
          ? state.primaryPanel
          : null
        : state.secondaryPanel;
    set({
      primaryPanel: id,
      secondaryPanel: nextSecondary,
      focusedPane: "primary",
      mobileNavOpen: false,
    });
  },

  toggleSidebar: () => {
    const next = !get().sidebarCollapsed;
    writeSidebarCollapsed(next);
    set({ sidebarCollapsed: next });
  },

  setSidebarCollapsed: (collapsed) => {
    writeSidebarCollapsed(collapsed);
    set({ sidebarCollapsed: collapsed });
  },

  toggleSplitWith: (id) => {
    const state = get();
    if (state.secondaryPanel && !id) {
      set({ secondaryPanel: null, focusedPane: "primary" });
      return;
    }
    const target =
      id || (state.primaryPanel === "chat" ? "graph" : "chat");
    set({
      secondaryPanel: target,
      focusedPane: "secondary",
    });
  },

  closeSplit: () => set({ secondaryPanel: null, focusedPane: "primary" }),

  swapPanes: () => {
    const { primaryPanel, secondaryPanel } = get();
    if (!secondaryPanel) return;
    syncBrowserUrl(secondaryPanel);
    set({
      primaryPanel: secondaryPanel,
      secondaryPanel: primaryPanel,
    });
  },

  setFocusedPane: (pane) => set({ focusedPane: pane }),

  toggleDrawer: (tab) => {
    const state = get();
    if (tab && (!state.drawerOpen || state.drawerTab !== tab)) {
      set({ drawerOpen: true, drawerTab: tab });
      return;
    }
    set({ drawerOpen: !state.drawerOpen });
  },

  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
  setMobileNavOpen: (open) => set({ mobileNavOpen: open }),

  askChatPrompt: (text) => {
    const state = get();
    const inSecondary = state.secondaryPanel === "chat";
    if (!inSecondary && state.primaryPanel !== "chat") {
      state.openPanel("chat");
    }
    set({ pendingChatPrompt: text });
  },

  consumeChatPrompt: () => {
    const prompt = get().pendingChatPrompt;
    if (prompt !== null) {
      set({ pendingChatPrompt: null });
    }
    return prompt;
  },

  focusConceptInGraph: (conceptId, opts = {}) => {
    set({ graphFocusConceptId: conceptId });
    get().openPanel("graph", { inSplit: Boolean(opts.split) });
  },

  clearGraphFocusConcept: () => set({ graphFocusConceptId: null }),
}));

export async function refreshSubagentStatus(): Promise<void> {
  try {
    const res = await apiFetch<{
      coding_enabled?: boolean;
      tasks?: Array<{ status?: unknown }>;
    }>("/api/subagents?limit=20");
    if (Array.isArray(res?.tasks) || typeof res?.coding_enabled === "boolean") {
      syncSubagentSnapshot({
        tasks: Array.isArray(res.tasks) ? res.tasks : undefined,
        codingEnabled: res.coding_enabled,
      });
    }
  } catch {
    // Ignore transient boot fetch errors; SubagentsPanel & WS events handle retries cleanly.
  }
}

// Keep rail badges updated live via WS events.
wsClient.on("subagent.updated", (ev) => {
  const data = (ev.data || {}) as Partial<WsSubagentUpdatedData>;
  const tasks = Array.isArray(data.tasks) && data.tasks.length ? data.tasks : undefined;
  if (tasks || typeof data.coding_enabled === "boolean" || data.event) {
    syncSubagentSnapshot({
      tasks,
      codingEnabled: data.coding_enabled ?? undefined,
      eventNote: data.event ?? undefined,
    });
  }
});

wsClient.on("dedup.progress", (ev) => {
  const data = (ev.data || {}) as Partial<WsDedupProgressData>;
  const runStatus = String((data.run as { status?: string } | null)?.status || "");
  useLayoutStore.setState((s) => ({
    domainStatus: {
      ...s.domainStatus,
      dedupRunning: runStatus === "running" || runStatus === "scanning",
      latestEventNote: data.event ? `实体去重: ${data.event}` : s.domainStatus.latestEventNote,
    },
  }));
});

wsClient.on("maintenance.updated", (ev) => {
  const data = (ev.data || {}) as Partial<WsMaintenanceUpdatedData>;
  useLayoutStore.setState((s) => ({
    domainStatus: {
      ...s.domainStatus,
      updateAvailable: Boolean(data.can_update),
    },
  }));
});

if (typeof window !== "undefined") {
  window.addEventListener("popstate", () => {
    const id = panelFromPathname(window.location.pathname);
    useLayoutStore.getState().openPanel(id, { skipPushState: true });
  });
}
