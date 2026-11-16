import { useCallback, useEffect, useState } from "react";
import {
  ArrowLeft,
  Bot,
  ChevronRight,
  ChevronUp,
  Code2,
  ExternalLink,
  Link2,
  RefreshCw,
} from "lucide-react";
import { apiFetch } from "../../api/client";
import { wsClient, type WsSubagentUpdatedData } from "../../api/ws";
import { Badge, type BadgeVariant } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { toast } from "../../components/ui/Toast";
import { useIsCompactLayout } from "../workbench/panels";
import { openDshWorkspace } from "./dsh";

interface SubagentTaskSummary {
  id: string;
  objective?: string;
  status?: string;
  archived?: number | boolean;
  cleanup_error?: string | null;
  created_at?: number;
  updated_at?: number;
  turn_id?: number | null;
  delivery?: string | null;
  result?: string | null;
  error?: string | null;
  finished_at?: number | null;
}

interface SubagentTurn {
  id: number;
  message?: string;
  status?: string;
  delivery?: string;
  result?: string | null;
  error?: string | null;
  usage?: Record<string, unknown> | null;
  created_at?: number;
  finished_at?: number | null;
}

interface SubagentTaskDetail extends SubagentTaskSummary {
  background?: string | null;
  dsh_session_id?: string | null;
}

interface DshSession {
  id?: string;
  session_id?: string;
  sessionId?: string;
  title?: string;
  cwd?: string;
  workspace?: string;
  status?: string;
  updated_at?: number | string;
}

const STATUS_BADGE: Record<string, { label: string; variant: BadgeVariant }> = {
  running: { label: "运行中", variant: "accent" },
  queued: { label: "排队中", variant: "warn" },
  completed: { label: "已完成", variant: "ok" },
  failed: { label: "失败", variant: "danger" },
  cancelled: { label: "已取消", variant: "default" },
  archived: { label: "已归档", variant: "default" },
};

const DELIVERY_LABEL: Record<string, string> = {
  pending: "待主会话接收",
  waiting: "等待投递",
  background: "后台运行",
  delivered: "已汇报主会话",
  suppressed: "已静默",
};

function formatEpoch(ts?: number | string | null): string {
  if (ts === undefined || ts === null) return "—";
  const num = typeof ts === "number" ? ts : Number(ts);
  if (!Number.isFinite(num) || num <= 0) return String(ts);
  return new Date(num * 1000).toLocaleString();
}

export function SubagentsPanel() {
  const compact = useIsCompactLayout();

  const [enabled, setEnabled] = useState(true);
  const [codingEnabled, setCodingEnabled] = useState(false);
  const [executionBlocked, setExecutionBlocked] = useState(false);
  const [tasks, setTasks] = useState<SubagentTaskSummary[]>([]);
  const [hasMoreTasks, setHasMoreTasks] = useState(false);
  const [loading, setLoading] = useState(true);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mobileShowDetail, setMobileShowDetail] = useState(false);
  const [detailTask, setDetailTask] = useState<SubagentTaskDetail | null>(null);
  const [turns, setTurns] = useState<SubagentTurn[]>([]);
  const [hasMoreTurns, setHasMoreTurns] = useState(false);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);

  const [dshOpen, setDshOpen] = useState(false);
  const [dshSessions, setDshSessions] = useState<DshSession[]>([]);
  const [loadingDsh, setLoadingDsh] = useState(false);
  const [attachingId, setAttachingId] = useState<string | null>(null);

  const loadTasks = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch<{
        enabled: boolean;
        coding_enabled?: boolean;
        execution_blocked?: boolean;
        has_more?: boolean;
        tasks: SubagentTaskSummary[];
      }>("/api/subagents?limit=30");
      setEnabled(res.enabled);
      setCodingEnabled(Boolean(res.coding_enabled));
      setExecutionBlocked(Boolean(res.execution_blocked));
      setHasMoreTasks(Boolean(res.has_more));
      setTasks(res.tasks || []);
      if (res.tasks.length > 0 && !selectedId) {
        setSelectedId(res.tasks[0]!.id);
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载子代理失败", "error");
    } finally {
      setLoading(false);
    }
  }, [selectedId]);

  const loadDetail = useCallback(async (taskId: string, before?: number | null) => {
    setLoadingDetail(true);
    try {
      const query = before ? `?before=${before}&limit=20` : "?limit=20";
      const res = await apiFetch<{
        task: SubagentTaskDetail;
        turns: SubagentTurn[];
        has_more?: boolean;
        next_before?: number | null;
      }>(`/api/subagents/${encodeURIComponent(taskId)}${query}`);
      setDetailTask(res.task);
      setTurns((prev) => (before ? [...res.turns, ...prev] : res.turns));
      setHasMoreTurns(Boolean(res.has_more));
      setNextBefore(res.next_before ?? null);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载子任务详情失败", "error");
    } finally {
      setLoadingDetail(false);
    }
  }, []);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  useEffect(() => {
    if (selectedId) {
      void loadDetail(selectedId);
    } else {
      setDetailTask(null);
      setTurns([]);
    }
  }, [selectedId, loadDetail]);

  // Live WS subscription for subagent.updated
  useEffect(() => {
    return wsClient.on("subagent.updated", (ev) => {
      const data = (ev.data || {}) as WsSubagentUpdatedData;
      if (typeof data.enabled === "boolean") setEnabled(data.enabled);
      if (typeof data.coding_enabled === "boolean") setCodingEnabled(data.coding_enabled);
      if (typeof data.execution_blocked === "boolean") setExecutionBlocked(data.execution_blocked);
      if (typeof data.has_more === "boolean") setHasMoreTasks(data.has_more);
      if (Array.isArray(data.tasks)) {
        setTasks(data.tasks as unknown as SubagentTaskSummary[]);
      } else {
        void loadTasks();
      }
      if (selectedId && (!data.task_id || data.task_id === selectedId)) {
        void loadDetail(selectedId);
      }
    });
  }, [selectedId, loadDetail, loadTasks]);

  const handleOpenCoding = () => {
    void openDshWorkspace();
  };

  const handleLoadDshSessions = async () => {
    setDshOpen(true);
    setLoadingDsh(true);
    try {
      const res = await apiFetch<{ sessions: DshSession[] }>("/api/subagents/dsh/sessions");
      setDshSessions(res.sessions || []);
    } catch (err) {
      toast(err instanceof Error ? err.message : "读取 DSH 会话失败", "error");
    } finally {
      setLoadingDsh(false);
    }
  };

  const handleAttachDsh = async (sessionId: string) => {
    setAttachingId(sessionId);
    try {
      const res = await apiFetch<{ task_id: string }>("/api/subagents/dsh/attach", {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId }),
      });
      toast("已接管 DSH 会话为子任务", "ok");
      setDshOpen(false);
      await loadTasks();
      if (res.task_id) {
        setSelectedId(res.task_id);
        if (compact) setMobileShowDetail(true);
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "接管会话失败", "error");
    } finally {
      setAttachingId(null);
    }
  };

  const showListPane = !compact || !mobileShowDetail;
  const showDetailPane = !compact || mobileShowDetail;

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-3 py-2 md:px-4 md:py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <Bot className="h-4 w-4 text-accent" />
          <h2 className="text-sm font-semibold text-fg">子代理与后台任务</h2>
          <Badge variant={enabled ? "ok" : "default"}>
            {enabled ? "已启用" : "未启用"}
          </Badge>
          {executionBlocked && <Badge variant="warn">工作区清理异常，执行已阻塞</Badge>}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {codingEnabled ? (
            <>
              <Button size="sm" variant="secondary" onClick={() => void handleLoadDshSessions()}>
                <Link2 className="h-3.5 w-3.5" />
                DSH 会话
              </Button>
              <Button size="sm" variant="primary" onClick={handleOpenCoding}>
                <Code2 className="h-3.5 w-3.5" />
                打开编码工作区
                <ExternalLink className="h-3 w-3" />
              </Button>
            </>
          ) : (
            enabled && (
              <span
                className="px-1.5 text-[11px] text-fg-faint"
                title="可在系统配置中心的 subagents / DSH 选项开启编码工作区"
              >
                DSH 未启用
              </span>
            )
          )}
          <Button size="sm" variant="secondary" onClick={() => void loadTasks()} disabled={loading}>
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            刷新
          </Button>
        </div>
      </div>

      {/* Mobile Detail Navigation Bar */}
      {compact && showDetailPane && (
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border-soft bg-surface/85 px-3 py-2">
          <button
            type="button"
            data-testid="subagent-mobile-back"
            onClick={() => setMobileShowDetail(false)}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium text-fg transition-colors hover:bg-hover"
          >
            <ArrowLeft className="h-3.5 w-3.5" />
            <span>返回任务列表</span>
            <span className="text-fg-faint">({tasks.length})</span>
          </button>
          {selectedId && (
            <span className="font-mono text-[11px] text-fg-faint">
              {selectedId.slice(0, 8)}
            </span>
          )}
        </div>
      )}

      {/* Body: Desktop side-by-side split / Mobile single-pane drill-down */}
      <div className="flex min-h-0 flex-1">
        {/* Task list */}
        {showListPane && (
          <aside
            className={cn(
              "overflow-y-auto",
              compact
                ? "min-h-0 flex-1 bg-bg p-3"
                : "w-80 shrink-0 border-r border-border bg-surface/60 p-2.5",
            )}
          >
            {!enabled ? (
              <div className="p-4 text-xs text-fg-dim">
                子代理功能当前处于关闭状态，可在「配置」面板的 subagents 节点开启。
              </div>
            ) : tasks.length === 0 && !loading ? (
              <div className="p-4 text-xs text-fg-dim">
                暂无子代理任务记录。可在对话中委派后台任务。
              </div>
            ) : (
              <div className="space-y-1.5">
                {tasks.map((task) => {
                  const badge = STATUS_BADGE[task.status || ""] || {
                    label: task.status || "未知",
                    variant: "default" as BadgeVariant,
                  };
                  const selected = !compact && task.id === selectedId;
                  return (
                    <button
                      key={task.id}
                      type="button"
                      onClick={() => {
                        setSelectedId(task.id);
                        if (compact) setMobileShowDetail(true);
                      }}
                      className={cn(
                        "w-full rounded-xl border p-3 text-left transition-colors cursor-pointer",
                        selected
                          ? "border-accent/50 bg-accent-soft/50"
                          : "border-border-soft bg-surface hover:bg-hover",
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <Badge variant={badge.variant}>{badge.label}</Badge>
                        <span className="flex items-center gap-1 font-mono text-[10px] text-fg-faint">
                          <span>{task.id.slice(0, 8)}</span>
                          {compact && <ChevronRight className="h-3.5 w-3.5 text-fg-faint" />}
                        </span>
                      </div>
                      <p className="mt-1.5 line-clamp-2 text-xs font-medium text-fg">
                        {task.objective || "（无目标描述）"}
                      </p>
                      <div className="mt-1.5 flex items-center justify-between text-[11px] text-fg-faint">
                        <span>
                          {task.delivery ? DELIVERY_LABEL[task.delivery] || task.delivery : "—"}
                        </span>
                        <span>{formatEpoch(task.updated_at)}</span>
                      </div>
                      {task.cleanup_error && (
                        <p className="mt-1 text-[11px] text-danger">清理警告：{task.cleanup_error}</p>
                      )}
                    </button>
                  );
                })}
                {hasMoreTasks && (
                  <p className="px-2 py-1 text-center text-[11px] text-fg-faint">
                    仅展示最近 30 条任务记录
                  </p>
                )}
              </div>
            )}
          </aside>
        )}

        {/* Detail & Turns */}
        {showDetailPane && (
          <section className="flex min-w-0 flex-1 flex-col overflow-y-auto p-3 sm:p-4">
          {!selectedId || !detailTask ? (
            <div className="flex h-full items-center justify-center text-xs text-fg-dim">
              {loadingDetail ? "正在读取子任务详情…" : "从左侧选择一个子任务查看轮次与结果"}
            </div>
          ) : (
            <div className="mx-auto w-full max-w-3xl space-y-4">
              <div className="rounded-2xl border border-border bg-surface p-4">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <Badge variant={(STATUS_BADGE[detailTask.status || ""] || { variant: "default" }).variant}>
                        {(STATUS_BADGE[detailTask.status || ""] || { label: detailTask.status || "未知" }).label}
                      </Badge>
                      {detailTask.dsh_session_id && (
                        <>
                          <Badge variant="info" className="font-mono">
                            DSH: {detailTask.dsh_session_id}
                          </Badge>
                          {codingEnabled && (
                            <Button size="sm" variant="ghost" onClick={handleOpenCoding}>
                              <Code2 className="h-3.5 w-3.5" />
                              打开 DSH
                              <ExternalLink className="h-3 w-3" />
                            </Button>
                          )}
                        </>
                      )}
                      <span className="font-mono text-[11px] text-fg-faint">{detailTask.id}</span>
                    </div>
                    <h3 className="mt-2 text-sm font-semibold text-fg">
                      {detailTask.objective || "（无目标描述）"}
                    </h3>
                  </div>
                </div>

                {detailTask.background && (
                  <p className="mt-2 whitespace-pre-wrap rounded-xl bg-elevated/60 p-2.5 text-xs text-fg-dim">
                    {detailTask.background}
                  </p>
                )}
              </div>

              {hasMoreTurns && nextBefore !== null && (
                <div className="flex justify-center">
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={loadingDetail}
                    onClick={() => void loadDetail(detailTask.id, nextBefore)}
                  >
                    <ChevronUp className="h-3.5 w-3.5" />
                    加载更早轮次
                  </Button>
                </div>
              )}

              <div className="space-y-3">
                {turns.map((turn) => (
                  <div
                    key={turn.id}
                    className="rounded-2xl border border-border bg-surface p-3.5 text-xs"
                  >
                    <div className="mb-2 flex flex-wrap items-center justify-between gap-2 border-b border-border-soft pb-2">
                      <div className="flex items-center gap-2">
                        <span className="font-mono font-semibold text-fg">Turn #{turn.id}</span>
                        <Badge variant={(STATUS_BADGE[turn.status || ""] || { variant: "default" }).variant}>
                          {(STATUS_BADGE[turn.status || ""] || { label: turn.status || "" }).label}
                        </Badge>
                        {turn.delivery && (
                          <span className="text-fg-faint">
                            {DELIVERY_LABEL[turn.delivery] || turn.delivery}
                          </span>
                        )}
                      </div>
                      <span className="text-[11px] text-fg-faint">
                        {formatEpoch(turn.created_at)}
                      </span>
                    </div>

                    {turn.message && (
                      <div className="mb-2">
                        <div className="mb-0.5 text-[11px] font-medium text-fg-faint">委派指令</div>
                        <pre className="whitespace-pre-wrap rounded-lg bg-elevated/60 p-2.5 font-sans text-fg">
                          {turn.message}
                        </pre>
                      </div>
                    )}

                    {turn.result && (
                      <div className="mb-2">
                        <div className="mb-0.5 text-[11px] font-medium text-fg-faint">执行结果</div>
                        <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-bg p-2.5 font-mono text-[11px] text-fg">
                          {turn.result}
                        </pre>
                      </div>
                    )}

                    {turn.error && (
                      <div className="rounded-lg border border-danger/30 bg-danger-soft/50 p-2 text-danger">
                        {turn.error}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
        )}
      </div>

      {/* DSH Sessions Modal */}
      <Dialog
        open={dshOpen}
        onOpenChange={setDshOpen}
        title="DSH 编码工作区会话"
        description="选择历史或运行中的 DSH 会话接管至子代理任务"
        className="w-[min(94vw,620px)]"
      >
        <div className="max-h-96 space-y-2 overflow-y-auto">
          {loadingDsh ? (
            <p className="py-6 text-center text-xs text-fg-dim">正在查询 DSH 会话…</p>
          ) : dshSessions.length === 0 ? (
            <p className="py-6 text-center text-xs text-fg-dim">暂无可接管的 DSH 会话。</p>
          ) : (
            dshSessions.map((s) => {
              const sid = s.session_id || s.sessionId || s.id || "";
              return (
                <div
                  key={sid}
                  className="flex items-center justify-between gap-3 rounded-xl border border-border bg-elevated/50 p-3"
                >
                  <div className="min-w-0">
                    <div className="truncate text-xs font-semibold text-fg">
                      {s.title || sid}
                    </div>
                    <div className="mt-0.5 truncate font-mono text-[11px] text-fg-faint">
                      {sid} {s.cwd || s.workspace ? `· ${s.cwd || s.workspace}` : ""}
                    </div>
                  </div>
                  <Button
                    size="sm"
                    variant="primary"
                    disabled={!sid || attachingId === sid}
                    onClick={() => void handleAttachDsh(sid)}
                  >
                    {attachingId === sid ? "接管中…" : "接管"}
                  </Button>
                </div>
              );
            })
          )}
        </div>
      </Dialog>
    </div>
  );
}
