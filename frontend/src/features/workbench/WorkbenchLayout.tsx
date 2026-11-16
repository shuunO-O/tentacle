import { lazy, Suspense } from "react";
import {
  ArrowLeftRight,
  Code2,
  Columns2,
  Command,
  ExternalLink,
  Menu,
  Moon,
  PanelBottom,
  PanelLeftClose,
  PanelLeftOpen,
  Sun,
  X,
} from "lucide-react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { Badge } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { Tooltip, TooltipProvider } from "../../components/ui/Tooltip";
import { useThemeStore } from "../../stores/themeStore";
import { useChatStore } from "../chat/chatStore";
import { openDshWorkspace } from "../subagents/dsh";
import { useLayoutStore } from "./layoutStore";
import {
  getPanelDef,
  PANEL_GROUPS,
  PANELS,
  useIsCompactLayout,
  type PanelId,
} from "./panels";
import { PanelView } from "./PanelView";

const LazyTerminalPanel = lazy(() =>
  import("../terminal/TerminalPanel").then((m) => ({ default: m.TerminalPanel })),
);

function TentacleEmblem({ size = 20 }: { size?: number }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="8" r="4.5" />
      <path d="M8 12c0 3-1.5 4-3 5M12 12.5c0 3.5-.5 5-1.5 6.5M16 12c0 3 1.5 4 3 5" />
    </svg>
  );
}

function PanelBadge({ id, collapsed = false }: { id: PanelId; collapsed?: boolean }) {
  const domainStatus = useLayoutStore((s) => s.domainStatus);
  if (id === "subagents" && domainStatus.runningSubagents > 0) {
    return (
      <span
        className={cn(
          "flex h-4 min-w-[16px] items-center justify-center rounded-full bg-accent px-1 text-[10px] font-semibold text-accent-fg",
          collapsed && "absolute right-1 top-1",
        )}
      >
        {domainStatus.runningSubagents}
      </span>
    );
  }
  if (id === "dedup" && domainStatus.dedupRunning) {
    return (
      <span
        className={cn(
          "h-2 w-2 rounded-full bg-warn animate-pulse",
          collapsed && "absolute right-1.5 top-1.5",
        )}
      />
    );
  }
  if (id === "status" && domainStatus.updateAvailable) {
    return (
      <span
        className={cn(
          "h-2 w-2 rounded-full bg-ok",
          collapsed && "absolute right-1.5 top-1.5",
        )}
      />
    );
  }
  return null;
}

function DrawerBody() {
  const drawerTab = useLayoutStore((s) => s.drawerTab);
  const toggleDrawer = useLayoutStore((s) => s.toggleDrawer);
  const latestNote = useLayoutStore((s) => s.domainStatus.latestEventNote);
  const openPanel = useLayoutStore((s) => s.openPanel);

  return (
    <div className="flex h-full flex-col border-t border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border-soft px-3 py-1.5">
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => toggleDrawer("terminal")}
            className={cn(
              "rounded-md px-2.5 py-1 text-xs font-medium",
              drawerTab === "terminal" ? "bg-elevated text-fg" : "text-fg-dim hover:text-fg",
            )}
          >
            终端控制台
          </button>
          <button
            type="button"
            onClick={() => toggleDrawer("activity")}
            className={cn(
              "rounded-md px-2.5 py-1 text-xs font-medium",
              drawerTab === "activity" ? "bg-elevated text-fg" : "text-fg-dim hover:text-fg",
            )}
          >
            实时事件
          </button>
        </div>
        <div className="flex items-center gap-1">
          <Button
            size="sm"
            variant="ghost"
            onClick={() => openPanel("terminal")}
            title="在主区打开全屏终端"
          >
            最大化
          </Button>
          <button
            type="button"
            onClick={() => toggleDrawer()}
            aria-label="关闭底部抽屉"
            className="rounded-md p-1 text-fg-dim hover:bg-hover hover:text-fg"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto text-xs text-fg-dim">
        {drawerTab === "terminal" ? (
          <Suspense
            fallback={
              <div className="flex h-full items-center justify-center p-3 font-mono text-fg-dim">
                正在初始化只读终端…
              </div>
            }
          >
            <LazyTerminalPanel compactDrawer />
          </Suspense>
        ) : (
          <div className="space-y-1.5 p-3 font-mono">
            <div>{latestNote || "等待实时子代理、去重与系统状态事件…"}</div>
          </div>
        )}
      </div>
    </div>
  );
}

export function WorkbenchLayout() {
  const compact = useIsCompactLayout();
  const agentName = useChatStore((s) => s.settings.agent_name || "Tentacle");
  const wsStatus = useChatStore((s) => s.wsStatus);

  const primaryPanel = useLayoutStore((s) => s.primaryPanel);
  const secondaryPanel = useLayoutStore((s) => s.secondaryPanel);
  const codingEnabled = useLayoutStore((s) => s.domainStatus.codingEnabled);
  const sidebarCollapsed = useLayoutStore((s) => s.sidebarCollapsed);
  const drawerOpen = useLayoutStore((s) => s.drawerOpen);
  const mobileNavOpen = useLayoutStore((s) => s.mobileNavOpen);

  const openPanel = useLayoutStore((s) => s.openPanel);
  const toggleSidebar = useLayoutStore((s) => s.toggleSidebar);
  const toggleSplitWith = useLayoutStore((s) => s.toggleSplitWith);
  const closeSplit = useLayoutStore((s) => s.closeSplit);
  const swapPanes = useLayoutStore((s) => s.swapPanes);
  const toggleDrawer = useLayoutStore((s) => s.toggleDrawer);
  const setCommandPaletteOpen = useLayoutStore((s) => s.setCommandPaletteOpen);
  const setMobileNavOpen = useLayoutStore((s) => s.setMobileNavOpen);

  const resolvedTheme = useThemeStore((s) => s.resolved);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);

  const primaryDef = getPanelDef(primaryPanel);
  const themeActionLabel = resolvedTheme === "dark" ? "浅色模式" : "深色模式";
  const themeTooltip = resolvedTheme === "dark" ? "切换浅色主题" : "切换深色主题";
  const wsMeta =
    wsStatus === "connected"
      ? { label: "实时已连接", dotClass: "bg-ok" }
      : wsStatus === "reconnecting"
        ? { label: "连接恢复中…", dotClass: "bg-warn animate-pulse" }
        : wsStatus === "connecting"
          ? { label: "正在连接…", dotClass: "bg-info animate-pulse" }
          : { label: "离线 / 等待连接", dotClass: "bg-fg-faint" };

  if (compact) {
    return (
      <div
        data-testid="workbench-root"
        data-primary-panel={primaryPanel}
        className="flex h-full flex-col bg-bg text-fg"
      >
        <header className="flex h-12 shrink-0 items-center justify-between border-b border-border-soft bg-sidebar px-3">
          <div className="flex items-center gap-2">
            <Button
              size="icon"
              variant="ghost"
              aria-label="打开导航菜单"
              aria-expanded={mobileNavOpen}
              data-testid="mobile-menu-btn"
              onClick={() => setMobileNavOpen(true)}
            >
              <Menu className="h-5 w-5" />
            </Button>
            <span className="text-sm font-semibold">{primaryDef.label}</span>
            <Badge variant="default">{agentName}</Badge>
          </div>
          <div className="flex items-center gap-1">
            <Button
              size="icon"
              variant="ghost"
              aria-label="打开命令面板"
              onClick={() => setCommandPaletteOpen(true)}
            >
              <Command className="h-4 w-4" />
            </Button>
            <Button
              size="icon"
              variant="ghost"
              aria-label="切换深浅主题"
              onClick={toggleTheme}
            >
              {resolvedTheme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>
          </div>
        </header>

        <main className="min-h-0 flex-1 overflow-hidden">
          <PanelView panelId={primaryPanel} />
        </main>

        {mobileNavOpen && (
          <div
            className="fixed inset-0 z-50 flex bg-black/55 backdrop-blur-[2px]"
            onClick={() => setMobileNavOpen(false)}
          >
            <aside
              data-testid="mobile-nav-drawer"
              className="flex h-full w-64 flex-col border-r border-border bg-sidebar p-3"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="mb-3 flex items-center justify-between px-2">
                <span className="flex items-center gap-2 text-sm font-semibold text-fg">
                  <TentacleEmblem size={18} />
                  <span>{agentName} 工作台</span>
                </span>
                <button
                  type="button"
                  onClick={() => setMobileNavOpen(false)}
                  aria-label="关闭导航菜单"
                  className="rounded-lg p-1 text-fg-dim hover:bg-hover hover:text-fg"
                >
                  <X className="h-4 w-4" />
                </button>
              </div>
              <nav className="flex-1 space-y-3 overflow-y-auto">
                {PANEL_GROUPS.map((group) => (
                  <div key={group.id} className="space-y-1">
                    <div className="px-3 text-[10.5px] font-medium tracking-[0.12em] text-fg-faint select-none">
                      {group.label}
                    </div>
                    {PANELS.filter((panel) => panel.group === group.id).map((panel) => {
                      const Icon = panel.icon;
                      const active = primaryPanel === panel.id;
                      return (
                        <button
                          key={panel.id}
                          type="button"
                          data-panel-nav={panel.id}
                          aria-current={active ? "page" : undefined}
                          onClick={() => openPanel(panel.id)}
                          className={cn(
                            "relative flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm transition-colors",
                            active
                              ? "bg-accent-soft font-medium text-accent shadow-[inset_2px_0_0_var(--t-accent)]"
                              : "text-fg-dim hover:bg-hover hover:text-fg",
                          )}
                        >
                          <Icon className="h-4 w-4 shrink-0" />
                          <span className="flex-1 truncate">{panel.label}</span>
                          <PanelBadge id={panel.id} />
                        </button>
                      );
                    })}
                    {group.id === "chat" && codingEnabled && (
                      <button
                        type="button"
                        data-testid="dsh-nav-btn"
                        onClick={() => {
                          setMobileNavOpen(false);
                          void openDshWorkspace();
                        }}
                        className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm text-fg-dim transition-colors hover:bg-hover hover:text-fg"
                      >
                        <Code2 className="h-4 w-4 shrink-0 text-accent" />
                        <span className="flex-1 truncate">DSH 编码台</span>
                        <ExternalLink className="h-3.5 w-3.5 shrink-0 text-fg-faint" />
                      </button>
                    )}
                  </div>
                ))}
              </nav>
            </aside>
          </div>
        )}
      </div>
    );
  }

  return (
    <TooltipProvider>
      <div
        data-testid="workbench-root"
        data-primary-panel={primaryPanel}
        className="flex h-full w-full overflow-hidden bg-bg text-fg"
      >
        {/* Left Navigation Sidebar */}
        <aside
          data-testid="desktop-rail"
          data-collapsed={sidebarCollapsed ? "true" : "false"}
          aria-label="工作区导航栏"
          className={cn(
            "flex shrink-0 flex-col justify-between border-r border-border-soft bg-sidebar py-2.5 transition-[width] duration-150 ease-out",
            sidebarCollapsed ? "w-14 items-center" : "w-[216px]",
          )}
        >
          {/* Brand Header */}
          <div
            className={cn(
              "mb-2 flex shrink-0 items-center",
              sidebarCollapsed ? "flex-col gap-1.5" : "justify-between px-3",
            )}
          >
            <div
              className="flex min-w-0 items-center gap-2.5"
              title={agentName}
            >
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent">
                <TentacleEmblem size={18} />
              </div>
              {!sidebarCollapsed && (
                <span className="truncate text-sm font-semibold tracking-tight text-fg">
                  {agentName}
                </span>
              )}
            </div>

            <Tooltip content={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"} side="right">
              <button
                type="button"
                onClick={toggleSidebar}
                aria-label={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-fg-faint transition-colors hover:bg-hover hover:text-fg cursor-pointer"
              >
                {sidebarCollapsed ? (
                  <PanelLeftOpen className="h-4 w-4" />
                ) : (
                  <PanelLeftClose className="h-4 w-4" />
                )}
              </button>
            </Tooltip>
          </div>

          {/* Grouped Navigation */}
          <nav
            aria-label="工作区面板"
            className={cn(
              "min-h-0 flex-1 overflow-y-auto",
              sidebarCollapsed ? "flex w-full flex-col items-center gap-1 px-1.5" : "space-y-2.5 px-2",
            )}
          >
            {PANEL_GROUPS.map((group, groupIndex) => {
              const items = PANELS.filter((panel) => panel.group === group.id);
              return (
                <div key={group.id} className={cn(sidebarCollapsed ? "flex w-full flex-col items-center gap-1" : "space-y-0.5")}>
                  {!sidebarCollapsed ? (
                    <div className="px-2.5 pb-1 pt-1 text-[10.5px] font-medium tracking-[0.12em] text-fg-faint select-none">
                      {group.label}
                    </div>
                  ) : (
                    groupIndex > 0 && <div className="my-1 w-7 border-t border-border-soft" />
                  )}

                  {items.map((panel) => {
                    const Icon = panel.icon;
                    const isPrimary = primaryPanel === panel.id;
                    const isSecondary = secondaryPanel === panel.id;

                    if (sidebarCollapsed) {
                      return (
                        <Tooltip key={panel.id} content={panel.label} side="right">
                          <button
                            type="button"
                            onClick={() => openPanel(panel.id)}
                            aria-label={panel.label}
                            aria-current={isPrimary ? "page" : undefined}
                            data-panel-nav={panel.id}
                            className={cn(
                              "relative flex h-9 w-9 items-center justify-center rounded-xl transition-colors cursor-pointer",
                              isPrimary || isSecondary
                                ? "bg-accent-soft text-accent"
                                : "text-fg-dim hover:bg-hover hover:text-fg",
                            )}
                          >
                            <Icon className="h-[17px] w-[17px]" />
                            <PanelBadge id={panel.id} collapsed />
                          </button>
                        </Tooltip>
                      );
                    }

                    return (
                      <div key={panel.id} className="group relative flex items-center">
                        <button
                          type="button"
                          onClick={() => openPanel(panel.id)}
                          aria-label={panel.label}
                          aria-current={isPrimary ? "page" : undefined}
                          data-panel-nav={panel.id}
                          className={cn(
                            "flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left text-[13.5px] transition-colors cursor-pointer",
                            isPrimary
                              ? "bg-accent-soft font-medium text-accent shadow-[inset_2px_0_0_var(--t-accent)]"
                              : isSecondary
                                ? "bg-hover/75 text-fg"
                                : "text-fg-dim hover:bg-hover hover:text-fg",
                          )}
                        >
                          <Icon
                            className={cn(
                              "h-4 w-4 shrink-0 transition-opacity",
                              isPrimary ? "opacity-100" : "opacity-80 group-hover:opacity-100",
                            )}
                          />
                          <span className="min-w-0 flex-1 truncate">{panel.label}</span>
                          <PanelBadge id={panel.id} />
                          {isSecondary && (
                            <span className="rounded bg-elevated px-1.5 py-0.5 text-[10px] text-fg-dim">
                              分屏
                            </span>
                          )}
                        </button>
                        {!isPrimary && !isSecondary && (
                          <button
                            type="button"
                            onClick={() => openPanel(panel.id, { inSplit: true })}
                            title={`在右侧分屏打开${panel.label}`}
                            aria-label={`在右侧分屏打开${panel.label}`}
                            className="absolute right-1.5 flex h-6 w-6 items-center justify-center rounded-md bg-sidebar/90 text-fg-faint opacity-0 transition-opacity hover:bg-elevated hover:text-fg group-hover:opacity-100 focus:opacity-100 cursor-pointer"
                          >
                            <Columns2 className="h-3.5 w-3.5" />
                          </button>
                        )}
                      </div>
                    );
                  })}
                  {group.id === "chat" && codingEnabled && (
                    sidebarCollapsed ? (
                      <Tooltip content="打开 DSH 编码台" side="right">
                        <button
                          type="button"
                          data-testid="dsh-nav-btn"
                          aria-label="DSH 编码台"
                          onClick={() => void openDshWorkspace()}
                          className="flex h-9 w-9 items-center justify-center rounded-xl text-fg-dim transition-colors hover:bg-hover hover:text-fg cursor-pointer"
                        >
                          <Code2 className="h-[17px] w-[17px] text-accent" />
                        </button>
                      </Tooltip>
                    ) : (
                      <button
                        type="button"
                        data-testid="dsh-nav-btn"
                        aria-label="DSH 编码台"
                        onClick={() => void openDshWorkspace()}
                        className="group flex w-full items-center gap-2.5 rounded-xl px-2.5 py-2 text-left text-[13.5px] text-fg-dim transition-colors hover:bg-hover hover:text-fg cursor-pointer"
                      >
                        <Code2 className="h-4 w-4 shrink-0 text-accent opacity-85 group-hover:opacity-100" />
                        <span className="min-w-0 flex-1 truncate">DSH 编码台</span>
                        <ExternalLink className="h-3.5 w-3.5 shrink-0 text-fg-faint opacity-75 group-hover:opacity-100" />
                      </button>
                    )
                  )}
                </div>
              );
            })}
          </nav>

          {/* Bottom Utilities & Connection Footer */}
          {sidebarCollapsed ? (
            <div className="flex flex-col items-center gap-1 border-t border-border-soft pt-2">
              <Tooltip content={secondaryPanel ? "关闭右侧分屏" : "双栏分屏视图"} side="right">
                <Button
                  size="icon"
                  variant={secondaryPanel ? "secondary" : "ghost"}
                  aria-label="切换分屏"
                  onClick={() => toggleSplitWith()}
                >
                  <Columns2 className="h-4 w-4" />
                </Button>
              </Tooltip>

              <Tooltip content="命令面板 (Ctrl/⌘+K)" side="right">
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label="命令面板"
                  onClick={() => setCommandPaletteOpen(true)}
                >
                  <Command className="h-4 w-4" />
                </Button>
              </Tooltip>

              <Tooltip content="底部工具抽屉 (Ctrl/⌘+J)" side="right">
                <Button
                  size="icon"
                  variant={drawerOpen ? "secondary" : "ghost"}
                  aria-label="切换底部抽屉"
                  onClick={() => toggleDrawer()}
                >
                  <PanelBottom className="h-4 w-4" />
                </Button>
              </Tooltip>

              <Tooltip content={themeTooltip} side="right">
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label="切换深浅主题"
                  onClick={toggleTheme}
                >
                  {resolvedTheme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
                </Button>
              </Tooltip>
            </div>
          ) : (
            <div className="shrink-0 space-y-0.5 border-t border-border-soft px-2 pt-2">
              <button
                type="button"
                aria-label="切换分屏"
                onClick={() => toggleSplitWith()}
                className={cn(
                  "flex w-full items-center justify-between rounded-xl px-2.5 py-1.5 text-xs transition-colors cursor-pointer",
                  secondaryPanel
                    ? "bg-accent-soft text-accent font-medium"
                    : "text-fg-dim hover:bg-hover hover:text-fg",
                )}
              >
                <span className="flex items-center gap-2">
                  <Columns2 className="h-3.5 w-3.5 shrink-0" />
                  <span>双栏分屏</span>
                </span>
                <span className="text-[10px] text-fg-faint">
                  {secondaryPanel ? "已开启" : "并排查看"}
                </span>
              </button>

              <button
                type="button"
                aria-label="命令面板"
                onClick={() => setCommandPaletteOpen(true)}
                className="flex w-full items-center justify-between rounded-xl px-2.5 py-1.5 text-xs text-fg-dim transition-colors hover:bg-hover hover:text-fg cursor-pointer"
              >
                <span className="flex items-center gap-2">
                  <Command className="h-3.5 w-3.5 shrink-0" />
                  <span>命令面板</span>
                </span>
                <kbd className="rounded bg-elevated px-1.5 py-0.5 font-mono text-[10px] text-fg-faint">
                  ⌘K
                </kbd>
              </button>

              <button
                type="button"
                aria-label="切换底部抽屉"
                onClick={() => toggleDrawer()}
                className={cn(
                  "flex w-full items-center justify-between rounded-xl px-2.5 py-1.5 text-xs transition-colors cursor-pointer",
                  drawerOpen
                    ? "bg-elevated font-medium text-fg"
                    : "text-fg-dim hover:bg-hover hover:text-fg",
                )}
              >
                <span className="flex items-center gap-2">
                  <PanelBottom className="h-3.5 w-3.5 shrink-0" />
                  <span>底部终端</span>
                </span>
                <kbd className="rounded bg-elevated px-1.5 py-0.5 font-mono text-[10px] text-fg-faint">
                  ⌘J
                </kbd>
              </button>

              <button
                type="button"
                aria-label="切换深浅主题"
                title={themeTooltip}
                onClick={toggleTheme}
                className="flex w-full items-center justify-between rounded-xl px-2.5 py-1.5 text-xs text-fg-dim transition-colors hover:bg-hover hover:text-fg cursor-pointer"
              >
                <span className="flex items-center gap-2">
                  {resolvedTheme === "dark" ? (
                    <Sun className="h-3.5 w-3.5 shrink-0" />
                  ) : (
                    <Moon className="h-3.5 w-3.5 shrink-0" />
                  )}
                  <span>{themeActionLabel}</span>
                </span>
              </button>

              <div className="mt-1.5 flex items-center gap-2 rounded-xl border border-border-soft/70 bg-bg/50 px-2.5 py-1.5 text-[11px] text-fg-dim">
                <span className={cn("h-2 w-2 shrink-0 rounded-full", wsMeta.dotClass)} />
                <span className="truncate">{wsMeta.label}</span>
              </div>
            </div>
          )}
        </aside>

        {/* Main Workspace Area */}
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          {/* Contextual Split Bar (only visible while viewing side-by-side panes) */}
          {secondaryPanel && (
            <div
              data-testid="split-bar"
              className="flex h-9 shrink-0 items-center justify-between border-b border-border-soft bg-sidebar/70 px-3"
            >
              <div className="flex min-w-0 items-center gap-2 text-xs">
                <Columns2 className="h-3.5 w-3.5 shrink-0 text-accent" />
                <span className="font-medium text-fg">分屏：{primaryDef.label}</span>
                <span className="text-fg-faint">|</span>
                <select
                  aria-label="选择右侧分屏面板"
                  value={secondaryPanel}
                  onChange={(e) => openPanel(e.target.value as PanelId, { inSplit: true })}
                  className="h-6 rounded-md border border-border bg-elevated px-2 text-xs text-fg focus:outline-none"
                >
                  {PANELS.filter((panel) => panel.id !== primaryPanel).map((panel) => (
                    <option key={panel.id} value={panel.id}>
                      {panel.label}
                    </option>
                  ))}
                </select>
              </div>

              <div className="flex shrink-0 items-center gap-1">
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={swapPanes}
                  title="交换左右分屏"
                  aria-label="交换左右分屏"
                >
                  <ArrowLeftRight className="h-3.5 w-3.5" />
                  <span className="hidden sm:inline">交换</span>
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={closeSplit}
                  title="关闭分屏"
                  aria-label="关闭分屏"
                >
                  <X className="h-3.5 w-3.5" />
                  <span className="hidden sm:inline">退出分屏</span>
                </Button>
              </div>
            </div>
          )}

          {/* Vertical Split: Top Primary/Secondary Panes + Optional Bottom Drawer */}
          <div className="min-h-0 flex-1">
            <PanelGroup direction="vertical" autoSaveId="tentacle-wb-v">
              <Panel id="wb-main" order={1} defaultSize={drawerOpen ? 72 : 100} minSize={35}>
                {secondaryPanel ? (
                  <PanelGroup direction="horizontal" autoSaveId="tentacle-wb-h">
                    <Panel id="wb-primary" order={1} defaultSize={55} minSize={25}>
                      <PanelView panelId={primaryPanel} />
                    </Panel>
                    <PanelResizeHandle className="w-1 bg-border-soft transition-colors hover:bg-accent" />
                    <Panel id="wb-secondary" order={2} defaultSize={45} minSize={25}>
                      <PanelView panelId={secondaryPanel} />
                    </Panel>
                  </PanelGroup>
                ) : (
                  <PanelView panelId={primaryPanel} />
                )}
              </Panel>

              {drawerOpen && (
                <>
                  <PanelResizeHandle className="h-1 bg-border-soft transition-colors hover:bg-accent" />
                  <Panel id="wb-drawer" order={2} defaultSize={28} minSize={15} maxSize={65}>
                    <DrawerBody />
                  </Panel>
                </>
              )}
            </PanelGroup>
          </div>
        </div>
      </div>
    </TooltipProvider>
  );
}
