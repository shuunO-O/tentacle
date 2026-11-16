import { useCallback, useEffect, useRef, useState } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { Eraser, Minus, Plus, RefreshCw, Terminal as TerminalIcon } from "lucide-react";
import { apiFetch } from "../../api/client";
import { wsClient } from "../../api/ws";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { toast } from "../../components/ui/Toast";
import { useThemeStore } from "../../stores/themeStore";

interface TerminalEntry {
  ts: number;
  content: string;
}

const DARK_XTERM_THEME = {
  background: "#18130f",
  foreground: "#efe7dc",
  cursor: "#d97757",
  selectionBackground: "rgba(217, 119, 87, 0.28)",
};

const LIGHT_XTERM_THEME = {
  background: "#faf7f2",
  foreground: "#231c16",
  cursor: "#b55334",
  selectionBackground: "rgba(181, 83, 52, 0.22)",
};

export function TerminalPanel({ compactDrawer = false }: { compactDrawer?: boolean }) {
  const theme = useThemeStore((s) => s.resolved);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const lastTsRef = useRef(0);

  const [fontSize, setFontSize] = useState(13);
  const [loading, setLoading] = useState(false);

  const fetchHistory = useCallback(async (reset = false) => {
    setLoading(true);
    try {
      const since = reset ? 0 : lastTsRef.current;
      const res = await apiFetch<{ ok: boolean; entries: TerminalEntry[] }>(
        `/api/terminal/history?since=${encodeURIComponent(String(since))}`,
      );
      if (reset) {
        termRef.current?.clear();
        lastTsRef.current = 0;
      }
      for (const entry of res.entries || []) {
        if (entry.content) {
          termRef.current?.write(entry.content);
        }
        if (typeof entry.ts === "number" && entry.ts > lastTsRef.current) {
          lastTsRef.current = entry.ts;
        }
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "读取终端历史失败", "error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const container = hostRef.current;
    if (!container) return;

    const term = new Terminal({
      convertEol: true,
      disableStdin: true,
      scrollback: 5000,
      fontSize,
      fontFamily: '"JetBrains Mono", "Cascadia Mono", Consolas, monospace',
      theme: theme === "light" ? LIGHT_XTERM_THEME : DARK_XTERM_THEME,
    });
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(container);
    fitAddon.fit();

    termRef.current = term;
    fitRef.current = fitAddon;

    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => {
        try {
          fitAddon.fit();
        } catch {
          // Ignore resize race while hidden.
        }
      });
      ro.observe(container);
    }

    const unsub = wsClient.on("terminal_data", (ev) => {
      const data = (ev.data || {}) as { content?: string };
      if (data.content) {
        term.write(data.content);
      }
    });

    void fetchHistory(true);

    return () => {
      unsub();
      ro?.disconnect();
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
    // Theme & fontSize updates are synced smoothly in the effect below without recreating Terminal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const term = termRef.current;
    if (!term) return;
    term.options.theme = theme === "light" ? LIGHT_XTERM_THEME : DARK_XTERM_THEME;
    term.options.fontSize = fontSize;
    fitRef.current?.fit();
  }, [theme, fontSize]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      {!compactDrawer && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-4 py-2.5">
          <div className="flex items-center gap-2">
            <TerminalIcon className="h-4 w-4 text-accent" />
            <h2 className="text-sm font-semibold text-fg">会话只读终端</h2>
            <Badge variant="default">ANSI 实时输出流</Badge>
          </div>
          <div className="flex items-center gap-1.5">
            <Button
              size="icon"
              variant="ghost"
              className="h-7 w-7"
              onClick={() => setFontSize((s) => Math.max(10, s - 1))}
              aria-label="减小字号"
            >
              <Minus className="h-3.5 w-3.5" />
            </Button>
            <span className="font-mono text-xs text-fg-dim">{fontSize}px</span>
            <Button
              size="icon"
              variant="ghost"
              className="h-7 w-7"
              onClick={() => setFontSize((s) => Math.min(22, s + 1))}
              aria-label="增大字号"
            >
              <Plus className="h-3.5 w-3.5" />
            </Button>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => termRef.current?.clear()}
            >
              <Eraser className="h-3.5 w-3.5" />
              清屏
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={loading}
              onClick={() => void fetchHistory(true)}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              重放历史
            </Button>
          </div>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-hidden p-2">
        <div
          ref={hostRef}
          className="h-full w-full overflow-hidden rounded-xl border border-border-soft"
        />
      </div>
    </div>
  );
}
