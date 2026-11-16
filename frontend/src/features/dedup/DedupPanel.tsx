import { useCallback, useEffect, useState } from "react";
import {
  Ban,
  GitMerge,
  History,
  Network,
  Play,
  RefreshCw,
  RotateCcw,
  Sparkles,
} from "lucide-react";
import { apiFetch } from "../../api/client";
import { wsClient, type WsDedupProgressData } from "../../api/ws";
import { Badge, type BadgeVariant } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { toast } from "../../components/ui/Toast";
import { useLayoutStore } from "../workbench/layoutStore";
import { useIsCompactLayout } from "../workbench/panels";

interface ConceptPreviewSide {
  id?: string;
  name?: string;
  description?: string;
  relations?: number;
  [key: string]: unknown;
}

interface DedupPairRow {
  id: string;
  run_id?: string;
  left_id: string;
  right_id: string;
  state: string;
  updated?: number;
  choice?: Record<string, unknown> | null;
  data?: {
    preview?: {
      left?: ConceptPreviewSide;
      right?: ConceptPreviewSide;
      conflicts?: unknown[];
    };
    score?: {
      probabilities?: Record<string, number>;
      label?: string | number;
    };
    reason?: string;
    model?: string;
    error_code?: string;
  };
}

interface DedupRunInfo {
  id?: string;
  status?: string;
  checked?: number;
  visited?: number;
  errors?: number;
  message?: string;
  updated?: number;
}

interface AuditEntry {
  id: string;
  actor: string;
  source?: string;
  action: string;
  reference?: string;
  status?: string;
  created?: number;
  data?: Record<string, unknown>;
}

const STATE_TABS = [
  { id: "pending", label: "待审核" },
  { id: "merged", label: "已合并" },
  { id: "independent", label: "独立保留" },
  { id: "different", label: "不同概念" },
  { id: "all", label: "全部" },
] as const;

const STATE_BADGE: Record<string, { label: string; variant: BadgeVariant }> = {
  pending: { label: "待审核", variant: "warn" },
  merged: { label: "已合并", variant: "ok" },
  independent: { label: "已独立", variant: "info" },
  different: { label: "不同概念", variant: "default" },
};

export function DedupPanel() {
  const compact = useIsCompactLayout();
  const focusConceptInGraph = useLayoutStore((s) => s.focusConceptInGraph);

  const [mode, setMode] = useState<"pairs" | "audit">("pairs");
  const [filterState, setFilterState] = useState<string>("pending");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [run, setRun] = useState<DedupRunInfo | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [pairs, setPairs] = useState<DedupPairRow[]>([]);

  const [auditActor, setAuditActor] = useState<"all" | "manual" | "automatic">("all");
  const [auditOffset, setAuditOffset] = useState(0);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditEntries, setAuditEntries] = useState<AuditEntry[]>([]);

  const [topK, setTopK] = useState(5);
  const [maxPairs, setMaxPairs] = useState(200);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const loadPairs = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch<{
        run?: DedupRunInfo | null;
        counts?: Record<string, number>;
        pairs?: DedupPairRow[];
        offset?: number;
      }>(`/api/memory/dedup?state=${encodeURIComponent(filterState)}&offset=${offset}`);
      setRun(res.run || null);
      setCounts(res.counts || {});
      setPairs(res.pairs || []);
    } catch (err) {
      toast(err instanceof Error ? err.message : "读取概念去重列表失败", "error");
    } finally {
      setLoading(false);
    }
  }, [filterState, offset]);

  const loadAudit = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch<{
        entries?: AuditEntry[];
        total?: number;
      }>(`/api/memory/dedup/audit?actor=${auditActor}&offset=${auditOffset}`);
      setAuditEntries(res.entries || []);
      setAuditTotal(res.total || 0);
    } catch (err) {
      toast(err instanceof Error ? err.message : "读取审计日志失败", "error");
    } finally {
      setLoading(false);
    }
  }, [auditActor, auditOffset]);

  useEffect(() => {
    if (mode === "pairs") {
      void loadPairs();
    } else {
      void loadAudit();
    }
  }, [mode, loadPairs, loadAudit]);

  // Live WS subscription for dedup.progress
  useEffect(() => {
    return wsClient.on("dedup.progress", (ev) => {
      const data = (ev.data || {}) as WsDedupProgressData;
      if (data.run) setRun(data.run as DedupRunInfo);
      if (data.counts) setCounts(data.counts);
      if (mode === "pairs" && data.event && data.event !== "progress") {
        void loadPairs();
      }
    });
  }, [mode, loadPairs]);

  const handleStart = async () => {
    setStarting(true);
    try {
      await apiFetch("/api/memory/dedup/start", {
        method: "POST",
        body: JSON.stringify({ top_k: topK, max_pairs: maxPairs }),
      });
      toast("已启动概念去重扫描", "ok");
      await loadPairs();
    } catch (err) {
      toast(err instanceof Error ? err.message : "启动扫描失败", "error");
    } finally {
      setStarting(false);
    }
  };

  const handleCancel = async () => {
    try {
      await apiFetch("/api/memory/dedup/cancel", { method: "POST" });
      toast("已请求停止扫描", "info");
      await loadPairs();
    } catch (err) {
      toast(err instanceof Error ? err.message : "取消扫描失败", "error");
    }
  };

  const handlePairAction = async (
    pair: DedupPairRow,
    endpoint: "decide" | "refresh" | "reevaluate",
    body?: Record<string, unknown>,
  ) => {
    setBusyId(pair.id);
    try {
      await apiFetch(`/api/memory/dedup/${encodeURIComponent(pair.id)}/${endpoint}`, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      toast("操作已提交", "ok");
      await loadPairs();
    } catch (err) {
      toast(err instanceof Error ? err.message : "执行操作失败", "error");
    } finally {
      setBusyId(null);
    }
  };

  const isRunning = run?.status === "running";
  const totalInFilter =
    filterState === "all"
      ? Object.values(counts).reduce((a, b) => a + b, 0)
      : counts[filterState] || 0;

  const scanControlStrip = (
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1 text-fg-dim">
          <span>Top-K</span>
          <Input
            type="number"
            min={1}
            max={20}
            value={topK}
            onChange={(e) => setTopK(Number(e.target.value) || 5)}
            className="h-7 w-16 px-2 text-xs"
          />
        </label>
        <label className="flex items-center gap-1 text-fg-dim">
          <span>最大检查对数</span>
          <Input
            type="number"
            min={1}
            max={2000}
            value={maxPairs}
            onChange={(e) => setMaxPairs(Number(e.target.value) || 200)}
            className="h-7 w-20 px-2 text-xs"
          />
        </label>
        {isRunning ? (
          <Button size="sm" variant="danger" onClick={() => void handleCancel()}>
            <Ban className="h-3.5 w-3.5" />
            停止扫描
          </Button>
        ) : (
          <Button size="sm" variant="primary" disabled={starting} onClick={() => void handleStart()}>
            <Play className="h-3.5 w-3.5" />
            {starting ? "启动中…" : "启动扫描"}
          </Button>
        )}
      </div>

      {run?.message && <span className="text-fg-dim">{run.message}</span>}
    </div>
  );

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-3 py-2 md:px-4 md:py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <GitMerge className="h-4 w-4 text-accent" />
          <h2 className="text-sm font-semibold text-fg">概念实体去重</h2>
          {run && (
            <Badge variant={isRunning ? "warn" : run.status === "failed" ? "danger" : "default"}>
              {run.status || "idle"} · 已检查 {run.checked ?? 0} 对 / 遍历 {run.visited ?? 0}
            </Badge>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            size="sm"
            variant={mode === "pairs" ? "primary" : "secondary"}
            onClick={() => setMode("pairs")}
          >
            候选配对
          </Button>
          <Button
            size="sm"
            variant={mode === "audit" ? "primary" : "secondary"}
            onClick={() => setMode("audit")}
          >
            <History className="h-3.5 w-3.5" />
            审计日志
          </Button>
        </div>
      </div>

      {/* Scan Control Strip (sticky on desktop, unified scrollable card on mobile) */}
      {!compact && (
        <div className="border-b border-border-soft bg-surface/60 px-4 py-2">
          {scanControlStrip}
        </div>
      )}

      {/* Main Content */}
      <div className="min-h-0 flex-1 overflow-y-auto p-3 md:p-4">
        {compact && (
          <div className="mx-auto mb-3 max-w-4xl rounded-2xl border border-border bg-surface p-3">
            {scanControlStrip}
          </div>
        )}
        {mode === "pairs" ? (
          <div className="mx-auto max-w-4xl space-y-3">
            {/* Filter Pills */}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap gap-1.5">
                {STATE_TABS.map((tab) => {
                  const count =
                    tab.id === "all"
                      ? Object.values(counts).reduce((a, b) => a + b, 0)
                      : counts[tab.id] || 0;
                  return (
                    <button
                      key={tab.id}
                      type="button"
                      onClick={() => {
                        setFilterState(tab.id);
                        setOffset(0);
                      }}
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors cursor-pointer",
                        filterState === tab.id
                          ? "border-accent/40 bg-accent-soft text-accent font-medium"
                          : "border-border bg-surface text-fg-dim hover:text-fg",
                      )}
                    >
                      <span>{tab.label}</span>
                      <span className="font-mono text-[11px]">{count}</span>
                    </button>
                  );
                })}
              </div>

              <Button size="sm" variant="ghost" onClick={() => void loadPairs()} disabled={loading}>
                <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
                刷新
              </Button>
            </div>

            {pairs.length === 0 && !loading ? (
              <div className="rounded-2xl border border-border bg-surface p-8 text-center text-xs text-fg-dim">
                当前状态下暂无候选概念配对。
              </div>
            ) : (
              pairs.map((pair) => {
                const badge = STATE_BADGE[pair.state] || {
                  label: pair.state,
                  variant: "default" as BadgeVariant,
                };
                const left = pair.data?.preview?.left;
                const right = pair.data?.preview?.right;
                const conflicts = pair.data?.preview?.conflicts || [];
                const probs = pair.data?.score?.probabilities;
                const sameProb = probs?.["2"];
                const diffProb = probs?.["0"];
                const isBusy = busyId === pair.id;

                return (
                  <div
                    key={pair.id}
                    className="rounded-2xl border border-border bg-surface p-4 text-xs"
                  >
                    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <Badge variant={badge.variant}>{badge.label}</Badge>
                        {pair.data?.reason && (
                          <Badge variant="default">{pair.data.reason}</Badge>
                        )}
                        {sameProb !== undefined && (
                          <span className="font-mono text-[11px] text-ok">
                            同义 {(sameProb * 100).toFixed(1)}%
                          </span>
                        )}
                        {diffProb !== undefined && (
                          <span className="font-mono text-[11px] text-fg-faint">
                            不同 {(diffProb * 100).toFixed(1)}%
                          </span>
                        )}
                        {conflicts.length > 0 && (
                          <Badge variant="warn">{conflicts.length} 项关系冲突</Badge>
                        )}
                      </div>
                      <span className="font-mono text-[10px] text-fg-faint">{pair.id}</span>
                    </div>

                    <div className="grid gap-3 md:grid-cols-2">
                      {[
                        { side: left, id: pair.left_id, title: "概念 A" },
                        { side: right, id: pair.right_id, title: "概念 B" },
                      ].map(({ side, id, title }) => (
                        <div
                          key={id}
                          className="flex flex-col justify-between rounded-xl border border-border-soft bg-elevated/60 p-3"
                        >
                          <div>
                            <div className="flex items-center justify-between gap-2">
                              <span className="text-[11px] font-medium text-fg-faint">{title}</span>
                              <button
                                type="button"
                                onClick={() => focusConceptInGraph(id)}
                                className="inline-flex items-center gap-1 text-[11px] text-accent hover:underline cursor-pointer"
                              >
                                <Network className="h-3 w-3" />
                                图谱定位
                              </button>
                            </div>
                            <div className="mt-1 text-sm font-semibold text-fg">
                              {side?.name || id}
                            </div>
                            <div className="mt-0.5 font-mono text-[10px] text-fg-faint">{id}</div>
                            {side?.description && (
                              <p className="mt-1.5 text-fg-dim">{side.description}</p>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>

                    <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-border-soft pt-3">
                      <div className="flex flex-wrap gap-1.5">
                        <Button
                          size="sm"
                          variant="primary"
                          disabled={isBusy}
                          onClick={() =>
                            void handlePairAction(pair, "decide", {
                              action: "merge",
                              keep_id: pair.left_id,
                              policy: "reject",
                            })
                          }
                        >
                          <GitMerge className="h-3.5 w-3.5" />
                          保留 A 并合并
                        </Button>
                        <Button
                          size="sm"
                          variant="primary"
                          disabled={isBusy}
                          onClick={() =>
                            void handlePairAction(pair, "decide", {
                              action: "merge",
                              keep_id: pair.right_id,
                              policy: "reject",
                            })
                          }
                        >
                          保留 B 并合并
                        </Button>
                        <Button
                          size="sm"
                          variant="secondary"
                          disabled={isBusy}
                          onClick={() =>
                            void handlePairAction(pair, "decide", {
                              action: "independent",
                              policy: "reject",
                            })
                          }
                        >
                          标为独立概念
                        </Button>
                      </div>

                      <div className="flex gap-1.5">
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={isBusy}
                          onClick={() => void handlePairAction(pair, "refresh")}
                        >
                          <RotateCcw className="h-3.5 w-3.5" />
                          刷新预览
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={isBusy}
                          onClick={() => void handlePairAction(pair, "reevaluate")}
                        >
                          <Sparkles className="h-3.5 w-3.5" />
                          重评
                        </Button>
                      </div>
                    </div>
                  </div>
                );
              })
            )}

            {/* Pagination */}
            {totalInFilter > 30 && (
              <div className="flex items-center justify-between pt-2 text-xs text-fg-dim">
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={offset === 0}
                  onClick={() => setOffset((o) => Math.max(0, o - 30))}
                >
                  上一页
                </Button>
                <span>
                  第 {Math.floor(offset / 30) + 1} 页（共 {totalInFilter} 条）
                </span>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={offset + 30 >= totalInFilter}
                  onClick={() => setOffset((o) => o + 30)}
                >
                  下一页
                </Button>
              </div>
            )}
          </div>
        ) : (
          <div className="mx-auto max-w-4xl space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex gap-1.5">
                {(["all", "manual", "automatic"] as const).map((actor) => (
                  <button
                    key={actor}
                    type="button"
                    onClick={() => {
                      setAuditActor(actor);
                      setAuditOffset(0);
                    }}
                    className={cn(
                      "rounded-full border px-3 py-1 text-xs cursor-pointer",
                      auditActor === actor
                        ? "border-accent/40 bg-accent-soft text-accent font-medium"
                        : "border-border bg-surface text-fg-dim",
                    )}
                  >
                    {actor === "all" ? "全部" : actor === "manual" ? "人工裁决" : "自动扫描"}
                  </button>
                ))}
              </div>
              <Button size="sm" variant="ghost" onClick={() => void loadAudit()}>
                刷新日志
              </Button>
            </div>

            {auditEntries.length === 0 ? (
              <div className="rounded-2xl border border-border bg-surface p-8 text-center text-xs text-fg-dim">
                暂无审计记录。
              </div>
            ) : (
              <div className="space-y-2">
                {auditEntries.map((entry) => (
                  <div
                    key={entry.id}
                    className="rounded-xl border border-border bg-surface p-3 text-xs"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-2">
                        <Badge variant={entry.actor === "manual" ? "accent" : "default"}>
                          {entry.actor}
                        </Badge>
                        <span className="font-mono font-medium text-fg">{entry.action}</span>
                        {entry.status && <Badge variant="default">{entry.status}</Badge>}
                      </div>
                      <span className="text-[11px] text-fg-faint">
                        {entry.created ? new Date(entry.created * 1000).toLocaleString() : "—"}
                      </span>
                    </div>
                    {entry.reference && (
                      <div className="mt-1 font-mono text-[11px] text-fg-dim">
                        目标：{entry.reference}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {auditTotal > 30 && (
              <div className="flex items-center justify-between pt-2 text-xs text-fg-dim">
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={auditOffset === 0}
                  onClick={() => setAuditOffset((o) => Math.max(0, o - 30))}
                >
                  上一页
                </Button>
                <span>共 {auditTotal} 条</span>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={auditOffset + 30 >= auditTotal}
                  onClick={() => setAuditOffset((o) => o + 30)}
                >
                  下一页
                </Button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
