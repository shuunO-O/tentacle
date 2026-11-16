import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent } from "react";
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Download,
  HardDrive,
  Lock,
  RefreshCw,
  RotateCcw,
  Trash2,
  Upload,
} from "lucide-react";
import {
  ApiError,
  apiDownloadGet,
  apiDownloadPost,
  apiFetch,
  authHeaders,
  clearStoredToken,
} from "../../api/client";
import type { components } from "../../api/schema";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Input } from "../../components/ui/Input";
import { Switch } from "../../components/ui/Switch";
import { toast } from "../../components/ui/Toast";
import { useAuthStore } from "../auth/authStore";
import {
  BASE_SCOPE_OPTIONS,
  ManualSelectionControls,
  formatBytesCompact as formatBytes,
} from "./ManualSelectionControls";

type BackupStatusResponse = components["schemas"]["BackupStatusResponse"];
type BackupItemDto = components["schemas"]["BackupItemDto"];
type BackupImportInspectResponse = components["schemas"]["BackupImportInspectResponse"];
type BackupImportApplyResponse = components["schemas"]["BackupImportApplyResponse"];

const ALL_SCOPES = BASE_SCOPE_OPTIONS.map((opt) => opt.id);

const INTERVAL_PRESETS = [
  { label: "每 6 小时", hours: 6 },
  { label: "每 12 小时", hours: 12 },
  { label: "每 24 小时（推荐）", hours: 24 },
  { label: "每 3 天", hours: 72 },
  { label: "每 7 天", hours: 168 },
];

const KIND_LABELS: Record<string, { label: string; variant: "default" | "accent" | "warn" }> = {
  auto: { label: "自动", variant: "default" },
  manual: { label: "手动", variant: "accent" },
  pre_import: { label: "导入前保护", variant: "warn" },
};

function formatTs(ts: number | string | null | undefined): string {
  if (!ts) return "—";
  const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString();
}

export interface BackupSectionProps {
  authSignature?: string;
  onRestored?: () => void;
}

export function BackupSection({ authSignature, onRestored }: BackupSectionProps) {
  const bootAuth = useAuthStore((s) => s.boot);

  const [status, setStatus] = useState<BackupStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);

  // Policy draft
  const [enabled, setEnabled] = useState(true);
  const [intervalHours, setIntervalHours] = useState(24);
  const [retentionCount, setRetentionCount] = useState(7);
  const [includeFiles, setIncludeFiles] = useState(true);
  const [savingPolicy, setSavingPolicy] = useState(false);

  // Running actions & Manual Backup modal
  const [manualOpen, setManualOpen] = useState(false);
  const [manualScopes, setManualScopes] = useState<string[]>(ALL_SCOPES);
  const [manualIncludeFiles, setManualIncludeFiles] = useState(true);
  const [manualWorkspacePaths, setManualWorkspacePaths] = useState<string[]>([]);
  const [creatingBackup, setCreatingBackup] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  // Ephemeral Export modal (includeEnv ALWAYS resets to false every open)
  const [exportOpen, setExportOpen] = useState(false);
  const [exportScopes, setExportScopes] = useState<string[]>(ALL_SCOPES);
  const [exportIncludeFiles, setExportIncludeFiles] = useState(true);
  const [exportWorkspacePaths, setExportWorkspacePaths] = useState<string[]>([]);
  const [exportIncludeEnv, setExportIncludeEnv] = useState(false);
  const [exportPassphrase, setExportPassphrase] = useState("");
  const [exportPassphraseConfirm, setExportPassphraseConfirm] = useState("");
  const [exportShowOptionalPassphrase, setExportShowOptionalPassphrase] = useState(false);
  const [exporting, setExporting] = useState(false);

  // Upload / Inspect / Apply state
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const [passphrasePrompt, setPassphrasePrompt] = useState<{
    file?: File;
    backupId?: string;
    value: string;
    error?: string;
  } | null>(null);
  const [inspectResult, setInspectResult] = useState<BackupImportInspectResponse | null>(null);
  const [applyFiles, setApplyFiles] = useState(true);
  const [applyEnv, setApplyEnv] = useState(false);
  const [applyWorkspace, setApplyWorkspace] = useState(true);
  const [createPreBackup, setCreatePreBackup] = useState(true);
  const [applyingImport, setApplyingImport] = useState(false);

  // Post-apply restart polling
  const [restartingState, setRestartingState] = useState<{
    active: boolean;
    message: string;
  }>({ active: false, message: "" });
  const unmountedRef = useRef(false);

  useEffect(() => {
    unmountedRef.current = false;
    return () => {
      unmountedRef.current = true;
    };
  }, []);

  const syncFormFromStatus = useCallback((snap: BackupStatusResponse) => {
    setStatus(snap);
    setEnabled(snap.policy.enabled);
    setIntervalHours(snap.policy.interval_hours);
    setRetentionCount(snap.policy.retention_count);
    setIncludeFiles(snap.policy.include_files);
  }, []);

  const loadBackupStatus = useCallback(async () => {
    setLoading(true);
    try {
      const snap = await apiFetch<BackupStatusResponse>("/api/backups");
      if (!unmountedRef.current) {
        syncFormFromStatus(snap);
      }
    } catch (err) {
      if (!unmountedRef.current) {
        toast(err instanceof Error ? err.message : "读取备份状态失败", "error");
      }
    } finally {
      if (!unmountedRef.current) {
        setLoading(false);
      }
    }
  }, [syncFormFromStatus]);

  useEffect(() => {
    void loadBackupStatus();
  }, [loadBackupStatus, authSignature]);

  const handleSavePolicy = async (e: FormEvent) => {
    e.preventDefault();
    const hours = Math.round(Number(intervalHours));
    const ret = Math.round(Number(retentionCount));
    if (!Number.isFinite(hours) || hours < 1 || hours > 720) {
      toast("备份间隔需在 1 ~ 720 小时之间", "error");
      return;
    }
    if (!Number.isFinite(ret) || ret < 1 || ret > 100) {
      toast("自动保留份数需在 1 ~ 100 份之间", "error");
      return;
    }
    setSavingPolicy(true);
    try {
      await apiFetch("/api/backups/policy", {
        method: "PUT",
        body: JSON.stringify({
          enabled,
          interval_hours: hours,
          retention_count: ret,
          include_files: includeFiles,
        }),
      });
      toast("定期备份策略已保存", "ok");
      await loadBackupStatus();
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存备份策略失败", "error");
    } finally {
      setSavingPolicy(false);
    }
  };

  const openManualBackupDialog = () => {
    setManualScopes(ALL_SCOPES);
    setManualIncludeFiles(status?.policy.include_files ?? includeFiles);
    setManualWorkspacePaths([]);
    setManualOpen(true);
  };

  const handleCreateBackupSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (manualScopes.length === 0 && !manualIncludeFiles && manualWorkspacePaths.length === 0) {
      toast("请至少勾选一项模块、对话附件或工作区文件", "error");
      return;
    }
    setCreatingBackup(true);
    try {
      await apiFetch("/api/backups/now", {
        method: "POST",
        body: JSON.stringify({
          include_files: manualIncludeFiles,
          scopes: manualScopes,
          ...(manualWorkspacePaths.length > 0 ? { workspace_paths: manualWorkspacePaths } : {}),
        }),
      });
      toast("已完成本机快照备份", "ok");
      setManualOpen(false);
      await loadBackupStatus();
    } catch (err) {
      toast(err instanceof Error ? err.message : "立即备份失败", "error");
    } finally {
      setCreatingBackup(false);
    }
  };

  const handleDeleteBackup = async (backupId: string) => {
    if (!window.confirm(`确定删除本地备份 ${backupId}？此操作不可恢复。`)) return;
    setDeletingId(backupId);
    try {
      await apiFetch(`/api/backups/${encodeURIComponent(backupId)}`, { method: "DELETE" });
      toast("已删除本地备份", "ok");
      await loadBackupStatus();
    } catch (err) {
      toast(err instanceof Error ? err.message : "删除备份失败", "error");
    } finally {
      setDeletingId(null);
    }
  };

  const openExportDialog = () => {
    // 每次手动打开导出窗口都重置 .env 勾选与口令输入，防止误带出明文凭据
    setExportScopes(ALL_SCOPES);
    setExportIncludeFiles(status?.policy.include_files ?? true);
    setExportWorkspacePaths([]);
    setExportIncludeEnv(false);
    setExportPassphrase("");
    setExportPassphraseConfirm("");
    setExportShowOptionalPassphrase(false);
    setExportOpen(true);
  };

  const handleExportSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (
      exportScopes.length === 0 &&
      !exportIncludeFiles &&
      exportWorkspacePaths.length === 0 &&
      !exportIncludeEnv
    ) {
      toast("请至少勾选一项导出内容或工作区文件", "error");
      return;
    }
    if (exportIncludeEnv) {
      if (exportPassphrase.length < 8) {
        toast("包含 .env 时必须设置至少 8 位加密口令", "error");
        return;
      }
    }
    if (exportPassphrase) {
      if (exportPassphrase.length < 8) {
        toast("加密口令至少需要 8 位字符", "error");
        return;
      }
      if (exportPassphrase !== exportPassphraseConfirm) {
        toast("两次输入的加密口令不一致", "error");
        return;
      }
    }
    setExporting(true);
    try {
      await apiDownloadPost(
        "/api/backups/export",
        {
          include_files: exportIncludeFiles,
          include_env: exportIncludeEnv,
          scopes: exportScopes,
          ...(exportWorkspacePaths.length > 0 ? { workspace_paths: exportWorkspacePaths } : {}),
          ...(exportPassphrase ? { passphrase: exportPassphrase } : {}),
        },
        exportPassphrase ? "tentacle-migration.tbackup" : "tentacle-migration.tar.gz",
        "导出迁移包失败",
      );
      toast(exportPassphrase ? "已生成并下载加密迁移包" : "已生成并下载迁移包", "ok");
      setExportOpen(false);
      setExportIncludeEnv(false);
      setExportPassphrase("");
      setExportPassphraseConfirm("");
      setExportShowOptionalPassphrase(false);
    } catch (err) {
      toast(err instanceof Error ? err.message : "导出失败", "error");
    } finally {
      setExporting(false);
    }
  };

  const runInspect = useCallback(
    async (opts: { file?: File; backupId?: string; passphrase?: string }) => {
      setInspecting(true);
      try {
        const form = new FormData();
        if (opts.file) {
          form.append("file", opts.file);
        } else if (opts.backupId) {
          form.append("backup_id", opts.backupId);
        }
        if (opts.passphrase) {
          form.append("passphrase", opts.passphrase);
        }
        const preview = await apiFetch<BackupImportInspectResponse>("/api/backups/import/inspect", {
          method: "POST",
          body: form,
        });
        setPassphrasePrompt(null);
        setInspectResult(preview);
        setApplyFiles(Boolean(preview.has_files));
        setApplyEnv(Boolean(preview.includes_env));
        setApplyWorkspace(Boolean(preview.has_workspace));
        setCreatePreBackup(true);
      } catch (err) {
        if (err instanceof ApiError && err.code === "passphrase_required") {
          setPassphrasePrompt({
            file: opts.file,
            backupId: opts.backupId,
            value: opts.passphrase || "",
            error: opts.passphrase ? "解密口令不正确或未输入，请重新输入" : undefined,
          });
        } else {
          toast(err instanceof Error ? err.message : "校验迁移包失败", "error");
        }
      } finally {
        setInspecting(false);
      }
    },
    [],
  );

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] || null;
    setSelectedFile(file);
  };

  const waitForRestartRecovery = useCallback(
    async (oldInstanceId: string | null | undefined) => {
      setRestartingState({
        active: true,
        message: "正在等待当前对话回合结束并重启应用以完成数据切换…",
      });
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => window.setTimeout(r, 1200));
        if (unmountedRef.current) return;
        try {
          const res = await fetch("/api/backups", { headers: authHeaders() });
          if (res.status === 401) {
            clearStoredToken();
            setRestartingState({ active: false, message: "" });
            toast("应用已重启并完成迁移恢复，请重新验证身份", "ok");
            await bootAuth();
            return;
          }
          if (!res.ok) continue;
          const next = (await res.json()) as BackupStatusResponse;
          const switched = oldInstanceId ? next.instance_id && next.instance_id !== oldInstanceId : i >= 2;
          if (switched) {
            setRestartingState({ active: false, message: "" });
            syncFormFromStatus(next);
            toast("备份恢复完成，应用已重启就绪", "ok");
            onRestored?.();
            return;
          }
        } catch {
          // 重启停服窗口连接中断属正常现象，继续轮询
        }
      }
      if (!unmountedRef.current) {
        setRestartingState({
          active: true,
          message: "等待超时，若服务已在后台重启完成请手动刷新页面。",
        });
      }
    },
    [bootAuth, onRestored, syncFormFromStatus],
  );

  const handleApplyImport = async () => {
    if (!inspectResult) return;
    setApplyingImport(true);
    try {
      const res = await apiFetch<BackupImportApplyResponse>("/api/backups/import/apply", {
        method: "POST",
        body: JSON.stringify({
          import_token: inspectResult.import_token,
          apply_files: applyFiles,
          apply_env: applyEnv,
          apply_workspace: applyWorkspace,
          create_pre_backup: createPreBackup,
        }),
      });
      setInspectResult(null);
      setSelectedFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      void waitForRestartRecovery(res.instance_id || status?.instance_id);
    } catch (err) {
      toast(err instanceof Error ? err.message : "提交导入恢复失败", "error");
    } finally {
      setApplyingImport(false);
    }
  };

  const handleAckLastRestore = async () => {
    try {
      await apiFetch("/api/backups/last-restore", { method: "DELETE" });
      await loadBackupStatus();
    } catch (err) {
      toast(err instanceof Error ? err.message : "关闭提示失败", "error");
    }
  };

  const effectiveMissing = inspectResult
    ? applyEnv
      ? inspectResult.missing_if_apply_env || []
      : inspectResult.missing_if_keep_env || []
    : [];

  return (
    <section className="space-y-4 rounded-2xl border border-border bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <HardDrive className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-semibold text-fg">定期备份与迁移导入导出</h3>
          {status && (
            <Badge variant="default">数据代号 #{status.data_epoch}</Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={loading}
            onClick={() => void loadBackupStatus()}
          >
            <RefreshCw className="h-3.5 w-3.5" />
            刷新
          </Button>
          <Button
            type="button"
            size="sm"
            variant="secondary"
            disabled={creatingBackup || Boolean(status?.running)}
            onClick={openManualBackupDialog}
          >
            <Archive className="h-3.5 w-3.5" />
            {creatingBackup ? "备份中…" : "立即备份"}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="primary"
            disabled={exporting || Boolean(status?.running)}
            onClick={openExportDialog}
          >
            <Download className="h-3.5 w-3.5" />
            导出迁移包
          </Button>
        </div>
      </div>

      <p className="text-[11px] leading-relaxed text-fg-faint">
        自动备份覆盖 <code className="text-fg-dim">workspace/</code> 之外的记忆图库、对话记录、定时任务、配置、提示词、技能、插件及可选附件；定期快照与常规导出默认不含{" "}
        <code className="text-fg-dim">.env</code> 凭据及 Web 鉴权设备文件。
      </p>

      {/* 上次导入恢复回执横幅 */}
      {status?.last_restore && (
        <div
          className={
            status.last_restore.status === "succeeded"
              ? "space-y-2 rounded-xl border border-ok/40 bg-ok/10 p-3 text-xs"
              : "space-y-2 rounded-xl border border-danger/40 bg-danger/10 p-3 text-xs"
          }
        >
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 font-medium text-fg">
              {status.last_restore.status === "succeeded" ? (
                <CheckCircle2 className="h-4 w-4 text-ok" />
              ) : (
                <AlertTriangle className="h-4 w-4 text-danger" />
              )}
              <span>
                {status.last_restore.status === "succeeded"
                  ? `已于 ${formatTs(status.last_restore.applied_at)} 完成备份恢复`
                  : `上次恢复失败：${status.last_restore.error || "未知错误"}`}
              </span>
            </div>
            <Button size="sm" variant="ghost" onClick={() => void handleAckLastRestore()}>
              知道了
            </Button>
          </div>
          {(status.last_restore.missing_env_vars || []).length > 0 && (
            <div className="text-[11px] text-fg-dim">
              当前缺少配置引用的环境变量，请前往「应用配置」补填：
              <span className="ml-1 font-mono text-warn">
                {(status.last_restore.missing_env_vars || []).join(", ")}
              </span>
            </div>
          )}
        </div>
      )}

      {/* 定期策略表单 */}
      <form
        onSubmit={(e) => void handleSavePolicy(e)}
        className="space-y-3 rounded-xl border border-border-soft bg-elevated/40 p-3"
      >
        <div className="flex items-center justify-between gap-4 border-b border-border-soft pb-2.5">
          <div>
            <div className="text-xs font-medium text-fg">开启定期自动备份</div>
            <div className="text-[11px] text-fg-faint">
              静默在后台运行；仅对「自动」备份按保留份数滚动淘汰，不影响手动留档
            </div>
          </div>
          <Switch
            checked={enabled}
            onCheckedChange={setEnabled}
            aria-label="开启定期自动备份"
          />
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="mb-1 block text-xs text-fg-dim">备份间隔（小时）</label>
            <div className="flex flex-wrap items-center gap-1.5">
              <select
                aria-label="常用备份间隔"
                value={INTERVAL_PRESETS.some((p) => p.hours === intervalHours) ? String(intervalHours) : "custom"}
                onChange={(e) => {
                  if (e.target.value !== "custom") {
                    setIntervalHours(Number(e.target.value));
                  }
                }}
                className="h-8 rounded-lg border border-border bg-bg px-2 text-xs text-fg focus:border-accent focus:outline-none"
              >
                {INTERVAL_PRESETS.map((p) => (
                  <option key={p.hours} value={String(p.hours)}>
                    {p.label}
                  </option>
                ))}
                <option value="custom">自定义小时</option>
              </select>
              <Input
                type="number"
                min={1}
                max={720}
                value={intervalHours}
                onChange={(e) => setIntervalHours(Number(e.target.value))}
                className="h-8 w-24 text-xs"
                aria-label="自定义备份间隔小时数"
              />
            </div>
          </div>

          <div>
            <label className="mb-1 block text-xs text-fg-dim">自动备份保留份数（1~100）</label>
            <Input
              type="number"
              min={1}
              max={100}
              value={retentionCount}
              onChange={(e) => setRetentionCount(Number(e.target.value))}
              className="h-8 w-28 text-xs"
              aria-label="自动备份保留份数"
            />
          </div>
        </div>

        <div className="flex items-center justify-between gap-4 pt-1">
          <div>
            <div className="text-xs font-medium text-fg">自动备份包含对话附件（data/files/）</div>
            <div className="text-[11px] text-fg-faint">
              若图片/文档附件较多且希望节省磁盘空间，可关闭此项并在完整迁移时再单独导出
            </div>
          </div>
          <Switch
            checked={includeFiles}
            onCheckedChange={setIncludeFiles}
            aria-label="自动备份包含对话附件"
          />
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border-soft pt-2.5 text-[11px] text-fg-faint">
          <div className="space-y-0.5">
            <div>
              最近自动备份：{formatTs(status?.policy.last_auto_backup_at)}
              {status?.policy.last_status === "failed" && status?.policy.last_error && (
                <span className="ml-2 text-danger">失败：{status.policy.last_error}</span>
              )}
            </div>
            <div>
              下次预计运行：{status?.policy.enabled ? formatTs(status?.next_run_at) : "已暂停"} · 当前留存{" "}
              {status?.backups.length ?? 0} 份（{formatBytes(status?.total_bytes)}）
            </div>
          </div>
          <Button type="submit" size="sm" variant="secondary" disabled={savingPolicy}>
            {savingPolicy ? "保存中…" : "保存策略"}
          </Button>
        </div>
      </form>

      {/* 导入迁移包 */}
      <div className="space-y-2 rounded-xl border border-border-soft bg-elevated/40 p-3">
        <div className="flex items-center gap-2">
          <Upload className="h-3.5 w-3.5 text-accent" />
          <span className="text-xs font-medium text-fg">从迁移包导入恢复</span>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={fileInputRef}
            type="file"
            accept=".tar.gz,.tgz,.tbackup"
            onChange={handleFileChange}
            aria-label="选择迁移包文件"
            className="block max-w-full text-xs text-fg-dim file:mr-2 file:rounded-lg file:border file:border-border file:bg-surface file:px-2.5 file:py-1 file:text-xs file:font-medium file:text-fg hover:file:bg-hover"
          />
          <Button
            type="button"
            size="sm"
            variant="secondary"
            disabled={!selectedFile || inspecting}
            onClick={() => selectedFile && void runInspect({ file: selectedFile })}
          >
            {inspecting ? "校验中…" : "校验并预览导入"}
          </Button>
        </div>
        <p className="text-[11px] text-fg-faint">
          支持未加密的 <code className="text-fg-dim">.tar.gz</code> 与含加密 <code className="text-fg-dim">.env</code> 的{" "}
          <code className="text-fg-dim">.tbackup</code>；导入始终跳过 Web 访问密码与设备绑定文件。
        </p>
      </div>

      {/* 本地历史备份列表 */}
      <div className="space-y-2">
        <div className="text-xs font-medium text-fg-dim">
          本机保留备份（{status?.backups.length ?? 0}）
        </div>
        {loading && !status ? (
          <p className="text-xs text-fg-faint">正在读取备份记录…</p>
        ) : (status?.backups.length || 0) === 0 ? (
          <p className="text-xs text-fg-faint">暂无本地备份，点击上方「立即备份」即可创建第一份快照。</p>
        ) : (
          <div className="space-y-1.5">
            {(status?.backups || []).map((item: BackupItemDto) => {
              const kindBadge = KIND_LABELS[item.kind] || { label: item.kind, variant: "default" as const };
              return (
                <div
                  key={item.backup_id}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border-soft bg-elevated/40 px-3 py-2 text-xs"
                >
                  <div className="min-w-0 space-y-0.5">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="font-mono font-medium text-fg">{item.backup_id}</span>
                      <Badge variant={kindBadge.variant}>{kindBadge.label}</Badge>
                      <Badge variant="default">
                        {item.includes_files ? "含附件" : "无附件"}
                      </Badge>
                      {item.includes_workspace && (
                        <Badge variant="accent">含工作区 ({item.workspace_file_count || 0})</Badge>
                      )}
                      {item.included_scopes && item.included_scopes.length < ALL_SCOPES.length && (
                        <Badge variant="warn">部分模块</Badge>
                      )}
                    </div>
                    <div className="text-[11px] text-fg-faint">
                      {formatTs(item.created_at)} · {item.file_count} 个文件 · {formatBytes(item.archive_bytes)}
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        void apiDownloadGet(
                          `/api/backups/${encodeURIComponent(item.backup_id)}/download`,
                          `${item.backup_id}.tar.gz`,
                          "下载备份失败",
                        ).catch((err) => toast(err instanceof Error ? err.message : "下载失败", "error"))
                      }
                    >
                      <Download className="h-3.5 w-3.5" />
                      下载
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={inspecting}
                      onClick={() => void runInspect({ backupId: item.backup_id })}
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                      恢复
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={deletingId === item.backup_id}
                      onClick={() => void handleDeleteBackup(item.backup_id)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* 手动备份选择 Dialog */}
      <Dialog
        open={manualOpen}
        onOpenChange={setManualOpen}
        title="创建手动备份"
        description="默认备份全部核心数据；可按需取消部分模块或从 workspace/ 勾选需要保留的 Agent 产出。"
        className="max-h-[90vh] w-[min(94vw,560px)] overflow-y-auto"
      >
        <form onSubmit={(e) => void handleCreateBackupSubmit(e)} className="space-y-3 text-xs">
          <ManualSelectionControls
            scopes={manualScopes}
            onScopesChange={setManualScopes}
            includeFiles={manualIncludeFiles}
            onIncludeFilesChange={setManualIncludeFiles}
            workspacePaths={manualWorkspacePaths}
            onWorkspacePathsChange={setManualWorkspacePaths}
          />
          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" size="sm" variant="ghost" onClick={() => setManualOpen(false)}>
              取消
            </Button>
            <Button type="submit" size="sm" variant="primary" disabled={creatingBackup}>
              {creatingBackup ? "正在创建…" : "创建本机快照"}
            </Button>
          </div>
        </form>
      </Dialog>

      {/* 一次性导出 Dialog */}
      <Dialog
        open={exportOpen}
        onOpenChange={(open) => {
          setExportOpen(open);
          if (!open) {
            setExportIncludeEnv(false);
            setExportPassphrase("");
            setExportPassphraseConfirm("");
            setExportShowOptionalPassphrase(false);
          }
        }}
        title="导出迁移包"
        description="按需勾选应用模块或 workspace/ 产出文件打包下载，用于迁移至新主机。"
        className="max-h-[90vh] w-[min(94vw,560px)] overflow-y-auto"
      >
        <form onSubmit={(e) => void handleExportSubmit(e)} className="space-y-3 text-xs">
          <ManualSelectionControls
            scopes={exportScopes}
            onScopesChange={setExportScopes}
            includeFiles={exportIncludeFiles}
            onIncludeFilesChange={setExportIncludeFiles}
            workspacePaths={exportWorkspacePaths}
            onWorkspacePathsChange={setExportWorkspacePaths}
          />

          <label
            className={
              status?.env_export_allowed
                ? "flex items-start gap-2.5 rounded-xl border border-warn/40 bg-warn/10 p-3 cursor-pointer"
                : "flex items-start gap-2.5 rounded-xl border border-border-soft bg-elevated/30 p-3 opacity-60 cursor-not-allowed"
            }
          >
            <input
              type="checkbox"
              checked={exportIncludeEnv}
              disabled={!status?.env_export_allowed}
              onChange={(e) => setExportIncludeEnv(e.target.checked)}
              className="mt-0.5"
            />
            <div>
              <div className="flex items-center gap-1.5 font-medium text-fg">
                <Lock className="h-3.5 w-3.5 text-warn" />
                <span>包含凭据文件（.env，仅此次手动导出生效）</span>
              </div>
              <div className="mt-0.5 text-[11px] text-fg-dim">
                {status?.env_export_allowed
                  ? "须设置至少 8 位口令进行端到端加密（.tbackup），仅本次流式下载、绝不保存至服务端备份目录。"
                  : "需先设置 Web 访问密码或开启设备 Cookie 验证后，才允许手动导出加密 .env。"}
              </div>
            </div>
          </label>

          {(exportIncludeEnv || exportShowOptionalPassphrase) && (
            <div className="space-y-2 rounded-xl border border-border-soft bg-elevated/40 p-3">
              <div>
                <label className="mb-1 block text-xs text-fg-dim">
                  {exportIncludeEnv ? "设置加密口令（必填，至少 8 位）" : "可选加密口令（至少 8 位）"}
                </label>
                <Input
                  type="password"
                  value={exportPassphrase}
                  onChange={(e) => setExportPassphrase(e.target.value)}
                  placeholder="输入口令…"
                  autoComplete="new-password"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-fg-dim">确认加密口令</label>
                <Input
                  type="password"
                  value={exportPassphraseConfirm}
                  onChange={(e) => setExportPassphraseConfirm(e.target.value)}
                  placeholder="再次输入口令…"
                  autoComplete="new-password"
                />
              </div>
            </div>
          )}

          {!exportIncludeEnv && !exportShowOptionalPassphrase && (
            <div className="text-[11px] text-fg-faint">
              未勾选 .env 时将生成标准 <code className="text-fg-dim">.tar.gz</code>（仅含变量名清单，不含密钥值）；你也可以{" "}
              <button
                type="button"
                className="text-accent underline cursor-pointer"
                onClick={() => setExportShowOptionalPassphrase(true)}
              >
                可选设置口令加密
              </button>
              。
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" size="sm" variant="ghost" onClick={() => setExportOpen(false)}>
              取消
            </Button>
            <Button type="submit" size="sm" variant="primary" disabled={exporting}>
              {exporting ? "打包导出中…" : "生成并下载迁移包"}
            </Button>
          </div>
        </form>
      </Dialog>

      {/* 解密口令弹窗 */}
      <Dialog
        open={passphrasePrompt !== null}
        onOpenChange={(open) => {
          if (!open) setPassphrasePrompt(null);
        }}
        title="输入迁移包解密口令"
        description="检测到此归档已加密（.tbackup），请输入导出时设置的解密口令以校验内容。"
      >
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!passphrasePrompt) return;
            void runInspect({
              file: passphrasePrompt.file,
              backupId: passphrasePrompt.backupId,
              passphrase: passphrasePrompt.value,
            });
          }}
          className="space-y-3 text-xs"
        >
          <Input
            type="password"
            value={passphrasePrompt?.value || ""}
            onChange={(e) =>
              setPassphrasePrompt((prev) => (prev ? { ...prev, value: e.target.value, error: undefined } : prev))
            }
            placeholder="输入解密口令…"
            autoFocus
          />
          {passphrasePrompt?.error && (
            <div className="text-[11px] text-danger">{passphrasePrompt.error}</div>
          )}
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="ghost" onClick={() => setPassphrasePrompt(null)}>
              取消
            </Button>
            <Button type="submit" size="sm" variant="primary" disabled={inspecting || !passphrasePrompt?.value}>
              {inspecting ? "解密校验中…" : "解密并预览"}
            </Button>
          </div>
        </form>
      </Dialog>

      {/* 导入预览确认 Dialog */}
      <Dialog
        open={inspectResult !== null}
        onOpenChange={(open) => {
          if (!open) setInspectResult(null);
        }}
        title="确认导入恢复"
        description="请核对迁移包内容。确认后将等待当前对话回合结束并自动重启应用完成切换。"
        className="max-h-[90vh] overflow-y-auto"
      >
        {inspectResult && (
          <div className="space-y-3 text-xs">
            <div className="space-y-1.5 rounded-xl border border-border-soft bg-elevated/50 p-3">
              <div>
                来源备份：<span className="font-mono text-fg">{inspectResult.backup_id || "上传归档"}</span>
              </div>
              <div>创建时间：{formatTs(inspectResult.backup_created_at)}</div>
              <div>
                数据代号：#{inspectResult.data_epoch} · 文件数：{inspectResult.file_count}（
                {formatBytes(inspectResult.payload_bytes)}）
              </div>
              <div className="flex flex-wrap items-center gap-1 pt-1">
                <span className="text-fg-dim">包含模块：</span>
                {(inspectResult.modules || []).map((mod) => (
                  <Badge key={mod} variant="default">
                    {mod}
                  </Badge>
                ))}
                {inspectResult.includes_env && (
                  <Badge variant="warn">含加密 .env（{inspectResult.env_key_count} 项）</Badge>
                )}
              </div>
              {inspectResult.included_scopes && inspectResult.included_scopes.length < ALL_SCOPES.length && (
                <div className="text-[11px] text-accent">
                  局部备份：仅更新所含模块（
                  {inspectResult.included_scopes.join(" / ") || "无基础模块"}
                  {inspectResult.has_files ? " + files" : ""}
                  {inspectResult.has_workspace ? " + workspace" : ""}），未包含模块保持目标机原样。
                </div>
              )}
            </div>

            {inspectResult.has_files && (
              <label className="flex items-start gap-2 rounded-xl border border-border-soft bg-elevated/30 p-2.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={applyFiles}
                  onChange={(e) => setApplyFiles(e.target.checked)}
                  className="mt-0.5"
                />
                <span>
                  恢复对话附件（data/files/，共 {inspectResult.files_count} 个 /{" "}
                  {formatBytes(inspectResult.files_bytes)}）
                </span>
              </label>
            )}

            {inspectResult.has_workspace && (
              <div className="space-y-1.5 rounded-xl border border-border-soft bg-elevated/30 p-2.5">
                <label className="flex items-start gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={applyWorkspace}
                    onChange={(e) => setApplyWorkspace(e.target.checked)}
                    className="mt-0.5"
                  />
                  <span>
                    合并恢复包含的 workspace/ 产出文件（共 {inspectResult.workspace_files_count} 个 /{" "}
                    {formatBytes(inspectResult.workspace_bytes)}，仅写入同路径，不删除目标工作区其他文件）
                  </span>
                </label>
                {(inspectResult.workspace_sample_paths || []).length > 0 && (
                  <div className="max-h-20 overflow-y-auto pl-5 font-mono text-[10px] text-fg-faint">
                    {(inspectResult.workspace_sample_paths || []).map((p) => (
                      <div key={p}>workspace/{p}</div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {inspectResult.includes_env && (
              <label className="flex items-start gap-2 rounded-xl border border-warn/40 bg-warn/10 p-2.5 cursor-pointer">
                <input
                  type="checkbox"
                  checked={applyEnv}
                  onChange={(e) => setApplyEnv(e.target.checked)}
                  className="mt-0.5"
                />
                <span>一并恢复包内的 .env 凭据（将覆盖本机现有 config/.env）</span>
              </label>
            )}

            <label className="flex items-start gap-2 rounded-xl border border-border-soft bg-elevated/30 p-2.5 cursor-pointer">
              <input
                type="checkbox"
                checked={createPreBackup}
                onChange={(e) => setCreatePreBackup(e.target.checked)}
                className="mt-0.5"
              />
              <span>导入前先为当前机器自动创建一份保护快照（推荐）</span>
            </label>

            {effectiveMissing.length > 0 && (
              <div className="rounded-xl border border-warn/40 bg-warn/10 p-2.5 text-[11px] text-fg-dim">
                注意：按当前选项恢复后，本机仍缺少以下配置所需的环境变量（需在重启后前往「应用配置」补填）：
                <div className="mt-1 font-mono text-warn">{effectiveMissing.join(", ")}</div>
              </div>
            )}

            <div className="text-[11px] text-fg-faint">
              不会保留或覆盖本机当前 Web 访问密码、显示开关及受信任设备绑定记录。
            </div>

            {!status?.restart_available && (
              <div className="rounded-xl border border-danger/40 bg-danger/10 p-2.5 text-[11px] text-danger">
                当前运行时未启用自动重启，无法通过 Web 直接切换。请停止服务后使用{" "}
                <code>scripts/backup_home.py import --apply</code> 完成导入。
              </div>
            )}

            <div className="flex justify-end gap-2 pt-1">
              <Button type="button" size="sm" variant="ghost" onClick={() => setInspectResult(null)}>
                取消
              </Button>
              <Button
                type="button"
                size="sm"
                variant="primary"
                disabled={applyingImport || !status?.restart_available}
                onClick={() => void handleApplyImport()}
              >
                {applyingImport ? "正在准备切换…" : "确认导入并重启恢复"}
              </Button>
            </div>
          </div>
        )}
      </Dialog>

      {/* 重启等待遮罩 Dialog */}
      <Dialog
        open={restartingState.active}
        onOpenChange={() => undefined}
        preventClose
        hideClose
        title="正在重启应用并恢复备份"
      >
        <div className="space-y-3 text-xs text-fg-dim">
          <p>{restartingState.message}</p>
          <div className="flex justify-end">
            <Button size="sm" variant="secondary" onClick={() => window.location.reload()}>
              手动刷新页面
            </Button>
          </div>
        </div>
      </Dialog>
    </section>
  );
}
