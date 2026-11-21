import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  AppWindow,
  ChevronDown,
  ChevronRight,
  Globe,
  KeyRound,
  Plug,
  Plus,
  RefreshCw,
  RotateCw,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { apiFetch } from "../../api/client";
import { Badge, type BadgeVariant } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Input, Textarea } from "../../components/ui/Input";
import { Switch } from "../../components/ui/Switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/Tabs";
import { toast } from "../../components/ui/Toast";
import { useLayoutStore } from "../workbench/layoutStore";
import { pluginPanelId } from "../workbench/panels";
import { BrowserNekoControl } from "../browser/BrowserNekoControl";

export interface PermissionDetail {
  permission: string;
  level: string;
  summary: string;
}

export interface ConfigFieldSchema {
  key: string;
  label: string;
  type: string;
  default?: unknown;
  required?: boolean;
  choices?: unknown[] | null;
  minimum?: number | null;
  maximum?: number | null;
  multiline?: boolean;
  nullable?: boolean;
  help?: string;
}

export interface PluginToolRecord {
  raw_name: string;
  name: string;
  description?: string;
  enabled: boolean;
}

export interface PluginUiManifestSpec {
  dir: string;
  entry: string;
  title?: string;
  description?: string;
  placement?: "sidebar" | "plugins_only";
}

export interface LocalPluginItem {
  id: string;
  kind: "local";
  enabled: boolean;
  trusted: boolean;
  bundled?: boolean;
  saved_enabled?: boolean;
  saved_trusted?: boolean;
  status: string;
  error?: string | null;
  manifest?: {
    id: string;
    name: string;
    version: string;
    description?: string;
    author?: string;
    permissions?: string[];
    requires_env?: string[];
    ui?: PluginUiManifestSpec | null;
    config_schema?: Record<string, ConfigFieldSchema>;
  } | null;
  permission_details?: PermissionDetail[];
  config?: Record<string, unknown>;
  saved_config?: Record<string, unknown>;
  disabled_tools?: string[];
  saved_disabled_tools?: string[];
  tools?: PluginToolRecord[];
  commands?: string[];
  hooks?: string[];
  skills?: string[];
  channels?: string[];
  routes?: string[];
  ui?: PluginUiManifestSpec | null;
  background_tasks?: number;
  missing_env?: string[];
  env_vars?: string[];
  env_status?: Record<string, boolean>;
  has_prompt?: boolean;
  pending_restart?: boolean;
}

export interface McpServerItem {
  id: string;
  kind: "mcp";
  enabled: boolean;
  trusted: boolean;
  status: string;
  error?: string | null;
  transport?: "stdio" | "streamable_http" | string | null;
  tools?: PluginToolRecord[];
  disabled_tools?: string[];
  missing_env?: string[];
  env_vars?: string[];
  env_status?: Record<string, boolean>;
  mcp_config?: Record<string, unknown> | null;
  saved_config?: Record<string, unknown> | null;
  pending_restart?: boolean;
}

export interface PluginsSnapshot {
  revision: string;
  plugins: LocalPluginItem[];
  mcp_servers: McpServerItem[];
  permission_catalog: Record<string, { level: string; summary: string }>;
  restart_required: boolean;
  restart: { available?: boolean; state?: string };
}

function statusBadge(status: string): { label: string; variant: BadgeVariant } {
  switch (status) {
    case "active":
      return { label: "运行中", variant: "ok" };
    case "pending":
      return { label: "待审核启用", variant: "warn" };
    case "disabled":
      return { label: "已停用", variant: "default" };
    case "degraded":
      return { label: "异常降级", variant: "danger" };
    default:
      return { label: status || "未知", variant: "default" };
  }
}

function riskVariant(level: string): BadgeVariant {
  if (level === "high") return "danger";
  if (level === "medium") return "warn";
  return "default";
}

function parseKeyValueLines(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of raw.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const idx = trimmed.indexOf("=");
    if (idx <= 0) {
      throw new Error(`格式应为 KEY=VALUE：${trimmed}`);
    }
    const k = trimmed.slice(0, idx).trim();
    const v = trimmed.slice(idx + 1).trim();
    out[k] = v;
  }
  return out;
}

function formatKeyValueLines(obj: unknown): string {
  if (!obj || typeof obj !== "object") return "";
  return Object.entries(obj as Record<string, unknown>)
    .map(([k, v]) => `${k}=${String(v ?? "")}`)
    .join("\n");
}

export function PluginsPanel() {
  const openPanel = useLayoutStore((s) => s.openPanel);
  const [snapshot, setSnapshot] = useState<PluginsSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [tab, setTab] = useState("local");

  // 本地插件未保存的表单草稿（按 plugin_id）
  const [localDrafts, setLocalDrafts] = useState<Record<string, Record<string, unknown>>>({});
  const [disabledDrafts, setDisabledDrafts] = useState<Record<string, string[]>>({});
  const [expandedPlugins, setExpandedPlugins] = useState<Record<string, boolean>>({});

  const togglePluginExpanded = (pluginId: string) => {
    setExpandedPlugins((prev) => ({ ...prev, [pluginId]: !prev[pluginId] }));
  };

  // .env 密钥录入弹窗
  const [envModal, setEnvModal] = useState<{ name: string } | null>(null);
  const [envValue, setEnvValue] = useState("");

  // 添加/编辑 MCP 服务弹窗
  const [mcpModalOpen, setMcpModalOpen] = useState(false);
  const [mcpForm, setMcpForm] = useState({
    id: "",
    isEdit: false,
    enabled: true,
    transport: "stdio" as "stdio" | "streamable_http",
    command: "",
    argsText: "",
    cwd: "",
    envText: "",
    envSecretsText: "",
    url: "",
    authBearerEnv: "",
    headersText: "",
    headerSecretsText: "",
    timeoutSeconds: "30",
    disabledToolsText: "",
  });

  const loadSnapshot = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch<PluginsSnapshot>("/api/plugins");
      setSnapshot(data);
      const nextCfg: Record<string, Record<string, unknown>> = {};
      const nextDisabled: Record<string, string[]> = {};
      for (const item of data.plugins || []) {
        nextCfg[item.id] = { ...(item.saved_config ?? item.config ?? {}) };
        nextDisabled[item.id] = [...(item.saved_disabled_tools ?? item.disabled_tools ?? [])];
      }
      setLocalDrafts(nextCfg);
      setDisabledDrafts(nextDisabled);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载插件列表失败", "error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSnapshot();
  }, [loadSnapshot]);

  const applyLocalDecision = async (
    plugin: LocalPluginItem,
    enabled: boolean,
    opts?: { silent?: boolean },
  ) => {
    if (!snapshot) return;
    setBusyKey(`local:${plugin.id}`);
    try {
      const updated = await apiFetch<PluginsSnapshot>(`/api/plugins/local/${plugin.id}/decision`, {
        method: "POST",
        body: JSON.stringify({
          revision: snapshot.revision,
          enabled,
          trusted: enabled ? true : plugin.saved_trusted ?? plugin.trusted,
          config: localDrafts[plugin.id] ?? {},
          disabled_tools: disabledDrafts[plugin.id] ?? [],
        }),
      });
      setSnapshot(updated);
      if (!opts?.silent) {
        toast(enabled ? `已保存并批准启用 ${plugin.id}（需重启生效）` : `已保存 ${plugin.id}（需重启生效）`, "ok");
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存插件配置失败", "error");
    } finally {
      setBusyKey(null);
    }
  };

  const handleDeleteLocal = async (plugin: LocalPluginItem) => {
    if (!snapshot) return;
    setBusyKey(`del-local:${plugin.id}`);
    try {
      const updated = await apiFetch<PluginsSnapshot>(
        `/api/plugins/local/${plugin.id}?revision=${encodeURIComponent(snapshot.revision)}&delete_files=true`,
        { method: "DELETE" },
      );
      setSnapshot(updated);
      toast(`已移除插件 ${plugin.id}`, "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "移除插件失败", "error");
    } finally {
      setBusyKey(null);
    }
  };

  const handleSaveEnv = async () => {
    if (!envModal || !envValue.trim()) {
      toast("请输入有效的环境变量值", "error");
      return;
    }
    setBusyKey(`env:${envModal.name}`);
    try {
      await apiFetch("/api/plugins/env", {
        method: "POST",
        body: JSON.stringify({ name: envModal.name, value: envValue }),
      });
      setEnvValue("");
      setEnvModal(null);
      await loadSnapshot();
      toast(`已将 ${envModal.name} 安全写入 .env`, "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "写入 .env 失败", "error");
    } finally {
      setBusyKey(null);
    }
  };

  const openNewMcpModal = () => {
    setMcpForm({
      id: "",
      isEdit: false,
      enabled: true,
      transport: "stdio",
      command: "",
      argsText: "",
      cwd: "",
      envText: "",
      envSecretsText: "",
      url: "",
      authBearerEnv: "",
      headersText: "",
      headerSecretsText: "",
      timeoutSeconds: "30",
      disabledToolsText: "",
    });
    setMcpModalOpen(true);
  };

  const openEditMcpModal = (srv: McpServerItem) => {
    const raw = (srv.saved_config ?? srv.mcp_config ?? {}) as Record<string, unknown>;
    const argsList = Array.isArray(raw.args) ? (raw.args as string[]) : [];
    const disabledList = Array.isArray(raw.disabled_tools) ? (raw.disabled_tools as string[]) : [];
    setMcpForm({
      id: srv.id,
      isEdit: true,
      enabled: Boolean(raw.enabled ?? srv.enabled),
      transport: (raw.transport === "streamable_http" ? "streamable_http" : "stdio"),
      command: String(raw.command ?? ""),
      argsText: argsList.join("\n"),
      cwd: String(raw.cwd ?? ""),
      envText: formatKeyValueLines(raw.env),
      envSecretsText: formatKeyValueLines(raw.env_secrets),
      url: String(raw.url ?? ""),
      authBearerEnv: String(raw.auth_bearer_env ?? ""),
      headersText: formatKeyValueLines(raw.headers),
      headerSecretsText: formatKeyValueLines(raw.header_secrets),
      timeoutSeconds: String(raw.timeout_seconds ?? "30"),
      disabledToolsText: disabledList.join(", "),
    });
    setMcpModalOpen(true);
  };

  const handleSaveMcp = async () => {
    if (!snapshot) return;
    const serverId = mcpForm.id.trim();
    if (!serverId) {
      toast("请填写 MCP 服务 ID", "error");
      return;
    }
    setBusyKey(`mcp-save:${serverId}`);
    try {
      const timeout = Number(mcpForm.timeoutSeconds) || 30;
      const disabledTools = mcpForm.disabledToolsText
        .split(/[,\n]/)
        .map((s) => s.trim())
        .filter(Boolean);
      const configPayload: Record<string, unknown> = {
        enabled: mcpForm.enabled,
        trusted: true,
        transport: mcpForm.transport,
        timeout_seconds: timeout,
        disabled_tools: disabledTools,
      };
      if (mcpForm.transport === "stdio") {
        configPayload.command = mcpForm.command.trim();
        configPayload.args = mcpForm.argsText
          .split(/\r?\n/)
          .map((s) => s.trim())
          .filter(Boolean);
        if (mcpForm.cwd.trim()) configPayload.cwd = mcpForm.cwd.trim();
        configPayload.env = parseKeyValueLines(mcpForm.envText);
        configPayload.env_secrets = parseKeyValueLines(mcpForm.envSecretsText);
      } else {
        configPayload.url = mcpForm.url.trim();
        if (mcpForm.authBearerEnv.trim()) configPayload.auth_bearer_env = mcpForm.authBearerEnv.trim();
        configPayload.headers = parseKeyValueLines(mcpForm.headersText);
        configPayload.header_secrets = parseKeyValueLines(mcpForm.headerSecretsText);
      }
      const updated = await apiFetch<PluginsSnapshot>(`/api/plugins/mcp/${encodeURIComponent(serverId)}`, {
        method: "POST",
        body: JSON.stringify({ revision: snapshot.revision, config: configPayload }),
      });
      setSnapshot(updated);
      setMcpModalOpen(false);
      toast(`MCP 服务 ${serverId} 已保存，可点击「立即重连」使其立即生效`, "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存 MCP 配置失败", "error");
    } finally {
      setBusyKey(null);
    }
  };

  const handleToggleMcp = async (srv: McpServerItem, enabled: boolean) => {
    if (!snapshot) return;
    const baseCfg = { ...(srv.saved_config ?? srv.mcp_config ?? {}), enabled, trusted: true };
    setBusyKey(`mcp-toggle:${srv.id}`);
    try {
      const updated = await apiFetch<PluginsSnapshot>(`/api/plugins/mcp/${encodeURIComponent(srv.id)}`, {
        method: "POST",
        body: JSON.stringify({ revision: snapshot.revision, config: baseCfg }),
      });
      setSnapshot(updated);
      const reconnected = await apiFetch<PluginsSnapshot>(`/api/plugins/mcp/${encodeURIComponent(srv.id)}/reconnect`, {
        method: "POST",
      });
      setSnapshot(reconnected);
      toast(enabled ? `MCP 服务 ${srv.id} 已连接` : `MCP 服务 ${srv.id} 已停用`, "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "切换 MCP 服务状态失败", "error");
    } finally {
      setBusyKey(null);
    }
  };

  const handleReconnectMcp = async (serverId: string) => {
    setBusyKey(`mcp-reconn:${serverId}`);
    try {
      const updated = await apiFetch<PluginsSnapshot>(`/api/plugins/mcp/${encodeURIComponent(serverId)}/reconnect`, {
        method: "POST",
      });
      setSnapshot(updated);
      toast(`MCP 服务 ${serverId} 重连完成`, "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "MCP 重连失败", "error");
    } finally {
      setBusyKey(null);
    }
  };

  const handleDeleteMcp = async (serverId: string) => {
    if (!snapshot) return;
    setBusyKey(`mcp-del:${serverId}`);
    try {
      const updated = await apiFetch<PluginsSnapshot>(
        `/api/plugins/mcp/${encodeURIComponent(serverId)}?revision=${encodeURIComponent(snapshot.revision)}`,
        { method: "DELETE" },
      );
      setSnapshot(updated);
      toast(`已删除 MCP 服务 ${serverId}`, "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "删除 MCP 服务失败", "error");
    } finally {
      setBusyKey(null);
    }
  };

  const handleRestartApp = async () => {
    if (!snapshot) return;
    setBusyKey("restart");
    try {
      await apiFetch("/api/plugins/restart", {
        method: "POST",
        body: JSON.stringify({ revision: snapshot.revision }),
      });
      toast("重启指令已触发，页面将在服务恢复后自动重新连接", "ok");
      await loadSnapshot();
    } catch (err) {
      toast(err instanceof Error ? err.message : "触发重启失败", "error");
    } finally {
      setBusyKey(null);
    }
  };

  const plugins = snapshot?.plugins ?? [];
  const mcpServers = snapshot?.mcp_servers ?? [];

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg text-fg">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border bg-surface px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <Plug className="h-4 w-4 text-accent" />
          <h2 className="text-sm font-semibold">插件与 MCP 扩展</h2>
          <Badge variant="info">本地 {plugins.length}</Badge>
          <Badge variant="info">MCP {mcpServers.length}</Badge>
          {plugins.some((p) => p.status === "pending") && (
            <Badge variant="warn">
              {plugins.filter((p) => p.status === "pending").length} 个待审核
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          {snapshot?.restart_required && snapshot.restart?.available !== false && (
            <Button
              size="sm"
              variant="primary"
              disabled={busyKey === "restart"}
              onClick={() => void handleRestartApp()}
            >
              <RotateCw className="h-3.5 w-3.5" />
              重启应用使变更生效
            </Button>
          )}
          <Button size="sm" variant="secondary" disabled={loading} onClick={() => void loadSnapshot()}>
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
            刷新
          </Button>
        </div>
      </div>

      {snapshot?.restart_required && (
        <div className="flex items-center justify-between gap-2 border-b border-warn/30 bg-warn-soft px-4 py-2 text-xs text-warn">
          <span>检测到本地插件开关、配置或环境变量有未生效改动；本地插件与环境变更需重启主进程生效（MCP 支持立即重连）。</span>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <Tabs value={tab} onValueChange={setTab} className="mx-auto max-w-5xl space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <TabsList>
              <TabsTrigger value="local">本地插件 ({plugins.length})</TabsTrigger>
              <TabsTrigger value="mcp">MCP 服务 ({mcpServers.length})</TabsTrigger>
            </TabsList>
            {tab === "mcp" && (
              <Button size="sm" variant="primary" onClick={openNewMcpModal}>
                <Plus className="h-3.5 w-3.5" />
                添加 MCP 服务
              </Button>
            )}
          </div>

          <TabsContent value="local" className="space-y-4">
            {plugins.length === 0 ? (
              <div className="rounded-2xl border border-border bg-surface p-6 text-center text-xs text-fg-dim">
                <p className="font-medium text-fg">当前尚未安装本地插件</p>
                <p className="mt-1">
                  可通过主 Agent 工具暂存插件，或将包含 manifest.yaml 的插件目录放入 TENTACLE_HOME/plugins/。
                </p>
              </div>
            ) : (
              plugins.map((plugin) => {
                const badge = statusBadge(plugin.status);
                const schema = plugin.manifest?.config_schema ?? {};
                const draft = localDrafts[plugin.id] ?? {};
                const disabledList = disabledDrafts[plugin.id] ?? [];
                const effectiveEnabled = plugin.saved_enabled ?? plugin.enabled;
                const expanded = Boolean(expandedPlugins[plugin.id]);
                const schemaCount = Object.keys(schema).length;
                const permCount = plugin.permission_details?.length ?? 0;
                const toolCount = plugin.tools?.length ?? 0;
                const envCount = plugin.env_vars?.length ?? 0;
                const missingCount = plugin.missing_env?.length ?? 0;
                const hasDetails =
                  schemaCount > 0 ||
                  permCount > 0 ||
                  toolCount > 0 ||
                  envCount > 0 ||
                  (plugin.commands?.length ?? 0) > 0 ||
                  (plugin.skills?.length ?? 0) > 0 ||
                  (plugin.channels?.length ?? 0) > 0 ||
                  (plugin.routes?.length ?? 0) > 0;
                const summaryParts: string[] = [];
                if (permCount > 0) summaryParts.push(`权限 ${permCount}`);
                if (toolCount > 0) summaryParts.push(`工具 ${toolCount}`);
                if (schemaCount > 0) summaryParts.push(`配置项 ${schemaCount}`);
                if (envCount > 0) summaryParts.push(`环境变量 ${envCount}`);
                if ((plugin.skills?.length ?? 0) > 0) summaryParts.push(`Skills ${plugin.skills!.length}`);
                if (plugin.ui || plugin.manifest?.ui) summaryParts.push("自定义页面");
                const canOpenUi = plugin.status === "active" && Boolean(plugin.ui);
                return (
                  <div
                    key={plugin.id}
                    className="space-y-3 rounded-2xl border border-border bg-surface p-4 shadow-xs"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0 flex-1 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold text-fg">
                            {plugin.manifest?.name || plugin.id}
                          </span>
                          <span className="font-mono text-xs text-fg-faint">
                            {plugin.id}@{plugin.manifest?.version || "?"}
                          </span>
                          {plugin.bundled && <Badge variant="accent">内置插件</Badge>}
                          <Badge variant={badge.variant}>{badge.label}</Badge>
                          {missingCount > 0 && (
                            <Badge variant="danger">缺失凭据 ({missingCount})</Badge>
                          )}
                          {plugin.pending_restart && <Badge variant="warn">待重启生效</Badge>}
                        </div>
                        {plugin.manifest?.description && (
                          <p className="text-xs text-fg-dim">{plugin.manifest.description}</p>
                        )}
                        {!expanded && summaryParts.length > 0 && (
                          <p className="text-[11px] text-fg-faint">{summaryParts.join(" · ")}</p>
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        {canOpenUi && (
                          <Button
                            size="sm"
                            variant="secondary"
                            aria-label={`打开 ${plugin.id} 页面`}
                            onClick={() => openPanel(pluginPanelId(plugin.id))}
                          >
                            <AppWindow className="h-3.5 w-3.5 text-accent" />
                            打开页面
                          </Button>
                        )}
                        {plugin.id === "browser" &&
                          plugin.status === "active" &&
                          typeof plugin.config?.live_view_url === "string" &&
                          /^https?:\/\//i.test(plugin.config.live_view_url.trim()) && (
                            <Button
                              size="sm"
                              variant="secondary"
                              aria-label="打开可视化浏览器"
                              onClick={() => openPanel("browser")}
                            >
                              <Globe className="h-3.5 w-3.5 text-accent" />
                              打开浏览器
                            </Button>
                          )}
                        {plugin.status === "pending" && (
                          <Button
                            size="sm"
                            variant="primary"
                            disabled={busyKey === `local:${plugin.id}`}
                            onClick={() => void applyLocalDecision(plugin, true)}
                          >
                            批准并启用
                          </Button>
                        )}
                        {hasDetails && (
                          <Button
                            size="sm"
                            variant="secondary"
                            aria-expanded={expanded}
                            aria-label={`${expanded ? "收起" : "展开"} ${plugin.id} 配置`}
                            onClick={() => togglePluginExpanded(plugin.id)}
                          >
                            {expanded ? (
                              <ChevronDown className="h-3.5 w-3.5" />
                            ) : (
                              <ChevronRight className="h-3.5 w-3.5" />
                            )}
                            {expanded ? "收起" : "展开配置"}
                          </Button>
                        )}
                        <Switch
                          aria-label={`启用 ${plugin.id}`}
                          checked={effectiveEnabled}
                          disabled={busyKey === `local:${plugin.id}`}
                          onCheckedChange={(checked) => void applyLocalDecision(plugin, checked)}
                        />
                        {!plugin.bundled && (
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={busyKey === `del-local:${plugin.id}`}
                            onClick={() => void handleDeleteLocal(plugin)}
                          >
                            <Trash2 className="h-3.5 w-3.5 text-danger" />
                          </Button>
                        )}
                      </div>
                    </div>

                    {plugin.error && (
                      <div className="flex items-start gap-2 rounded-xl border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        <span className="break-all">{plugin.error}</span>
                      </div>
                    )}

                    {expanded && (
                      <div className="space-y-3 border-t border-border-soft pt-3">
                        {plugin.id === "browser" && plugin.status === "active" && (
                          <BrowserNekoControl
                            hasUnsavedChanges={
                              JSON.stringify(localDrafts[plugin.id] ?? {}) !==
                              JSON.stringify(plugin.saved_config ?? plugin.config ?? {})
                            }
                          />
                        )}
                        {permCount > 0 && (
                          <div className="space-y-1.5 rounded-xl bg-elevated/45 p-3">
                            <div className="flex items-center gap-1.5 text-xs font-medium text-fg">
                              <ShieldAlert className="h-3.5 w-3.5 text-warn" />
                              <span>申请权限 ({permCount})</span>
                            </div>
                            <div className="flex flex-wrap gap-1.5">
                              {plugin.permission_details!.map((perm) => (
                                <Badge
                                  key={perm.permission}
                                  variant={riskVariant(perm.level)}
                                  title={perm.summary}
                                >
                                  {perm.permission} · {perm.summary}
                                </Badge>
                              ))}
                            </div>
                          </div>
                        )}

                        {envCount > 0 && (
                          <div className="space-y-1.5 rounded-xl bg-elevated/45 p-3">
                            <div className="flex items-center gap-1.5 text-xs font-medium text-fg">
                              <KeyRound className="h-3.5 w-3.5 text-accent" />
                              <span>环境变量 / 凭据（只写入 .env，不进对话与日志）</span>
                            </div>
                            <div className="flex flex-wrap gap-2">
                              {plugin.env_vars!.map((varName) => {
                                const ok = plugin.env_status?.[varName] ?? !plugin.missing_env?.includes(varName);
                                return (
                                  <div
                                    key={varName}
                                    className="flex items-center gap-2 rounded-lg border border-border-soft bg-surface px-2.5 py-1 text-xs"
                                  >
                                    <span className="font-mono">{varName}</span>
                                    <Badge variant={ok ? "ok" : "danger"}>{ok ? "已配置" : "缺失"}</Badge>
                                    <Button
                                      size="sm"
                                      variant="secondary"
                                      onClick={() => {
                                        setEnvValue("");
                                        setEnvModal({ name: varName });
                                      }}
                                    >
                                      {ok ? "更新" : "录入"}
                                    </Button>
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        )}

                        {schemaCount > 0 && (
                          <div className="space-y-2 rounded-xl border border-border-soft bg-elevated/30 p-3">
                            <div className="text-xs font-medium text-fg">插件配置项</div>
                            <div className="grid gap-3 md:grid-cols-2">
                              {Object.values(schema).map((field) => {
                                const val = draft[field.key] ?? field.default ?? "";
                                return (
                                  <div key={field.key} className="space-y-1 text-xs">
                                    <label className="block font-medium text-fg">
                                      {field.label}
                                      <span className="ml-1 font-mono text-[10px] text-fg-faint">
                                        ({field.key})
                                      </span>
                                    </label>
                                    {field.type === "boolean" ? (
                                      <Switch
                                        aria-label={field.label}
                                        checked={Boolean(val)}
                                        onCheckedChange={(checked) =>
                                          setLocalDrafts((prev) => ({
                                            ...prev,
                                            [plugin.id]: { ...prev[plugin.id], [field.key]: checked },
                                          }))
                                        }
                                      />
                                    ) : field.choices && field.choices.length > 0 ? (
                                      <select
                                        className="h-9 w-full rounded-lg border border-border bg-input px-2.5 text-xs text-fg"
                                        value={String(val ?? "")}
                                        onChange={(e) =>
                                          setLocalDrafts((prev) => ({
                                            ...prev,
                                            [plugin.id]: { ...prev[plugin.id], [field.key]: e.target.value },
                                          }))
                                        }
                                      >
                                        {field.choices.map((c) => (
                                          <option key={String(c)} value={String(c)}>
                                            {String(c)}
                                          </option>
                                        ))}
                                      </select>
                                    ) : field.type === "string_list" ? (
                                      <Textarea
                                        rows={2}
                                        placeholder="每行一项"
                                        value={Array.isArray(val) ? (val as string[]).join("\n") : ""}
                                        onChange={(e) =>
                                          setLocalDrafts((prev) => ({
                                            ...prev,
                                            [plugin.id]: {
                                              ...prev[plugin.id],
                                              [field.key]: e.target.value
                                                .split(/\r?\n/)
                                                .map((line) => line.trim())
                                                .filter(Boolean),
                                            },
                                          }))
                                        }
                                      />
                                    ) : (
                                      <Input
                                        type={field.type === "integer" || field.type === "number" ? "number" : "text"}
                                        value={val === null || val === undefined ? "" : String(val)}
                                        onChange={(e) => {
                                          const rawVal = e.target.value;
                                          const parsed =
                                            field.type === "integer"
                                              ? rawVal === "" ? null : parseInt(rawVal, 10)
                                              : field.type === "number"
                                                ? rawVal === "" ? null : parseFloat(rawVal)
                                                : rawVal;
                                          setLocalDrafts((prev) => ({
                                            ...prev,
                                            [plugin.id]: { ...prev[plugin.id], [field.key]: parsed },
                                          }));
                                        }}
                                      />
                                    )}
                                    {field.help && <p className="text-[11px] text-fg-dim">{field.help}</p>}
                                  </div>
                                );
                              })}
                            </div>
                          </div>
                        )}

                        {toolCount > 0 && (
                          <div className="space-y-1.5 rounded-xl bg-elevated/35 p-3 text-xs">
                            <div className="font-medium text-fg">注册工具 ({toolCount})</div>
                            <div className="flex flex-wrap gap-2">
                              {plugin.tools!.map((tool) => {
                                const disabled =
                                  disabledList.includes(tool.raw_name) || disabledList.includes(tool.name);
                                return (
                                  <label
                                    key={tool.name}
                                    className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-border-soft bg-surface px-2 py-1"
                                    title={tool.description}
                                  >
                                    <input
                                      type="checkbox"
                                      checked={!disabled}
                                      onChange={(e) => {
                                        const next = e.target.checked
                                          ? disabledList.filter((x) => x !== tool.raw_name && x !== tool.name)
                                          : [...disabledList, tool.raw_name];
                                        setDisabledDrafts((prev) => ({ ...prev, [plugin.id]: next }));
                                      }}
                                    />
                                    <span className="font-mono text-[11px]">{tool.name}</span>
                                  </label>
                                );
                              })}
                            </div>
                          </div>
                        )}

                        <div className="flex flex-wrap items-center justify-between gap-2 pt-1 text-[11px] text-fg-dim">
                          <div className="flex flex-wrap gap-2">
                            {(plugin.commands?.length ?? 0) > 0 && (
                              <span>命令: {plugin.commands!.map((c) => `/${c}`).join(", ")}</span>
                            )}
                            {(plugin.skills?.length ?? 0) > 0 && <span>Skills: {plugin.skills!.join(", ")}</span>}
                            {(plugin.channels?.length ?? 0) > 0 && <span>通道: {plugin.channels!.join(", ")}</span>}
                            {(plugin.routes?.length ?? 0) > 0 && <span>路由: {plugin.routes!.join(", ")}</span>}
                          </div>
                          <div className="flex items-center gap-2">
                            <Button
                              size="sm"
                              variant="secondary"
                              disabled={busyKey === `local:${plugin.id}`}
                              onClick={() => void applyLocalDecision(plugin, effectiveEnabled)}
                            >
                              保存配置
                            </Button>
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </TabsContent>

          <TabsContent value="mcp" className="space-y-4">
            {mcpServers.length === 0 ? (
              <div className="rounded-2xl border border-border bg-surface p-6 text-center text-xs text-fg-dim">
                <p className="font-medium text-fg">当前尚未配置外部 MCP 服务</p>
                <p className="mt-1">
                  点击右上角「添加 MCP 服务」，可通过 stdio（uvx / npx / Python）或 Streamable HTTP 挂载工具集。
                </p>
              </div>
            ) : (
              mcpServers.map((srv) => {
                const badge = statusBadge(srv.status);
                return (
                  <div
                    key={srv.id}
                    className="space-y-3 rounded-2xl border border-border bg-surface p-4 shadow-xs"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono text-sm font-semibold">{srv.id}</span>
                          <Badge variant="default">{srv.transport || "stdio"}</Badge>
                          <Badge variant={badge.variant}>{badge.label}</Badge>
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        <Button
                          size="sm"
                          variant="secondary"
                          disabled={busyKey === `mcp-reconn:${srv.id}`}
                          onClick={() => void handleReconnectMcp(srv.id)}
                        >
                          <RefreshCw className="h-3.5 w-3.5" />
                          立即重连
                        </Button>
                        <Button size="sm" variant="secondary" onClick={() => openEditMcpModal(srv)}>
                          编辑
                        </Button>
                        <Switch
                          aria-label={`启用 MCP ${srv.id}`}
                          checked={srv.enabled}
                          disabled={busyKey === `mcp-toggle:${srv.id}`}
                          onCheckedChange={(checked) => void handleToggleMcp(srv, checked)}
                        />
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busyKey === `mcp-del:${srv.id}`}
                          onClick={() => void handleDeleteMcp(srv.id)}
                        >
                          <Trash2 className="h-3.5 w-3.5 text-danger" />
                        </Button>
                      </div>
                    </div>

                    {srv.error && (
                      <div className="flex items-start gap-2 rounded-xl border border-danger/30 bg-danger-soft px-3 py-2 text-xs text-danger">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        <span className="break-all">{srv.error}</span>
                      </div>
                    )}

                    {(srv.env_vars?.length ?? 0) > 0 && (
                      <div className="flex flex-wrap gap-2 rounded-xl bg-elevated/45 p-3">
                        {srv.env_vars!.map((varName) => {
                          const ok = srv.env_status?.[varName] ?? !srv.missing_env?.includes(varName);
                          return (
                            <div
                              key={varName}
                              className="flex items-center gap-2 rounded-lg border border-border-soft bg-surface px-2.5 py-1 text-xs"
                            >
                              <span className="font-mono">{varName}</span>
                              <Badge variant={ok ? "ok" : "danger"}>{ok ? "已配置" : "缺失"}</Badge>
                              <Button
                                size="sm"
                                variant="secondary"
                                onClick={() => {
                                  setEnvValue("");
                                  setEnvModal({ name: varName });
                                }}
                              >
                                {ok ? "更新" : "录入"}
                              </Button>
                            </div>
                          );
                        })}
                      </div>
                    )}

                    {(srv.tools?.length ?? 0) > 0 && (
                      <div className="flex flex-wrap gap-1.5 text-[11px]">
                        {srv.tools!.map((t) => (
                          <Badge key={t.name} variant={t.enabled ? "info" : "default"} title={t.description}>
                            {t.name}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </TabsContent>
        </Tabs>
      </div>

      <Dialog
        open={envModal !== null}
        onOpenChange={(open) => {
          if (!open) {
            setEnvModal(null);
            setEnvValue("");
          }
        }}
        title={envModal ? `安全录入环境变量：${envModal.name}` : "录入环境变量"}
        description="值仅写入本地 .env 文件并同步当前环境变量，绝不写入 config.yaml 或回显到界面。"
      >
        <div className="space-y-3">
          <Input
            type="password"
            placeholder="请输入密钥值（提交后不回显）"
            value={envValue}
            onChange={(e) => setEnvValue(e.target.value)}
          />
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={() => setEnvModal(null)}>
              取消
            </Button>
            <Button
              variant="primary"
              disabled={!envModal || busyKey === `env:${envModal.name}`}
              onClick={() => void handleSaveEnv()}
            >
              保存到 .env
            </Button>
          </div>
        </div>
      </Dialog>

      <Dialog
        open={mcpModalOpen}
        onOpenChange={setMcpModalOpen}
        title={mcpForm.isEdit ? `编辑 MCP 服务：${mcpForm.id}` : "添加 MCP 服务"}
        description="支持 stdio 子进程（uvx / npx / Python）与 Streamable HTTP。敏感凭据请引用 .env 变量名。"
        className="max-h-[85vh] overflow-y-auto"
      >
        <div className="space-y-3 text-xs">
          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block font-medium">服务 ID</label>
              <Input
                value={mcpForm.id}
                disabled={mcpForm.isEdit}
                placeholder="如 github / filesystem"
                onChange={(e) => setMcpForm((s) => ({ ...s, id: e.target.value }))}
              />
            </div>
            <div>
              <label className="mb-1 block font-medium">传输类型</label>
              <select
                className="h-9 w-full rounded-lg border border-border bg-input px-2.5 text-xs text-fg"
                value={mcpForm.transport}
                onChange={(e) =>
                  setMcpForm((s) => ({
                    ...s,
                    transport: e.target.value === "streamable_http" ? "streamable_http" : "stdio",
                  }))
                }
              >
                <option value="stdio">stdio（独立子进程）</option>
                <option value="streamable_http">streamable_http（远程 HTTP）</option>
              </select>
            </div>
          </div>

          {mcpForm.transport === "stdio" ? (
            <>
              <div>
                <label className="mb-1 block font-medium">启动命令 (command)</label>
                <Input
                  value={mcpForm.command}
                  placeholder="如 npx / uvx / python"
                  onChange={(e) => setMcpForm((s) => ({ ...s, command: e.target.value }))}
                />
              </div>
              <div>
                <label className="mb-1 block font-medium">参数（每行一个）</label>
                <Textarea
                  rows={3}
                  value={mcpForm.argsText}
                  placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/path"}
                  onChange={(e) => setMcpForm((s) => ({ ...s, argsText: e.target.value }))}
                />
              </div>
              <div>
                <label className="mb-1 block font-medium">密钥环境变量映射（TARGET_VAR=HOST_ENV_VAR，每行一个）</label>
                <Textarea
                  rows={2}
                  value={mcpForm.envSecretsText}
                  placeholder="GITHUB_PERSONAL_ACCESS_TOKEN=GITHUB_TOKEN"
                  onChange={(e) => setMcpForm((s) => ({ ...s, envSecretsText: e.target.value }))}
                />
              </div>
              <div>
                <label className="mb-1 block font-medium">普通环境变量（KEY=VALUE，每行一个，可选）</label>
                <Textarea
                  rows={2}
                  value={mcpForm.envText}
                  onChange={(e) => setMcpForm((s) => ({ ...s, envText: e.target.value }))}
                />
              </div>
            </>
          ) : (
            <>
              <div>
                <label className="mb-1 block font-medium">服务 URL</label>
                <Input
                  value={mcpForm.url}
                  placeholder="https://example.com/mcp"
                  onChange={(e) => setMcpForm((s) => ({ ...s, url: e.target.value }))}
                />
              </div>
              <div>
                <label className="mb-1 block font-medium">Bearer Token 环境变量名（可选）</label>
                <Input
                  value={mcpForm.authBearerEnv}
                  placeholder="如 MCP_API_TOKEN"
                  onChange={(e) => setMcpForm((s) => ({ ...s, authBearerEnv: e.target.value }))}
                />
              </div>
              <div>
                <label className="mb-1 block font-medium">密钥 HTTP 头映射（Header=HOST_ENV_VAR，每行一个）</label>
                <Textarea
                  rows={2}
                  value={mcpForm.headerSecretsText}
                  onChange={(e) => setMcpForm((s) => ({ ...s, headerSecretsText: e.target.value }))}
                />
              </div>
            </>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block font-medium">超时秒数</label>
              <Input
                type="number"
                value={mcpForm.timeoutSeconds}
                onChange={(e) => setMcpForm((s) => ({ ...s, timeoutSeconds: e.target.value }))}
              />
            </div>
            <div className="flex items-end gap-2 pb-1">
              <Switch
                aria-label="保存后立即标记为启用"
                checked={mcpForm.enabled}
                onCheckedChange={(checked) => setMcpForm((s) => ({ ...s, enabled: checked }))}
              />
              <span>启用该 MCP 服务</span>
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button variant="secondary" onClick={() => setMcpModalOpen(false)}>
              取消
            </Button>
            <Button variant="primary" onClick={() => void handleSaveMcp()}>
              保存 MCP 配置
            </Button>
          </div>
        </div>
      </Dialog>
    </div>
  );
}
