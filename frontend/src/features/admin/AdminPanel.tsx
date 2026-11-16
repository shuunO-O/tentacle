import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  Archive,
  Database,
  Download,
  FolderOpen,
  Plus,
  RefreshCw,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { apiDownloadFile, apiDownloadMediaById, apiFetch } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { Input, Textarea } from "../../components/ui/Input";
import { toast } from "../../components/ui/Toast";
import { useIsCompactLayout } from "../workbench/panels";

type AdminSection = "messages" | "sessions" | "summaries" | "goals" | "files" | "backups";

interface TableRowsPayload {
  columns: string[];
  rows: unknown[][];
  total?: number | null;
}

interface StoredFileRow {
  local_id: string;
  filename: string;
  mime?: string;
  size?: number | null;
  kind?: string | null;
  path?: string | null;
}

interface BackupEntry {
  timestamp?: string;
  path?: string;
  size?: number;
  summary?: string;
  [key: string]: unknown;
}

const SECTION_TABS: Array<{ id: AdminSection; label: string }> = [
  { id: "messages", label: "消息记录" },
  { id: "sessions", label: "会话" },
  { id: "summaries", label: "阶段摘要" },
  { id: "goals", label: "目标与任务" },
  { id: "files", label: "附件文件" },
  { id: "backups", label: "自修改备份" },
];

function formatCell(col: string, val: unknown): string {
  if (val === null || val === undefined) return "—";
  if (col.endsWith("_time") && typeof val === "number" && val > 1_000_000) {
    return new Date(val * 1000).toLocaleString();
  }
  return String(val);
}

export function AdminPanel() {
  const compact = useIsCompactLayout();
  const [section, setSection] = useState<AdminSection>("messages");
  const [loading, setLoading] = useState(false);

  // Table state (messages/sessions/summaries)
  const [offset, setOffset] = useState(0);
  const [tableData, setTableData] = useState<TableRowsPayload>({ columns: [], rows: [], total: 0 });

  // Message filters
  const [msgRole, setMsgRole] = useState("");
  const [msgPlatform, setMsgPlatform] = useState("");
  const [msgSessionId, setMsgSessionId] = useState("");
  const [msgQuery, setMsgQuery] = useState("");

  // Goals & Tasks
  const [goalsData, setGoalsData] = useState<TableRowsPayload>({ columns: [], rows: [] });
  const [tasksData, setTasksData] = useState<TableRowsPayload>({ columns: [], rows: [] });
  const [newGoalDesc, setNewGoalDesc] = useState("");
  const [newGoalPriority, setNewGoalPriority] = useState(1);
  const [newTaskContent, setNewTaskContent] = useState("");
  const [newTaskGoalId, setNewTaskGoalId] = useState("");
  const [newTaskUrgency, setNewTaskUrgency] = useState("normal");

  // Files & Backups
  const [files, setFiles] = useState<StoredFileRow[]>([]);
  const [backupPathFilter, setBackupPathFilter] = useState("");
  const [backups, setBackups] = useState<BackupEntry[]>([]);

  const loadSection = useCallback(async () => {
    setLoading(true);
    try {
      if (section === "messages") {
        const qs = new URLSearchParams({
          offset: String(offset),
          limit: "30",
        });
        if (msgRole) qs.set("role", msgRole);
        if (msgPlatform) qs.set("platform", msgPlatform);
        if (msgSessionId) qs.set("session_id", msgSessionId);
        if (msgQuery) qs.set("q", msgQuery);
        setTableData(await apiFetch<TableRowsPayload>(`/api/admin/messages?${qs.toString()}`));
      } else if (section === "sessions") {
        setTableData(
          await apiFetch<TableRowsPayload>(`/api/admin/sessions?offset=${offset}&limit=30`),
        );
      } else if (section === "summaries") {
        setTableData(
          await apiFetch<TableRowsPayload>(`/api/admin/summaries?offset=${offset}&limit=30`),
        );
      } else if (section === "goals") {
        const [g, t] = await Promise.all([
          apiFetch<TableRowsPayload>("/api/admin/goals"),
          apiFetch<TableRowsPayload>("/api/admin/tasks"),
        ]);
        setGoalsData(g);
        setTasksData(t);
      } else if (section === "files") {
        const res = await apiFetch<{ files: StoredFileRow[] }>("/api/files");
        setFiles(res.files || []);
      } else if (section === "backups") {
        const q = backupPathFilter.trim()
          ? `?path=${encodeURIComponent(backupPathFilter.trim())}`
          : "";
        const res = await apiFetch<{ items: BackupEntry[] }>(`/api/admin/backups${q}`);
        setBackups(res.items || []);
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载管理数据失败", "error");
    } finally {
      setLoading(false);
    }
  }, [section, offset, msgRole, msgPlatform, msgSessionId, msgQuery, backupPathFilter]);

  useEffect(() => {
    void loadSection();
  }, [loadSection]);

  const handleDeleteRow = async (kind: "messages" | "summaries", idVal: unknown) => {
    const id = Number(idVal);
    if (!Number.isFinite(id)) return;
    try {
      await apiFetch(`/api/admin/${kind}/${id}`, { method: "DELETE" });
      toast("已删除记录", "ok");
      await loadSection();
    } catch (err) {
      toast(err instanceof Error ? err.message : "删除失败", "error");
    }
  };

  const handleCreateGoal = async (e: FormEvent) => {
    e.preventDefault();
    if (!newGoalDesc.trim()) return;
    try {
      await apiFetch("/api/admin/goals", {
        method: "POST",
        body: JSON.stringify({
          description: newGoalDesc.trim(),
          priority: newGoalPriority,
        }),
      });
      setNewGoalDesc("");
      toast("目标已创建", "ok");
      await loadSection();
    } catch (err) {
      toast(err instanceof Error ? err.message : "创建目标失败", "error");
    }
  };

  const handleGoalAction = async (goalId: number, action: "done" | "archive" | "activate") => {
    try {
      if (action === "archive") {
        await apiFetch(`/api/admin/goals/${goalId}`, { method: "DELETE" });
      } else {
        await apiFetch(`/api/admin/goals/${goalId}`, {
          method: "PUT",
          body: JSON.stringify({ status: action === "done" ? "completed" : "active" }),
        });
      }
      toast("目标状态已更新", "ok");
      await loadSection();
    } catch (err) {
      toast(err instanceof Error ? err.message : "更新目标失败", "error");
    }
  };

  const handleCreateTask = async (e: FormEvent) => {
    e.preventDefault();
    if (!newTaskContent.trim()) return;
    try {
      await apiFetch("/api/admin/tasks", {
        method: "POST",
        body: JSON.stringify({
          content: newTaskContent.trim(),
          goal_id: newTaskGoalId ? Number(newTaskGoalId) : null,
          urgency: newTaskUrgency,
        }),
      });
      setNewTaskContent("");
      toast("任务已创建", "ok");
      await loadSection();
    } catch (err) {
      toast(err instanceof Error ? err.message : "创建任务失败", "error");
    }
  };

  const handleTaskAction = async (taskId: number, action: "done" | "pending" | "delete") => {
    try {
      if (action === "delete") {
        await apiFetch(`/api/admin/tasks/${taskId}`, { method: "DELETE" });
      } else {
        await apiFetch(`/api/admin/tasks/${taskId}`, {
          method: "PUT",
          body: JSON.stringify({ status: action === "done" ? "completed" : "pending" }),
        });
      }
      toast("任务状态已更新", "ok");
      await loadSection();
    } catch (err) {
      toast(err instanceof Error ? err.message : "更新任务失败", "error");
    }
  };

  const handleDeleteFile = async (localId: string) => {
    try {
      await apiFetch(`/api/files/${encodeURIComponent(localId)}`, { method: "DELETE" });
      toast("附件已删除", "ok");
      await loadSection();
    } catch (err) {
      toast(err instanceof Error ? err.message : "删除附件失败", "error");
    }
  };

  const handleRestoreBackup = async (item: BackupEntry) => {
    if (!item.path || !item.timestamp) return;
    if (!window.confirm(`确定回滚 ${item.path} 到备份点 ${item.timestamp}？`)) return;
    try {
      await apiFetch("/api/admin/backups/restore", {
        method: "POST",
        body: JSON.stringify({ path: item.path, timestamp: item.timestamp }),
      });
      toast("文件已恢复到所选备份点", "ok");
      await loadSection();
    } catch (err) {
      toast(err instanceof Error ? err.message : "恢复备份失败", "error");
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      {/* Header + Tabs */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-3 py-2 md:px-4 md:py-2.5">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
          <Database className="h-4 w-4 shrink-0 text-accent" />
          <h2 className="text-sm font-semibold text-fg">记忆数据与系统资源管理</h2>
          {!compact && (
            <div className="ml-2 flex flex-wrap gap-1">
              {SECTION_TABS.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => {
                    setSection(t.id);
                    setOffset(0);
                  }}
                  className={cn(
                    "rounded-lg px-2.5 py-1 text-xs font-medium transition-colors cursor-pointer",
                    section === t.id
                      ? "bg-accent-soft text-accent"
                      : "text-fg-dim hover:bg-hover hover:text-fg",
                  )}
                >
                  {t.label}
                </button>
              ))}
            </div>
          )}
        </div>

        <Button size="sm" variant="secondary" onClick={() => void loadSection()} disabled={loading}>
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          刷新
        </Button>

        {compact && (
          <div className="no-scrollbar flex w-full flex-nowrap items-center gap-1 overflow-x-auto border-t border-border-soft pt-2 whitespace-nowrap">
            {SECTION_TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => {
                  setSection(t.id);
                  setOffset(0);
                }}
                className={cn(
                  "shrink-0 cursor-pointer rounded-lg px-2.5 py-1 text-xs font-medium transition-colors",
                  section === t.id
                    ? "bg-accent-soft text-accent"
                    : "text-fg-dim hover:bg-hover hover:text-fg",
                )}
              >
                {t.label}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1 overflow-y-auto p-3 md:p-4">
        <div className="mx-auto max-w-5xl space-y-4">
          {section === "messages" && (
            <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-surface p-3 text-xs">
              <select
                value={msgRole}
                onChange={(e) => {
                  setMsgRole(e.target.value);
                  setOffset(0);
                }}
                className="h-8 rounded-lg border border-border bg-input px-2 text-xs text-fg"
              >
                <option value="">全部角色</option>
                <option value="user">user</option>
                <option value="assistant">assistant</option>
                <option value="system">system</option>
                <option value="tool">tool</option>
              </select>
              <Input
                value={msgPlatform}
                onChange={(e) => {
                  setMsgPlatform(e.target.value);
                  setOffset(0);
                }}
                placeholder="平台 (web/qq...)"
                className="h-8 w-28 text-xs"
              />
              <Input
                value={msgSessionId}
                onChange={(e) => {
                  setMsgSessionId(e.target.value);
                  setOffset(0);
                }}
                placeholder="Session ID"
                className="h-8 w-36 font-mono text-xs"
              />
              <Input
                value={msgQuery}
                onChange={(e) => {
                  setMsgQuery(e.target.value);
                  setOffset(0);
                }}
                placeholder="正文关键词检索…"
                className="h-8 min-w-44 flex-1 text-xs"
              />
            </div>
          )}

          {(section === "messages" || section === "sessions" || section === "summaries") && (
            <>
              <div className="overflow-x-auto rounded-2xl border border-border bg-surface">
                <table className="w-full border-collapse text-left text-xs">
                  <thead>
                    <tr className="border-b border-border bg-elevated/60 text-fg-dim">
                      {tableData.columns.map((col) => (
                        <th key={col} className="whitespace-nowrap px-3 py-2 font-medium">
                          {col}
                        </th>
                      ))}
                      {section !== "sessions" && (
                        <th className="whitespace-nowrap px-3 py-2 text-right font-medium">操作</th>
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {tableData.rows.length === 0 ? (
                      <tr>
                        <td
                          colSpan={tableData.columns.length + 1}
                          className="px-3 py-8 text-center text-fg-faint"
                        >
                          暂无数据。
                        </td>
                      </tr>
                    ) : (
                      tableData.rows.map((row, idx) => (
                        <tr
                          key={idx}
                          className="border-b border-border-soft last:border-b-0 hover:bg-hover/40"
                        >
                          {tableData.columns.map((col, cIdx) => (
                            <td
                              key={col}
                              className="max-w-xs truncate px-3 py-2 align-top text-fg"
                              title={formatCell(col, row[cIdx])}
                            >
                              {formatCell(col, row[cIdx])}
                            </td>
                          ))}
                          {section !== "sessions" && (
                            <td className="px-3 py-2 text-right">
                              <Button
                                size="sm"
                                variant="danger"
                                onClick={() => void handleDeleteRow(section, row[0])}
                              >
                                <Trash2 className="h-3 w-3" />
                              </Button>
                            </td>
                          )}
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>

              {(tableData.total ?? 0) > 30 && (
                <div className="flex items-center justify-between text-xs text-fg-dim">
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={offset === 0}
                    onClick={() => setOffset((o) => Math.max(0, o - 30))}
                  >
                    上一页
                  </Button>
                  <span>
                    第 {Math.floor(offset / 30) + 1} 页（共 {tableData.total} 条）
                  </span>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={offset + 30 >= (tableData.total ?? 0)}
                    onClick={() => setOffset((o) => o + 30)}
                  >
                    下一页
                  </Button>
                </div>
              )}
            </>
          )}

          {section === "goals" && (
            <div className="grid gap-4 lg:grid-cols-2">
              {/* Goals */}
              <section className="space-y-3 rounded-2xl border border-border bg-surface p-4">
                <h3 className="text-sm font-semibold text-fg">长期目标 (Goals)</h3>
                <form onSubmit={(e) => void handleCreateGoal(e)} className="space-y-2">
                  <Textarea
                    rows={2}
                    value={newGoalDesc}
                    onChange={(e) => setNewGoalDesc(e.target.value)}
                    placeholder="添加新目标描述…"
                    className="text-xs"
                  />
                  <div className="flex items-center justify-between gap-2">
                    <label className="flex items-center gap-1.5 text-xs text-fg-dim">
                      <span>优先级</span>
                      <Input
                        type="number"
                        min={0}
                        max={10}
                        value={newGoalPriority}
                        onChange={(e) => setNewGoalPriority(Number(e.target.value) || 0)}
                        className="h-7 w-16 text-xs"
                      />
                    </label>
                    <Button type="submit" size="sm" variant="primary">
                      <Plus className="h-3.5 w-3.5" />
                      新增目标
                    </Button>
                  </div>
                </form>

                <div className="space-y-2">
                  {goalsData.rows.map((row) => {
                    const id = Number(row[0]);
                    const desc = String(row[1] ?? "");
                    const priority = String(row[2] ?? "");
                    const status = String(row[3] ?? "");
                    return (
                      <div
                        key={id}
                        className="rounded-xl border border-border-soft bg-elevated/50 p-3 text-xs"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-1.5">
                            <span className="font-mono text-fg-faint">#{id}</span>
                            <Badge variant={status === "completed" ? "ok" : status === "active" ? "accent" : "default"}>
                              {status}
                            </Badge>
                            <span className="text-fg-faint">P{priority}</span>
                          </div>
                          <div className="flex gap-1">
                            {status !== "completed" && (
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => void handleGoalAction(id, "done")}
                              >
                                完成
                              </Button>
                            )}
                            {status !== "active" && (
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => void handleGoalAction(id, "activate")}
                              >
                                激活
                              </Button>
                            )}
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => void handleGoalAction(id, "archive")}
                            >
                              <Archive className="h-3 w-3" />
                            </Button>
                          </div>
                        </div>
                        <p className="mt-1.5 text-fg">{desc}</p>
                      </div>
                    );
                  })}
                </div>
              </section>

              {/* Tasks */}
              <section className="space-y-3 rounded-2xl border border-border bg-surface p-4">
                <h3 className="text-sm font-semibold text-fg">行动待办 (Tasks)</h3>
                <form onSubmit={(e) => void handleCreateTask(e)} className="space-y-2">
                  <Textarea
                    rows={2}
                    value={newTaskContent}
                    onChange={(e) => setNewTaskContent(e.target.value)}
                    placeholder="添加具体执行待办…"
                    className="text-xs"
                  />
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <Input
                        value={newTaskGoalId}
                        onChange={(e) => setNewTaskGoalId(e.target.value)}
                        placeholder="关联 Goal ID"
                        className="h-7 w-24 text-xs"
                      />
                      <select
                        value={newTaskUrgency}
                        onChange={(e) => setNewTaskUrgency(e.target.value)}
                        className="h-7 rounded-lg border border-border bg-input px-2 text-xs text-fg"
                      >
                        <option value="low">low</option>
                        <option value="normal">normal</option>
                        <option value="high">high</option>
                      </select>
                    </div>
                    <Button type="submit" size="sm" variant="primary">
                      <Plus className="h-3.5 w-3.5" />
                      新增任务
                    </Button>
                  </div>
                </form>

                <div className="space-y-2">
                  {tasksData.rows.map((row) => {
                    const id = Number(row[0]);
                    const content = String(row[1] ?? "");
                    const status = String(row[2] ?? "");
                    const goalId = row[3];
                    const urgency = String(row[4] ?? "");
                    return (
                      <div
                        key={id}
                        className="rounded-xl border border-border-soft bg-elevated/50 p-3 text-xs"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-1.5">
                            <span className="font-mono text-fg-faint">#{id}</span>
                            <Badge variant={status === "completed" ? "ok" : "warn"}>
                              {status}
                            </Badge>
                            {urgency && <Badge variant="default">{urgency}</Badge>}
                            {goalId != null && (
                              <span className="font-mono text-[10px] text-fg-faint">
                                Goal #{String(goalId)}
                              </span>
                            )}
                          </div>
                          <div className="flex gap-1">
                            {status !== "completed" ? (
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => void handleTaskAction(id, "done")}
                              >
                                完成
                              </Button>
                            ) : (
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => void handleTaskAction(id, "pending")}
                              >
                                重开
                              </Button>
                            )}
                            <Button
                              size="sm"
                              variant="danger"
                              onClick={() => void handleTaskAction(id, "delete")}
                            >
                              <Trash2 className="h-3 w-3" />
                            </Button>
                          </div>
                        </div>
                        <p className="mt-1.5 text-fg">{content}</p>
                      </div>
                    );
                  })}
                </div>
              </section>
            </div>
          )}

          {section === "files" && (
            <div className="space-y-2">
              {files.length === 0 ? (
                <div className="rounded-2xl border border-border bg-surface p-8 text-center text-xs text-fg-dim">
                  暂无已上传或工作区托管附件。
                </div>
              ) : (
                files.map((f) => (
                  <div
                    key={f.local_id}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface px-4 py-3 text-xs"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <FolderOpen className="h-4 w-4 text-accent" />
                        <span className="truncate font-semibold text-fg">{f.filename}</span>
                        {f.kind && <Badge variant="default">{f.kind}</Badge>}
                      </div>
                      <div className="mt-0.5 truncate font-mono text-[11px] text-fg-faint">
                        {f.local_id} · {f.mime || "application/octet-stream"}
                        {typeof f.size === "number" ? ` · ${(f.size / 1024).toFixed(1)} KB` : ""}
                      </div>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() =>
                          void (f.path
                            ? apiDownloadFile(f.path)
                            : apiDownloadMediaById(f.local_id, f.filename))
                        }
                      >
                        <Download className="h-3.5 w-3.5" />
                        下载
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => void handleDeleteFile(f.local_id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                        删除
                      </Button>
                    </div>
                  </div>
                ))
              )}
            </div>
          )}

          {section === "backups" && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Input
                  value={backupPathFilter}
                  onChange={(e) => setBackupPathFilter(e.target.value)}
                  placeholder="按相对文件路径过滤（留空显示全部备份）…"
                  className="h-8 max-w-md text-xs"
                />
                <Button size="sm" variant="secondary" onClick={() => void loadSection()}>
                  过滤
                </Button>
              </div>
              {backups.length === 0 ? (
                <div className="rounded-2xl border border-border bg-surface p-8 text-center text-xs text-fg-dim">
                  暂无自修改文件备份记录。
                </div>
              ) : (
                backups.map((b, idx) => (
                  <div
                    key={`${b.path || ""}-${b.timestamp || idx}`}
                    className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-surface px-4 py-3 text-xs"
                  >
                    <div className="min-w-0">
                      <div className="truncate font-mono font-semibold text-fg">
                        {b.path || "未记录路径"}
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-fg-faint">
                        时间戳：{b.timestamp || "—"}
                        {b.size !== undefined ? ` · ${b.size} bytes` : ""}
                      </div>
                    </div>
                    <Button
                      size="sm"
                      variant="primary"
                      disabled={!b.path || !b.timestamp}
                      onClick={() => void handleRestoreBackup(b)}
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                      回滚此版本
                    </Button>
                  </div>
                ))
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
