import { useCallback, useEffect, useState } from "react";
import { ChevronRight, FileText, Folder, FolderOpen, X } from "lucide-react";
import { apiFetch } from "../../api/client";
import type { components } from "../../api/schema";
import { Button } from "../../components/ui/Button";
import { toast } from "../../components/ui/Toast";

type BackupWorkspaceTreeResponse = components["schemas"]["BackupWorkspaceTreeResponse"];

export const BASE_SCOPE_OPTIONS: Array<{ id: string; label: string; desc: string }> = [
  { id: "config", label: "应用配置", desc: "config/config.yaml" },
  { id: "data", label: "核心库与状态", desc: "对话、记忆图库与定时任务" },
  { id: "prompt", label: "提示词", desc: "prompt/" },
  { id: "skills", label: "技能", desc: "skills/" },
  { id: "plugins", label: "插件", desc: "plugins/" },
];

export function formatBytesCompact(bytes: number | undefined | null): string {
  const n = Number(bytes || 0);
  if (!Number.isFinite(n) || n <= 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

export interface ManualSelectionControlsProps {
  scopes: string[];
  onScopesChange: (next: string[]) => void;
  includeFiles: boolean;
  onIncludeFilesChange: (next: boolean) => void;
  workspacePaths: string[];
  onWorkspacePathsChange: (next: string[]) => void;
}

export function ManualSelectionControls({
  scopes,
  onScopesChange,
  includeFiles,
  onIncludeFilesChange,
  workspacePaths,
  onWorkspacePathsChange,
}: ManualSelectionControlsProps) {
  const [wsOpen, setWsOpen] = useState(false);
  const [wsPath, setWsPath] = useState("");
  const [wsTree, setWsTree] = useState<BackupWorkspaceTreeResponse | null>(null);
  const [wsLoading, setWsLoading] = useState(false);

  const loadWorkspaceDir = useCallback(async (dirPath: string) => {
    setWsLoading(true);
    try {
      const q = dirPath ? `?path=${encodeURIComponent(dirPath)}` : "";
      const res = await apiFetch<BackupWorkspaceTreeResponse>(`/api/backups/workspace${q}`);
      setWsTree(res);
      setWsPath(res.current_path || "");
    } catch (err) {
      toast(err instanceof Error ? err.message : "读取 workspace 目录失败", "error");
    } finally {
      setWsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (wsOpen && wsTree === null && !wsLoading) {
      void loadWorkspaceDir("");
    }
  }, [wsOpen, wsTree, wsLoading, loadWorkspaceDir]);

  const toggleScope = (scopeId: string) => {
    if (scopes.includes(scopeId)) {
      onScopesChange(scopes.filter((s) => s !== scopeId));
    } else {
      onScopesChange([...scopes, scopeId]);
    }
  };

  const toggleWorkspaceEntry = (relPath: string) => {
    if (workspacePaths.includes(relPath)) {
      onWorkspacePathsChange(workspacePaths.filter((p) => p !== relPath));
    } else {
      onWorkspacePathsChange([...workspacePaths, relPath]);
    }
  };

  return (
    <div className="space-y-3">
      {/* 标准模块勾选 */}
      <div className="space-y-1.5">
        <div className="flex items-center justify-between">
          <span className="font-medium text-fg">包含应用模块（默认全选）</span>
          <button
            type="button"
            className="text-[11px] text-accent underline cursor-pointer"
            onClick={() => {
              const allIds = BASE_SCOPE_OPTIONS.map((o) => o.id);
              const allChecked = allIds.every((id) => scopes.includes(id)) && includeFiles;
              onScopesChange(allChecked ? [] : allIds);
              onIncludeFilesChange(!allChecked);
            }}
          >
            {BASE_SCOPE_OPTIONS.every((o) => scopes.includes(o.id)) && includeFiles ? "清空模块" : "全选模块"}
          </button>
        </div>

        <div className="grid grid-cols-2 gap-1.5">
          {BASE_SCOPE_OPTIONS.map((opt) => (
            <label
              key={opt.id}
              className="flex items-start gap-2 rounded-lg border border-border-soft bg-elevated/40 px-2.5 py-2 cursor-pointer"
            >
              <input
                type="checkbox"
                checked={scopes.includes(opt.id)}
                onChange={() => toggleScope(opt.id)}
                className="mt-0.5"
              />
              <div className="min-w-0">
                <div className="font-medium text-fg">{opt.label}</div>
                <div className="truncate text-[10px] text-fg-faint">{opt.desc}</div>
              </div>
            </label>
          ))}
          <label className="flex items-start gap-2 rounded-lg border border-border-soft bg-elevated/40 px-2.5 py-2 cursor-pointer">
            <input
              type="checkbox"
              checked={includeFiles}
              onChange={(e) => onIncludeFilesChange(e.target.checked)}
              className="mt-0.5"
            />
            <div className="min-w-0">
              <div className="font-medium text-fg">包含对话附件</div>
              <div className="truncate text-[10px] text-fg-faint">data/files/</div>
            </div>
          </label>
        </div>
      </div>

      {/* workspace/ 产出文件按需勾选 */}
      <div className="space-y-2 rounded-xl border border-border-soft bg-elevated/30 p-2.5">
        <div className="flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => setWsOpen((v) => !v)}
            className="flex items-center gap-1.5 font-medium text-fg cursor-pointer"
          >
            <FolderOpen className="h-3.5 w-3.5 text-accent" />
            <span>
              勾选 workspace/ 产出文件（可选
              {workspacePaths.length > 0 ? ` · 已选 ${workspacePaths.length} 项` : ""}）
            </span>
          </button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => {
              if (!wsOpen) {
                setWsOpen(true);
              } else {
                void loadWorkspaceDir(wsPath);
              }
            }}
          >
            {wsOpen ? "刷新目录" : "浏览选择"}
          </Button>
        </div>

        {workspacePaths.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {workspacePaths.map((p) => (
              <span
                key={p}
                className="inline-flex items-center gap-1 rounded-md border border-accent/40 bg-accent-soft/40 px-2 py-0.5 font-mono text-[11px] text-fg"
              >
                workspace/{p}
                <button
                  type="button"
                  aria-label={`移除 workspace/${p}`}
                  onClick={() => toggleWorkspaceEntry(p)}
                  className="text-fg-dim hover:text-fg cursor-pointer"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}

        {wsOpen && (
          <div className="space-y-1.5 rounded-lg border border-border bg-bg p-2">
            <div className="flex items-center justify-between gap-2 border-b border-border-soft pb-1.5 text-[11px]">
              <div className="truncate font-mono text-fg-dim">
                workspace{wsPath ? `/${wsPath}` : "/"}
              </div>
              {wsTree?.parent_path !== null && wsTree?.parent_path !== undefined && (
                <button
                  type="button"
                  onClick={() => void loadWorkspaceDir(wsTree.parent_path || "")}
                  className="shrink-0 text-accent underline cursor-pointer"
                >
                  返回上级
                </button>
              )}
            </div>

            {wsLoading ? (
              <div className="py-2 text-center text-[11px] text-fg-faint">读取目录中…</div>
            ) : !wsTree?.exists ? (
              <div className="py-2 text-center text-[11px] text-fg-faint">当前 workspace 目录为空或不存在</div>
            ) : (wsTree.entries || []).length === 0 ? (
              <div className="py-2 text-center text-[11px] text-fg-faint">该目录下暂无可选文件</div>
            ) : (
              <div className="max-h-40 space-y-1 overflow-y-auto pr-1">
                {(wsTree.entries || []).map((entry) => {
                  const checked = workspacePaths.includes(entry.path);
                  return (
                    <div
                      key={entry.path}
                      className="flex items-center justify-between gap-2 rounded-md px-1.5 py-1 hover:bg-hover"
                    >
                      <label className="flex min-w-0 flex-1 items-center gap-2 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleWorkspaceEntry(entry.path)}
                          aria-label={`选择 workspace/${entry.path}`}
                        />
                        {entry.is_dir ? (
                          <Folder className="h-3.5 w-3.5 shrink-0 text-accent" />
                        ) : (
                          <FileText className="h-3.5 w-3.5 shrink-0 text-fg-dim" />
                        )}
                        <span className="truncate font-mono text-[11px] text-fg">{entry.name}</span>
                      </label>
                      {entry.is_dir ? (
                        <button
                          type="button"
                          onClick={() => void loadWorkspaceDir(entry.path)}
                          className="inline-flex shrink-0 items-center gap-0.5 text-[11px] text-accent hover:underline cursor-pointer"
                        >
                          打开
                          <ChevronRight className="h-3 w-3" />
                        </button>
                      ) : (
                        <span className="shrink-0 text-[10px] text-fg-faint">
                          {formatBytesCompact(entry.size)}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
            <div className="text-[10px] text-fg-faint">
              自动跳过 .git、node_modules、.venv、__pycache__ 及 .env/私钥等敏感文件。
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
