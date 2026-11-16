import { useEffect } from "react";
import { Command } from "cmdk";
import { Code2, Columns2, Moon, Search, Sun, Terminal } from "lucide-react";
import { useThemeStore } from "../../stores/themeStore";
import { openDshWorkspace } from "../subagents/dsh";
import { useLayoutStore } from "./layoutStore";
import { PANELS, useIsCompactLayout } from "./panels";

export function CommandPalette() {
  const open = useLayoutStore((s) => s.commandPaletteOpen);
  const setOpen = useLayoutStore((s) => s.setCommandPaletteOpen);
  const openPanel = useLayoutStore((s) => s.openPanel);
  const toggleDrawer = useLayoutStore((s) => s.toggleDrawer);
  const closeSplit = useLayoutStore((s) => s.closeSplit);
  const secondaryPanel = useLayoutStore((s) => s.secondaryPanel);
  const codingEnabled = useLayoutStore((s) => s.domainStatus.codingEnabled);
  const askChatPrompt = useLayoutStore((s) => s.askChatPrompt);

  const resolvedTheme = useThemeStore((s) => s.resolved);
  const toggleTheme = useThemeStore((s) => s.toggleTheme);
  const compact = useIsCompactLayout();

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen(!useLayoutStore.getState().commandPaletteOpen);
      } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "j") {
        e.preventDefault();
        toggleDrawer("terminal");
      } else if (e.key === "Escape" && useLayoutStore.getState().commandPaletteOpen) {
        setOpen(false);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [setOpen, toggleDrawer]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/55 pt-[14vh] backdrop-blur-[2px]"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-[min(92vw,560px)] overflow-hidden rounded-2xl border border-border bg-surface text-fg shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <Command label="全局命令面板" className="flex flex-col">
          <div className="flex items-center gap-2.5 border-b border-border-soft px-4 py-3">
            <Search className="h-4 w-4 text-fg-dim" />
            <Command.Input
              autoFocus
              placeholder="跳转工作区面板、打开分屏、执行快捷指令…"
              className="w-full bg-transparent text-sm text-fg placeholder:text-fg-faint focus:outline-none"
            />
            <kbd className="rounded border border-border bg-elevated px-1.5 py-0.5 font-mono text-[10px] text-fg-faint">
              Esc
            </kbd>
          </div>

          <Command.List className="max-h-80 overflow-y-auto p-2 text-sm">
            <Command.Empty className="py-6 text-center text-xs text-fg-dim">
              没有匹配的命令或面板
            </Command.Empty>

            <Command.Group heading="工作区面板" className="px-2 py-1 text-[11px] font-medium text-fg-faint">
              {PANELS.map((panel) => {
                const Icon = panel.icon;
                return (
                  <Command.Item
                    key={panel.id}
                    value={`${panel.label} ${panel.id} ${panel.description}`}
                    onSelect={() => {
                      openPanel(panel.id);
                      setOpen(false);
                    }}
                    className="flex cursor-pointer items-center justify-between rounded-lg px-2.5 py-2 text-xs text-fg aria-selected:bg-hover"
                  >
                    <span className="flex items-center gap-2.5">
                      <Icon className="h-4 w-4 text-accent" />
                      <span className="font-medium">{panel.label}</span>
                      <span className="text-fg-dim">{panel.description}</span>
                    </span>
                  </Command.Item>
                );
              })}
            </Command.Group>

            {!compact && (
              <Command.Group heading="分屏与抽屉" className="mt-2 px-2 py-1 text-[11px] font-medium text-fg-faint">
                {PANELS.filter((p) => p.id !== "terminal").map((panel) => (
                  <Command.Item
                    key={`split-${panel.id}`}
                    value={`分屏 split ${panel.label} ${panel.id}`}
                    onSelect={() => {
                      openPanel(panel.id, { inSplit: true });
                      setOpen(false);
                    }}
                    className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs text-fg aria-selected:bg-hover"
                  >
                    <Columns2 className="h-4 w-4 text-fg-dim" />
                    <span>在右侧分屏打开：{panel.label}</span>
                  </Command.Item>
                ))}
                {secondaryPanel && (
                  <Command.Item
                    value="关闭分屏 close split"
                    onSelect={() => {
                      closeSplit();
                      setOpen(false);
                    }}
                    className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs text-fg aria-selected:bg-hover"
                  >
                    <Columns2 className="h-4 w-4 text-danger" />
                    <span>关闭右侧分屏</span>
                  </Command.Item>
                )}
              </Command.Group>
            )}

            <Command.Group heading="快捷操作" className="mt-2 px-2 py-1 text-[11px] font-medium text-fg-faint">
              {codingEnabled && (
                <Command.Item
                  value="打开 DSH 编码工作区 coding dsh"
                  onSelect={() => {
                    setOpen(false);
                    void openDshWorkspace();
                  }}
                  className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs text-fg aria-selected:bg-hover"
                >
                  <Code2 className="h-4 w-4 text-accent" />
                  <span>打开 DSH 编码工作区</span>
                </Command.Item>
              )}
              <Command.Item
                value="切换底部终端 drawer terminal"
                onSelect={() => {
                  toggleDrawer("terminal");
                  setOpen(false);
                }}
                className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs text-fg aria-selected:bg-hover"
              >
                <Terminal className="h-4 w-4 text-fg-dim" />
                <span>切换底部工具抽屉（终端）</span>
              </Command.Item>
              <Command.Item
                value="切换主题 theme dark light"
                onSelect={() => {
                  toggleTheme();
                  setOpen(false);
                }}
                className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs text-fg aria-selected:bg-hover"
              >
                {resolvedTheme === "dark" ? (
                  <Sun className="h-4 w-4 text-warn" />
                ) : (
                  <Moon className="h-4 w-4 text-info" />
                )}
                <span>切换为{resolvedTheme === "dark" ? "浅色纸张主题" : "深色暖调主题"}</span>
              </Command.Item>
              <Command.Item
                value="开启新对话 /session"
                onSelect={() => {
                  askChatPrompt("/session");
                  setOpen(false);
                }}
                className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs text-fg aria-selected:bg-hover"
              >
                <span className="font-mono text-accent">/session</span>
                <span className="text-fg-dim">预填新会话指令到输入框</span>
              </Command.Item>
              <Command.Item
                value="整理记忆 /compress"
                onSelect={() => {
                  askChatPrompt("/compress");
                  setOpen(false);
                }}
                className="flex cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs text-fg aria-selected:bg-hover"
              >
                <span className="font-mono text-accent">/compress</span>
                <span className="text-fg-dim">预填记忆压缩指令到输入框</span>
              </Command.Item>
            </Command.Group>
          </Command.List>
        </Command>
      </div>
    </div>
  );
}
