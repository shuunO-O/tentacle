import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronRight,
  Plus,
  RefreshCw,
  RotateCcw,
  RotateCw,
  Save,
  Search,
  SlidersHorizontal,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { apiFetch } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Input } from "../../components/ui/Input";
import { toast } from "../../components/ui/Toast";
import { useIsCompactLayout } from "../workbench/panels";
import { ConfigFieldControl } from "./ConfigFieldControl";
import {
  fieldMatchesSubgroup,
  fieldSection,
  GENERAL_SECTIONS,
  groupFieldsForDisplay,
  listSectionSubgroups,
  sameValue,
} from "./configGrouping";
import type {
  ConfigDiffItem,
  ConfigFieldGroup,
  ConfigSectionId,
  ConfigSnapshot,
  DiscoveredModel,
} from "./types";

function automaticEntityId(
  kind: "providers" | "models" | "roles",
  usedIds: Set<string>,
  opts: { type?: string; provider?: string; name?: string },
): string {
  let base =
    kind === "providers"
      ? opts.type === "openai_compatible"
        ? "provider"
        : opts.type || "provider"
      : kind === "models"
        ? `${opts.provider || "provider"}/${
            (opts.name || "")
              .replace(/[^A-Za-z_0-9-]+/g, "-")
              .replace(/^-+|-+$/g, "") || "model"
          }`.slice(0, 128)
        : "role";
  if (kind !== "models") {
    base = base.replace(/[^A-Za-z_0-9-]+/g, "-").replace(/^-+|-+$/g, "") || "item";
  }
  if (!/^[A-Za-z_]/.test(base)) base = `id-${base}`;
  let candidate = base;
  let counter = 2;
  while (usedIds.has(candidate)) {
    candidate = `${base}-${counter++}`;
  }
  return candidate;
}

export function ConfigurationPanel() {
  const compact = useIsCompactLayout();
  const [snapshot, setSnapshot] = useState<ConfigSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  const [section, setSection] = useState<ConfigSectionId>("models");
  const [selectedPrefix, setSelectedPrefix] = useState<string>("");
  const [selectedSubgroup, setSelectedSubgroup] = useState<string>("");
  const [mobileView, setMobileView] = useState<"tree" | "editor">("tree");
  const [mobileToolsOpen, setMobileToolsOpen] = useState(false);
  const [entitySearch, setEntitySearch] = useState("");
  const [fieldSearch, setFieldSearch] = useState("");
  const [showDirtyOnly, setShowDirtyOnly] = useState(false);
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(() => new Set());
  const [groupExpanded, setGroupExpanded] = useState<Record<string, boolean>>({});

  const [draft, setDraft] = useState<Record<string, unknown>>({});

  // Diff & special action confirmation dialog
  const [diffState, setDiffState] = useState<{
    revision: string;
    changes: ConfigDiffItem[];
    endpoint?: "/structure" | "/restore";
    payload?: Record<string, unknown>;
    summaryText?: string;
  } | null>(null);

  // Environment write-only modal state
  const [secretTarget, setSecretTarget] = useState<{ path: string; envName: string } | null>(null);
  const [secretValue, setSecretValue] = useState("");
  const [savingSecret, setSavingSecret] = useState(false);

  // Create Entity Dialog
  const [createKind, setCreateKind] = useState<"providers" | "models" | "roles" | null>(null);
  const [createId, setCreateId] = useState("");
  const [createLabel, setCreateLabel] = useState("");
  const [createProviderType, setCreateProviderType] = useState("");
  const [createBaseUrl, setCreateBaseUrl] = useState("");
  const [createApiKeyEnv, setCreateApiKeyEnv] = useState("");
  const [createProject, setCreateProject] = useState("");
  const [createLocation, setCreateLocation] = useState("global");
  const [createCredsEnv, setCreateCredsEnv] = useState("");
  const [createModelProvider, setCreateModelProvider] = useState("");
  const [createModelName, setCreateModelName] = useState("");
  const [createApiStyle, setCreateApiStyle] = useState("chat");
  const [createContextWindow, setCreateContextWindow] = useState(131072);
  const [createVision, setCreateVision] = useState(false);
  const [createRoleModel, setCreateRoleModel] = useState("");

  // Discover remote models catalog
  const [catalog, setCatalog] = useState<{
    provider: string;
    revision: string;
    api_style: string;
    notice?: string;
    truncated?: boolean;
    models: DiscoveredModel[];
  } | null>(null);
  const [catalogSelected, setCatalogSelected] = useState<Set<string>>(() => new Set());
  const [catalogSearch, setCatalogSearch] = useState("");

  // Backup selection
  const [selectedBackup, setSelectedBackup] = useState("");

  const syncModelNavigation = useCallback((data: ConfigSnapshot) => {
    const providerKeys = (data.entities?.providers || []).map((p) => `providers:${p.id}`);
    setExpandedKeys((prev) => (prev.size > 0 ? prev : new Set(providerKeys)));
    setSelectedPrefix((prev) => {
      if (prev && (data.fields || []).some((f) => f.path.startsWith(prev))) {
        return prev;
      }
      return `roles.${data.active_role}.`;
    });
  }, []);

  const loadSnapshot = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiFetch<ConfigSnapshot>("/api/configuration");
      setSnapshot(data);
      if (data.history?.length && !selectedBackup) {
        setSelectedBackup(data.history[0]!.id);
      }
      syncModelNavigation(data);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载配置失败", "error");
    } finally {
      setLoading(false);
    }
  }, [selectedBackup, syncModelNavigation]);

  useEffect(() => {
    void loadSnapshot();
  }, [loadSnapshot]);

  const draftCount = Object.keys(draft).length;

  useEffect(() => {
    if (draftCount === 0 && showDirtyOnly) {
      setShowDirtyOnly(false);
    }
  }, [draftCount, showDirtyOnly]);

  const setFieldDraft = (path: string, initialValue: unknown, value: unknown) => {
    setDraft((prev) => {
      const next = { ...prev };
      if (sameValue(initialValue, value)) {
        delete next[path];
      } else {
        next[path] = value;
      }
      return next;
    });
  };

  const sectionCounts = useMemo(() => {
    const counts: Record<ConfigSectionId, { total: number; dirty: number }> = {
      models: { total: 0, dirty: 0 },
      memory: { total: 0, dirty: 0 },
      runtime: { total: 0, dirty: 0 },
      channels: { total: 0, dirty: 0 },
    };
    for (const f of snapshot?.fields || []) {
      const sec = fieldSection(f.path);
      counts[sec].total += 1;
      if (Object.prototype.hasOwnProperty.call(draft, f.path)) {
        counts[sec].dirty += 1;
      }
    }
    return counts;
  }, [snapshot, draft]);

  const sectionFields = useMemo(
    () => (snapshot?.fields || []).filter((f) => fieldSection(f.path) === section),
    [snapshot, section],
  );

  const nonModelSubgroups = useMemo(
    () => (section !== "models" ? listSectionSubgroups(section, sectionFields, draft) : []),
    [section, sectionFields, draft],
  );

  const focusedModelPrefix = useMemo(() => {
    if (section !== "models") return undefined;
    return selectedPrefix || undefined;
  }, [section, selectedPrefix]);

  const visibleFields = useMemo(() => {
    let base = sectionFields;
    if (focusedModelPrefix) {
      base = base.filter((f) => f.path.startsWith(focusedModelPrefix));
    } else if (section !== "models" && selectedSubgroup) {
      base = base.filter((f) => fieldMatchesSubgroup(section, f.path, selectedSubgroup));
    }

    if (showDirtyOnly) {
      base = base.filter((f) => Object.prototype.hasOwnProperty.call(draft, f.path));
    }

    const q = fieldSearch.trim().toLowerCase();
    if (q) {
      base = base.filter(
        (f) =>
          f.label.toLowerCase().includes(q) ||
          f.path.toLowerCase().includes(q) ||
          (f.help || "").toLowerCase().includes(q) ||
          (f.group || "").toLowerCase().includes(q),
      );
    }
    return base;
  }, [sectionFields, focusedModelPrefix, section, selectedSubgroup, showDirtyOnly, draft, fieldSearch]);

  const forceExpandGroups = Boolean(fieldSearch.trim() || showDirtyOnly);

  const groupedFields = useMemo(
    () =>
      groupFieldsForDisplay({
        section,
        fields: visibleFields,
        draft,
        forceExpand: forceExpandGroups,
        focusedModelPrefix,
      }),
    [section, visibleFields, draft, forceExpandGroups, focusedModelPrefix],
  );

  const selectedEntityMeta = useMemo(() => {
    if (!snapshot || !selectedPrefix) return null;
    const parts = selectedPrefix.split(".");
    const kind = parts[0] as "providers" | "models" | "roles";
    const id = parts.slice(1, -1).join(".");
    const list = snapshot.entities?.[kind] || [];
    const item = list.find((e) => e.id === id);
    if (!item) return null;
    const refs = item.references as Record<string, unknown[]> | unknown[] | undefined;
    const locked =
      Array.isArray(refs)
        ? refs.length > 0
        : refs && typeof refs === "object"
          ? Object.values(refs).some((arr) => Array.isArray(arr) && arr.length > 0)
          : false;
    return { kind, id, item, locked };
  }, [snapshot, selectedPrefix]);

  const entityChain = useMemo(() => {
    if (!snapshot || !selectedEntityMeta) return null;
    const providers = snapshot.entities?.providers || [];
    const models = snapshot.entities?.models || [];
    const roles = snapshot.entities?.roles || [];

    if (selectedEntityMeta.kind === "providers") {
      const childModels = models.filter((m) => m.provider === selectedEntityMeta.id);
      const childModelIds = new Set(childModels.map((m) => m.id));
      const childRoles = roles.filter((r) => r.model && childModelIds.has(r.model));
      return {
        provider: selectedEntityMeta.item,
        models: childModels,
        roles: childRoles,
      };
    }

    if (selectedEntityMeta.kind === "models") {
      const parentProvider = providers.find((p) => p.id === selectedEntityMeta.item.provider) || null;
      const childRoles = roles.filter((r) => r.model === selectedEntityMeta.id);
      return {
        provider: parentProvider,
        models: [selectedEntityMeta.item],
        roles: childRoles,
      };
    }

    const boundModel = models.find((m) => m.id === selectedEntityMeta.item.model) || null;
    const parentProvider = boundModel
      ? providers.find((p) => p.id === boundModel.provider) || null
      : null;
    return {
      provider: parentProvider,
      models: boundModel ? [boundModel] : [],
      roles: [selectedEntityMeta.item],
    };
  }, [snapshot, selectedEntityMeta]);

  const prefixDirtyCount = useCallback(
    (prefix: string) =>
      Object.keys(draft).filter((path) => path.startsWith(prefix)).length,
    [draft],
  );

  const handleSelectSection = (nextSection: ConfigSectionId) => {
    setSection(nextSection);
    setSelectedSubgroup("");
    setFieldSearch("");
    setMobileView("tree");
    setMobileToolsOpen(false);
  };

  const selectModelEntity = (prefix: string) => {
    setSelectedPrefix(prefix);
    if (compact) {
      setMobileView("editor");
    }
  };

  const handlePreviewSave = async () => {
    if (!snapshot || draftCount === 0) return;
    setBusy(true);
    try {
      const res = await apiFetch<{ revision?: string; changes?: ConfigDiffItem[] }>(
        "/api/configuration/preview",
        {
          method: "POST",
          body: JSON.stringify({ revision: snapshot.revision, changes: draft }),
        },
      );
      setDiffState({
        revision: res.revision || snapshot.revision,
        changes: res.changes || [],
      });
    } catch (err) {
      toast(err instanceof Error ? err.message : "预览配置修改失败", "error");
    } finally {
      setBusy(false);
    }
  };

  const handleConfirmSave = async () => {
    if (!diffState || !snapshot) return;
    setBusy(true);
    try {
      let res: { status?: ConfigSnapshot };
      if (diffState.endpoint) {
        res = await apiFetch<{ status?: ConfigSnapshot }>(
          `/api/configuration${diffState.endpoint}`,
          {
            method: "POST",
            body: JSON.stringify({
              ...(diffState.payload || {}),
              revision: diffState.revision,
              apply: true,
            }),
          },
        );
      } else {
        res = await apiFetch<{ status?: ConfigSnapshot }>("/api/configuration", {
          method: "PUT",
          body: JSON.stringify({ revision: diffState.revision, changes: draft }),
        });
        setDraft({});
      }
      const deletedTarget =
        diffState.endpoint === "/structure" &&
        Array.isArray(diffState.payload?.operations) &&
        (diffState.payload?.operations as Array<{ op?: string }>).some((o) => o.op === "delete");
      setDiffState(null);
      if (deletedTarget) {
        setMobileView("tree");
      }
      if (res.status) {
        setSnapshot(res.status);
        syncModelNavigation(res.status);
      } else {
        await loadSnapshot();
      }
      toast("配置已保存并自动创建备份", "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存配置失败", "error");
    } finally {
      setBusy(false);
    }
  };

  const handleSaveSecret = async (e: FormEvent) => {
    e.preventDefault();
    if (!snapshot || !secretTarget) return;
    setSavingSecret(true);
    try {
      const res = await apiFetch<{ status?: ConfigSnapshot }>("/api/configuration/environment", {
        method: "POST",
        body: JSON.stringify({
          path: secretTarget.path,
          value: secretValue,
          revision: snapshot.revision,
        }),
      });
      setSecretValue("");
      setSecretTarget(null);
      if (res.status) setSnapshot(res.status);
      else await loadSnapshot();
      toast(`环境变量 ${secretTarget.envName} 已写入应用目录 .env`, "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "写入 .env 凭据失败", "error");
    } finally {
      setSavingSecret(false);
    }
  };

  const handleApplyDynamic = async () => {
    setBusy(true);
    try {
      const res = await apiFetch<ConfigSnapshot>("/api/configuration/apply", {
        method: "POST",
      });
      setSnapshot(res);
      toast("已请求应用动态配置", "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "应用配置失败", "error");
    } finally {
      setBusy(false);
    }
  };

  const handleRestartOrReembed = async (reembed: boolean) => {
    if (!snapshot) return;
    if (draftCount > 0) {
      toast("请先保存或放弃未保存修改后再重启", "error");
      return;
    }
    const msg = reembed
      ? "重启期间将使用当前向量嵌入模型重算全部记忆向量（将消耗嵌入模型用量），确定继续？"
      : "重启将等待当前对话回合结束后短暂停机重载配置，确定重启？";
    if (!window.confirm(msg)) return;
    setBusy(true);
    try {
      await apiFetch("/api/configuration/restart", {
        method: "POST",
        body: JSON.stringify({ revision: snapshot.revision, reembed }),
      });
      toast(reembed ? "已请求重启并重算向量" : "已请求应用重启", "ok");
      await loadSnapshot();
    } catch (err) {
      toast(err instanceof Error ? err.message : "请求重启失败", "error");
    } finally {
      setBusy(false);
    }
  };

  const openCreateModal = (kind: "providers" | "models" | "roles", parentId?: string) => {
    if (!snapshot || draftCount > 0) {
      toast("请先处理未保存修改后再新增条目", "error");
      return;
    }
    const used = new Set((snapshot.entities?.[kind] || []).map((e) => e.id));
    setCreateKind(kind);
    setCreateLabel("");

    if (kind === "providers") {
      const firstType = Object.keys(snapshot.provider_types || {})[0] || "openai_compatible";
      const spec = snapshot.provider_types?.[firstType];
      setCreateProviderType(firstType);
      setCreateBaseUrl(spec?.base_url || "");
      setCreateApiKeyEnv("OPENAI_API_KEY");
      setCreateProject("");
      setCreateLocation("global");
      setCreateCredsEnv("");
      setCreateId(automaticEntityId("providers", used, { type: firstType }));
    } else if (kind === "models") {
      const prov = parentId || snapshot.entities?.providers?.[0]?.id || "";
      const provObj = snapshot.entities?.providers?.find((p) => p.id === prov);
      const spec = provObj?.type ? snapshot.provider_types?.[provObj.type] : undefined;
      setCreateModelProvider(prov);
      setCreateModelName("");
      setCreateApiStyle(spec?.api_style || "chat");
      setCreateContextWindow(131072);
      setCreateVision(false);
      setCreateId("");
    } else {
      const model = parentId || snapshot.entities?.models?.[0]?.id || "";
      setCreateRoleModel(model);
      setCreateId(automaticEntityId("roles", used, {}));
    }
  };

  const handleSubmitCreate = async (e: FormEvent) => {
    e.preventDefault();
    if (!snapshot || !createKind) return;
    let value: Record<string, unknown> = {};
    if (createKind === "providers") {
      const spec = snapshot.provider_types?.[createProviderType];
      value = {
        type: createProviderType,
        auth: spec?.auth || "api_key",
        base_url: createBaseUrl,
        ...(spec?.auth === "adc"
          ? {
              project: createProject,
              location: createLocation,
              credentials_env: createCredsEnv,
            }
          : { api_key_env: createApiKeyEnv }),
      };
    } else if (createKind === "models") {
      value = {
        provider: createModelProvider,
        name: createModelName,
        api_style: createApiStyle,
        context_window: createContextWindow,
        vision: createVision,
      };
    } else {
      value = { model: createRoleModel, tools: { enabled: [] } };
    }
    if (createLabel.trim()) value.label = createLabel.trim();

    const operations = [{ op: "create", kind: createKind, id: createId.trim(), value }];
    setBusy(true);
    try {
      const res = await apiFetch<{ revision?: string; changes?: ConfigDiffItem[] }>(
        "/api/configuration/structure",
        {
          method: "POST",
          body: JSON.stringify({ revision: snapshot.revision, operations }),
        },
      );
      setCreateKind(null);
      setDiffState({
        revision: res.revision || snapshot.revision,
        changes: res.changes || [],
        endpoint: "/structure",
        payload: { operations },
      });
    } catch (err) {
      toast(err instanceof Error ? err.message : "预览新增配置失败", "error");
    } finally {
      setBusy(false);
    }
  };

  const handleDeleteSelected = async () => {
    if (!snapshot || !selectedEntityMeta || draftCount > 0) return;
    const operations = [
      { op: "delete", kind: selectedEntityMeta.kind, id: selectedEntityMeta.id },
    ];
    setBusy(true);
    try {
      const res = await apiFetch<{ revision?: string; changes?: ConfigDiffItem[] }>(
        "/api/configuration/structure",
        {
          method: "POST",
          body: JSON.stringify({ revision: snapshot.revision, operations }),
        },
      );
      setDiffState({
        revision: res.revision || snapshot.revision,
        changes: res.changes || [],
        endpoint: "/structure",
        payload: { operations },
      });
    } catch (err) {
      toast(err instanceof Error ? err.message : "无法删除该条目", "error");
    } finally {
      setBusy(false);
    }
  };

  const handleDiscoverModels = async (providerId: string) => {
    if (!snapshot) return;
    setBusy(true);
    try {
      const res = await apiFetch<{
        api_style: string;
        notice?: string;
        truncated?: boolean;
        models: DiscoveredModel[];
      }>("/api/configuration/discover-models", {
        method: "POST",
        body: JSON.stringify({ revision: snapshot.revision, provider: providerId }),
      });
      setCatalog({
        provider: providerId,
        revision: snapshot.revision,
        api_style: res.api_style,
        notice: res.notice,
        truncated: res.truncated,
        models: res.models || [],
      });
      setCatalogSelected(new Set());
      setCatalogSearch("");
    } catch (err) {
      toast(err instanceof Error ? err.message : "拉取远端模型目录失败", "error");
    } finally {
      setBusy(false);
    }
  };

  const handlePreviewCatalogImport = async () => {
    if (!catalog || catalogSelected.size === 0 || !snapshot) return;
    const operations = catalog.models
      .filter((m) => catalogSelected.has(m.import_id))
      .map((m) => ({
        op: "create",
        kind: "models",
        id: m.import_id,
        value: {
          provider: catalog.provider,
          name: m.name,
          api_style: catalog.api_style,
          ...(m.context_window ? { context_window: m.context_window } : {}),
        },
      }));
    setBusy(true);
    try {
      const res = await apiFetch<{ revision?: string; changes?: ConfigDiffItem[] }>(
        "/api/configuration/structure",
        {
          method: "POST",
          body: JSON.stringify({ revision: catalog.revision, operations }),
        },
      );
      setCatalog(null);
      setDiffState({
        revision: res.revision || snapshot.revision,
        changes: res.changes || [],
        endpoint: "/structure",
        payload: { operations },
      });
    } catch (err) {
      toast(err instanceof Error ? err.message : "生成批量导入预览失败", "error");
    } finally {
      setBusy(false);
    }
  };

  const handleRestorePreview = async () => {
    if (!snapshot || !selectedBackup || draftCount > 0) return;
    setBusy(true);
    try {
      const res = await apiFetch<{ revision?: string; changes?: ConfigDiffItem[] }>(
        "/api/configuration/restore",
        {
          method: "POST",
          body: JSON.stringify({ revision: snapshot.revision, backup: selectedBackup }),
        },
      );
      if (!res.changes?.length) {
        toast("所选配置备份与当前配置一致，无需恢复", "info");
        return;
      }
      setDiffState({
        revision: res.revision || snapshot.revision,
        changes: res.changes,
        endpoint: "/restore",
        payload: { backup: selectedBackup },
      });
    } catch (err) {
      toast(err instanceof Error ? err.message : "对比配置备份失败", "error");
    } finally {
      setBusy(false);
    }
  };

  const toggleExpand = (key: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const isGroupExpanded = (group: ConfigFieldGroup): boolean => {
    if (!group.collapsible || forceExpandGroups) return true;
    const override = groupExpanded[group.id];
    if (override !== undefined) return override;
    return !group.defaultCollapsed;
  };

  const toggleGroupCollapsed = (group: ConfigFieldGroup) => {
    const currentlyOpen = isGroupExpanded(group);
    setGroupExpanded((prev) => ({ ...prev, [group.id]: !currentlyOpen }));
  };

  const isModelsSection = section === "models";

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-3 py-2 md:px-4 md:py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <SlidersHorizontal className="h-4 w-4 text-accent" />
          <h2 className="text-sm font-semibold text-fg">系统配置中心</h2>
          {snapshot?.active_model?.name && (
            <Badge variant="accent">主模型：{snapshot.active_model.name}</Badge>
          )}
          {snapshot?.applying && <Badge variant="warn">动态应用排队中</Badge>}
          {snapshot?.restart_required && <Badge variant="warn">含待重启生效项</Badge>}
          {snapshot?.error && <Badge variant="danger">{snapshot.error}</Badge>}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {draftCount > 0 && (
            <>
              <Badge variant="warn" data-testid="config-dirty-badge">
                {draftCount} 项未保存
              </Badge>
              <Button size="sm" variant="ghost" onClick={() => setDraft({})}>
                放弃修改
              </Button>
              <Button
                size="sm"
                variant="primary"
                disabled={busy}
                data-testid="config-preview-btn"
                onClick={() => void handlePreviewSave()}
              >
                <Save className="h-3.5 w-3.5" />
                预览保存
              </Button>
            </>
          )}
          {snapshot?.error && (
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={() => void handleApplyDynamic()}
            >
              重试应用动态配置
            </Button>
          )}
          <Button
            size="sm"
            variant="secondary"
            disabled={loading || busy}
            onClick={() => void loadSnapshot()}
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            刷新
          </Button>
        </div>
      </div>

      {/* Category Tabs + Maintenance Actions */}
      <div className="border-b border-border-soft bg-surface/60 px-3 py-2 text-xs md:px-4">
        <div className="flex items-center justify-between gap-2">
          <div className="no-scrollbar flex min-w-0 flex-1 flex-nowrap items-center gap-1 overflow-x-auto whitespace-nowrap md:flex-wrap md:gap-1.5 md:overflow-visible">
            {GENERAL_SECTIONS.map((s) => {
              const meta = sectionCounts[s.id];
              const active = section === s.id;
              return (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => handleSelectSection(s.id)}
                  className={cn(
                    "inline-flex shrink-0 items-center gap-1.5 rounded-lg px-2.5 py-1 font-medium transition-colors cursor-pointer md:px-3",
                    active
                      ? "bg-accent text-accent-fg"
                      : "text-fg-dim hover:bg-hover hover:text-fg",
                  )}
                >
                  <span>{s.label}</span>
                  {meta.total > 0 && (
                    <span
                      className={cn(
                        "rounded-full px-1.5 py-0 text-[10px]",
                        active ? "bg-accent-fg/15 text-accent-fg" : "bg-elevated text-fg-faint",
                      )}
                    >
                      {meta.total}
                    </span>
                  )}
                  {meta.dirty > 0 && (
                    <span
                      className={cn(
                        "h-1.5 w-1.5 rounded-full",
                        active ? "bg-accent-fg" : "bg-warn",
                      )}
                      title={`${meta.dirty} 项未保存`}
                    />
                  )}
                </button>
              );
            })}
          </div>

          {compact ? (
            <Button
              size="sm"
              variant={mobileToolsOpen ? "primary" : "ghost"}
              className="shrink-0"
              onClick={() => setMobileToolsOpen((o) => !o)}
              aria-expanded={mobileToolsOpen}
            >
              <Wrench className="h-3.5 w-3.5" />
              <span>维护</span>
            </Button>
          ) : (
            <div className="flex shrink-0 flex-wrap items-center gap-1.5">
              {snapshot?.history && snapshot.history.length > 0 && (
                <div className="flex items-center gap-1">
                  <select
                    value={selectedBackup}
                    onChange={(e) => setSelectedBackup(e.target.value)}
                    className="h-7 max-w-44 rounded-lg border border-border bg-input px-2 font-mono text-[11px] text-fg"
                    aria-label="选择配置备份"
                  >
                    {snapshot.history.map((h) => (
                      <option key={h.id} value={h.id}>
                        {new Date(h.created_at * 1000).toLocaleString()}
                      </option>
                    ))}
                  </select>
                  <Button size="sm" variant="secondary" onClick={() => void handleRestorePreview()}>
                    <RotateCcw className="h-3.5 w-3.5" />
                    预览回滚
                  </Button>
                </div>
              )}
              <Button
                size="sm"
                variant="secondary"
                disabled={draftCount > 0 || busy || !snapshot?.restart?.available}
                onClick={() => void handleRestartOrReembed(false)}
              >
                <RotateCw className="h-3.5 w-3.5" />
                重启
              </Button>
              <Button
                size="sm"
                variant="secondary"
                disabled={draftCount > 0 || busy || !snapshot?.restart?.available}
                onClick={() => void handleRestartOrReembed(true)}
              >
                重算向量并重启
              </Button>
            </div>
          )}
        </div>

        {compact && mobileToolsOpen && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 border-t border-border-soft pt-2">
            {snapshot?.history && snapshot.history.length > 0 && (
              <div className="flex w-full items-center gap-1.5">
                <select
                  value={selectedBackup}
                  onChange={(e) => setSelectedBackup(e.target.value)}
                  className="h-7 min-w-0 flex-1 rounded-lg border border-border bg-input px-2 font-mono text-[11px] text-fg"
                  aria-label="选择配置备份"
                >
                  {snapshot.history.map((h) => (
                    <option key={h.id} value={h.id}>
                      {new Date(h.created_at * 1000).toLocaleString()}
                    </option>
                  ))}
                </select>
                <Button size="sm" variant="secondary" onClick={() => void handleRestorePreview()}>
                  <RotateCcw className="h-3.5 w-3.5" />
                  预览回滚
                </Button>
              </div>
            )}
            <Button
              size="sm"
              variant="secondary"
              disabled={draftCount > 0 || busy || !snapshot?.restart?.available}
              onClick={() => void handleRestartOrReembed(false)}
            >
              <RotateCw className="h-3.5 w-3.5" />
              重启
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={draftCount > 0 || busy || !snapshot?.restart?.available}
              onClick={() => void handleRestartOrReembed(true)}
            >
              重算向量并重启
            </Button>
          </div>
        )}
      </div>

      {/* Master-Detail Body */}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        {/* Models Hierarchy Tree */}
        {isModelsSection && snapshot && (!compact || mobileView === "tree") && (
          <aside
            className={cn(
              "flex flex-col bg-surface/60",
              compact
                ? "min-h-0 flex-1"
                : "w-72 shrink-0 border-r border-border",
            )}
          >
            <div className="space-y-2 border-b border-border-soft p-2.5">
              <div className="flex items-center justify-between gap-2">
                <div className="relative flex-1">
                  <Search className="pointer-events-none absolute left-2 top-2 h-3.5 w-3.5 text-fg-faint" />
                  <Input
                    value={entitySearch}
                    onChange={(e) => setEntitySearch(e.target.value)}
                    placeholder="筛选供应商 / 模型 / 角色…"
                    className="h-7 pl-7 text-xs"
                  />
                </div>
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => openCreateModal("providers")}
                  title="新增供应商连接"
                >
                  <Plus className="h-3.5 w-3.5" />
                </Button>
              </div>
              <div className="flex items-center justify-between px-0.5 text-[10px] text-fg-faint">
                <span>供应商 {snapshot.entities?.providers?.length || 0}</span>
                <span>·</span>
                <span>模型 {snapshot.entities?.models?.length || 0}</span>
                <span>·</span>
                <span>角色 {snapshot.entities?.roles?.length || 0}</span>
              </div>
            </div>

            <div className="min-h-0 flex-1 space-y-1 overflow-y-auto p-2 text-xs">
              <div className="px-2 pb-0.5 text-[10px] font-semibold uppercase tracking-wide text-fg-faint">
                供应商 → 模型 → 角色
              </div>
              {(snapshot.entities?.providers || [])
                .filter(
                  (p) =>
                    !entitySearch.trim() ||
                    p.id.toLowerCase().includes(entitySearch.toLowerCase()) ||
                    (p.label || "").toLowerCase().includes(entitySearch.toLowerCase()),
                )
                .map((prov) => {
                  const pKey = `providers:${prov.id}`;
                  const isOpen = expandedKeys.has(pKey);
                  const childModels = (snapshot.entities?.models || []).filter(
                    (m) => m.provider === prov.id,
                  );
                  const connOk = snapshot.connection_status?.[prov.id]?.configured;
                  const provDirty = prefixDirtyCount(`providers.${prov.id}.`);

                  return (
                    <div key={prov.id} className="space-y-0.5">
                      <div
                        className={cn(
                          "flex items-center justify-between gap-1 rounded-lg px-2 py-1.5",
                          selectedPrefix === `providers.${prov.id}.`
                            ? "bg-accent-soft font-medium text-accent"
                            : "text-fg hover:bg-hover",
                        )}
                      >
                        <button
                          type="button"
                          onClick={() => toggleExpand(pKey)}
                          className="rounded p-0.5 text-fg-dim hover:text-fg"
                          aria-label="展开供应商"
                        >
                          {isOpen ? (
                            <ChevronDown className="h-3.5 w-3.5" />
                          ) : (
                            <ChevronRight className="h-3.5 w-3.5" />
                          )}
                        </button>
                        <button
                          type="button"
                          data-config-provider={prov.id}
                          onClick={() => selectModelEntity(`providers.${prov.id}.`)}
                          className="min-w-0 flex-1 cursor-pointer truncate text-left"
                        >
                          {prov.label || prov.id}
                        </button>
                        {provDirty > 0 && (
                          <span
                            className="h-1.5 w-1.5 shrink-0 rounded-full bg-warn"
                            title={`${provDirty} 项未保存`}
                          />
                        )}
                        <Badge variant={connOk ? "ok" : "default"} className="text-[10px]">
                          {prov.type}
                        </Badge>
                        {compact && <ChevronRight className="h-3.5 w-3.5 shrink-0 text-fg-faint" />}
                      </div>

                      {isOpen && (
                        <div className="ml-4 space-y-0.5 border-l border-border-soft pl-2">
                          <div className="flex items-center gap-1 py-0.5">
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-6 px-1.5 text-[11px]"
                              onClick={() => void handleDiscoverModels(prov.id)}
                            >
                              拉取目录
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              className="h-6 px-1.5 text-[11px]"
                              onClick={() => openCreateModal("models", prov.id)}
                            >
                              + 模型
                            </Button>
                          </div>
                          {childModels.map((m) => {
                            const childRoles = (snapshot.entities?.roles || []).filter(
                              (r) => r.model === m.id,
                            );
                            const modelDirty = prefixDirtyCount(`models.${m.id}.`);
                            return (
                              <div key={m.id} className="space-y-0.5">
                                <button
                                  type="button"
                                  data-config-model={m.id}
                                  onClick={() => selectModelEntity(`models.${m.id}.`)}
                                  className={cn(
                                    "flex w-full cursor-pointer items-center justify-between gap-1.5 rounded-lg px-2 py-1 text-left",
                                    selectedPrefix === `models.${m.id}.`
                                      ? "bg-accent-soft font-medium text-accent"
                                      : "text-fg-dim hover:bg-hover hover:text-fg",
                                  )}
                                >
                                  <span className="truncate">{m.label || m.name || m.id}</span>
                                  <span className="flex shrink-0 items-center gap-1">
                                    {modelDirty > 0 && (
                                      <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-warn" />
                                    )}
                                    {compact && <ChevronRight className="h-3 w-3 text-fg-faint" />}
                                  </span>
                                </button>
                                {childRoles.map((r) => (
                                  <button
                                    key={r.id}
                                    type="button"
                                    onClick={() => selectModelEntity(`roles.${r.id}.`)}
                                    className={cn(
                                      "ml-3 flex w-[calc(100%-0.75rem)] cursor-pointer items-center justify-between gap-1 rounded-lg px-2 py-1 text-left text-[11px]",
                                      selectedPrefix === `roles.${r.id}.`
                                        ? "bg-accent-soft font-medium text-accent"
                                        : "text-fg-faint hover:bg-hover hover:text-fg",
                                    )}
                                  >
                                    <span className="truncate">角色：{r.label || r.id}</span>
                                    {compact && <ChevronRight className="h-3 w-3 shrink-0 text-fg-faint" />}
                                  </button>
                                ))}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}

              {/* Direct Roles quick list */}
              <div className="mt-3 border-t border-border-soft pt-2">
                <div className="mb-1 flex items-center justify-between px-2 text-[11px] font-medium text-fg-faint">
                  <span>全部角色 ({snapshot.entities?.roles?.length || 0})</span>
                  <button
                    type="button"
                    onClick={() => openCreateModal("roles")}
                    className="cursor-pointer text-accent hover:underline"
                  >
                    + 新增角色
                  </button>
                </div>
                {(snapshot.entities?.roles || [])
                  .filter(
                    (r) =>
                      !entitySearch.trim() ||
                      r.id.toLowerCase().includes(entitySearch.toLowerCase()) ||
                      (r.label || "").toLowerCase().includes(entitySearch.toLowerCase()),
                  )
                  .map((r) => {
                    const roleDirty = prefixDirtyCount(`roles.${r.id}.`);
                    return (
                      <button
                        key={r.id}
                        type="button"
                        data-config-role={r.id}
                        onClick={() => selectModelEntity(`roles.${r.id}.`)}
                        className={cn(
                          "flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg px-2 py-1.5 text-left",
                          selectedPrefix === `roles.${r.id}.`
                            ? "bg-accent-soft font-medium text-accent"
                            : "text-fg-dim hover:bg-hover hover:text-fg",
                        )}
                      >
                        <span className="truncate">{r.label || r.id}</span>
                        <span className="flex shrink-0 items-center gap-1.5">
                          {roleDirty > 0 && (
                            <span className="h-1.5 w-1.5 rounded-full bg-warn" />
                          )}
                          {r.id === snapshot.active_role && (
                            <Badge variant="accent" className="text-[10px]">
                              主对话
                            </Badge>
                          )}
                          {compact && <ChevronRight className="h-3.5 w-3.5 text-fg-faint" />}
                        </span>
                      </button>
                    );
                  })}
              </div>
            </div>
          </aside>
        )}

        {/* Non-Model Sub-group Index (Desktop left sidebar) */}
        {!compact && section !== "models" && nonModelSubgroups.length > 0 && (
          <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-surface/60">
            <div className="flex items-center justify-between border-b border-border-soft px-3 py-2 text-[11px] font-medium text-fg-faint">
              <span>子分组目录</span>
              <span>{sectionFields.length} 项</span>
            </div>
            <div className="flex flex-col gap-1 overflow-y-auto p-2 text-xs">
              <button
                type="button"
                onClick={() => setSelectedSubgroup("")}
                className={cn(
                  "flex cursor-pointer items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors",
                  selectedSubgroup === ""
                    ? "bg-accent-soft font-medium text-accent"
                    : "text-fg-dim hover:bg-hover hover:text-fg",
                )}
              >
                <span>全部分组</span>
                <span className="text-[10px] text-fg-faint">{sectionFields.length}</span>
              </button>
              {nonModelSubgroups.map((sg) => {
                const active = selectedSubgroup === sg.id;
                return (
                  <button
                    key={sg.id}
                    type="button"
                    data-config-subgroup={sg.id}
                    onClick={() => setSelectedSubgroup(sg.id)}
                    className={cn(
                      "flex cursor-pointer items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left transition-colors",
                      active
                        ? "bg-accent-soft font-medium text-accent"
                        : "text-fg-dim hover:bg-hover hover:text-fg",
                    )}
                  >
                    <span className="truncate">{sg.title}</span>
                    <span className="flex shrink-0 items-center gap-1.5">
                      {sg.dirtyCount > 0 && (
                        <span
                          className="h-1.5 w-1.5 rounded-full bg-warn"
                          title={`${sg.dirtyCount} 项未保存`}
                        />
                      )}
                      <span className="text-[10px] text-fg-faint">{sg.count}</span>
                    </span>
                  </button>
                );
              })}
            </div>
          </aside>
        )}

        {/* Right Fields Editor (Full-screen on mobile when editing a model entity or browsing general tabs) */}
        {(!isModelsSection || !compact || mobileView === "editor") && (
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            {compact && isModelsSection && (
              <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border-soft bg-surface/85 px-3 py-2">
                <button
                  type="button"
                  data-testid="config-mobile-back"
                  onClick={() => setMobileView("tree")}
                  className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium text-fg transition-colors hover:bg-hover"
                >
                  <ArrowLeft className="h-3.5 w-3.5" />
                  <span>返回供应商与模型层级</span>
                </button>
                {selectedEntityMeta && (
                  <Badge variant="accent">
                    {selectedEntityMeta.item.label || selectedEntityMeta.id}
                  </Badge>
                )}
              </div>
            )}
            <section className="min-w-0 flex-1 overflow-y-auto p-3 md:p-4">
              <div className="mx-auto max-w-4xl space-y-4">
                {compact && section !== "models" && nonModelSubgroups.length > 0 && (
                  <div className="no-scrollbar flex items-center gap-1.5 overflow-x-auto pb-0.5 text-xs whitespace-nowrap">
                    <button
                      type="button"
                      onClick={() => setSelectedSubgroup("")}
                      className={cn(
                        "inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-full border px-3 py-1 transition-colors",
                        selectedSubgroup === ""
                          ? "border-accent/50 bg-accent-soft font-medium text-accent"
                          : "border-border bg-surface text-fg-dim hover:text-fg",
                      )}
                    >
                      <span>全部分组</span>
                      <span className="text-[10px] text-fg-faint">{sectionFields.length}</span>
                    </button>
                    {nonModelSubgroups.map((sg) => {
                      const active = selectedSubgroup === sg.id;
                      return (
                        <button
                          key={sg.id}
                          type="button"
                          data-config-subgroup={sg.id}
                          onClick={() => setSelectedSubgroup(sg.id)}
                          className={cn(
                            "inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-full border px-3 py-1 transition-colors",
                            active
                              ? "border-accent/50 bg-accent-soft font-medium text-accent"
                              : "border-border bg-surface text-fg-dim hover:text-fg",
                          )}
                        >
                          <span>{sg.title}</span>
                          {sg.dirtyCount > 0 && (
                            <span className="h-1.5 w-1.5 rounded-full bg-warn" />
                          )}
                          <span className="text-[10px] text-fg-faint">{sg.count}</span>
                        </button>
                      );
                    })}
                  </div>
                )}
            {/* Selected Entity Banner + 3-Tier Hierarchy Relationship */}
            {selectedEntityMeta && section === "models" && (
              <div className="space-y-3 rounded-2xl border border-border bg-surface p-4">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="accent">
                        {selectedEntityMeta.kind === "providers"
                          ? "① 供应商连接"
                          : selectedEntityMeta.kind === "models"
                            ? "② 模型定义"
                            : "③ 角色策略"}
                      </Badge>
                      <span className="text-sm font-semibold text-fg">
                        {selectedEntityMeta.item.label || selectedEntityMeta.id}
                      </span>
                    </div>
                    <div className="mt-0.5 font-mono text-[11px] text-fg-faint">
                      {selectedEntityMeta.kind}.{selectedEntityMeta.id}
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5">
                    {selectedEntityMeta.kind === "providers" && (
                      <>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => void handleDiscoverModels(selectedEntityMeta.id)}
                        >
                          发现模型目录
                        </Button>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => openCreateModal("models", selectedEntityMeta.id)}
                        >
                          + 模型
                        </Button>
                      </>
                    )}
                    {selectedEntityMeta.kind === "models" && (
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => openCreateModal("roles", selectedEntityMeta.id)}
                      >
                        + 绑定角色
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={selectedEntityMeta.locked || draftCount > 0 || busy}
                      title={selectedEntityMeta.locked ? "已被下级配置引用，无法删除" : "删除当前条目"}
                      onClick={() => void handleDeleteSelected()}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                      删除
                    </Button>
                  </div>
                </div>

                {entityChain && (
                  <div
                    data-testid="config-hierarchy-chain"
                    className="grid grid-cols-1 gap-2 rounded-xl border border-border-soft bg-elevated/40 p-2.5 text-[11px] sm:grid-cols-3"
                  >
                    <div className="min-w-0">
                      <div className="mb-1 text-[10px] font-medium text-fg-faint">① 供应商连接</div>
                      {entityChain.provider ? (
                        <button
                          type="button"
                          onClick={() => setSelectedPrefix(`providers.${entityChain.provider!.id}.`)}
                          className={cn(
                            "max-w-full cursor-pointer truncate rounded-lg border px-2 py-1 font-medium transition-colors",
                            selectedEntityMeta.kind === "providers"
                              ? "border-accent/50 bg-accent-soft text-accent"
                              : "border-border bg-surface text-fg hover:border-accent/40",
                          )}
                        >
                          {entityChain.provider.label || entityChain.provider.id}
                        </button>
                      ) : (
                        <span className="text-fg-faint">未关联供应商</span>
                      )}
                    </div>

                    <div className="min-w-0">
                      <div className="mb-1 text-[10px] font-medium text-fg-faint">② 模型定义</div>
                      <div className="flex flex-wrap gap-1">
                        {entityChain.models.length > 0 ? (
                          entityChain.models.map((m) => (
                            <button
                              key={m.id}
                              type="button"
                              onClick={() => setSelectedPrefix(`models.${m.id}.`)}
                              className={cn(
                                "max-w-full cursor-pointer truncate rounded-lg border px-2 py-1 font-medium transition-colors",
                                selectedEntityMeta.kind === "models" && selectedEntityMeta.id === m.id
                                  ? "border-accent/50 bg-accent-soft text-accent"
                                  : "border-border bg-surface text-fg hover:border-accent/40",
                              )}
                            >
                              {m.label || m.name || m.id}
                            </button>
                          ))
                        ) : (
                          <span className="text-warn">暂未分配模型</span>
                        )}
                      </div>
                    </div>

                    <div className="min-w-0">
                      <div className="mb-1 text-[10px] font-medium text-fg-faint">③ 绑定角色</div>
                      <div className="flex flex-wrap gap-1">
                        {entityChain.roles.length > 0 ? (
                          entityChain.roles.map((r) => (
                            <button
                              key={r.id}
                              type="button"
                              onClick={() => setSelectedPrefix(`roles.${r.id}.`)}
                              className={cn(
                                "max-w-full cursor-pointer truncate rounded-lg border px-2 py-1 font-medium transition-colors",
                                selectedEntityMeta.kind === "roles" && selectedEntityMeta.id === r.id
                                  ? "border-accent/50 bg-accent-soft text-accent"
                                  : "border-border bg-surface text-fg hover:border-accent/40",
                              )}
                            >
                              {r.label || r.id}
                            </button>
                          ))
                        ) : (
                          <span className="text-fg-faint">暂无角色使用</span>
                        )}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Quick Field Filter Toolbar */}
            <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border-soft bg-surface/60 px-3 py-2">
              <div className="relative min-w-52 flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-2 h-3.5 w-3.5 text-fg-faint" />
                <Input
                  value={fieldSearch}
                  onChange={(e) => setFieldSearch(e.target.value)}
                  placeholder="搜索当前页配置项名称、路径或说明…"
                  className="h-7 pl-8 pr-7 text-xs"
                />
                {fieldSearch && (
                  <button
                    type="button"
                    onClick={() => setFieldSearch("")}
                    aria-label="清空配置搜索"
                    className="absolute right-2 top-1.5 rounded p-0.5 text-fg-faint hover:text-fg cursor-pointer"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>

              <div className="flex items-center gap-2 text-[11px] text-fg-dim">
                {draftCount > 0 && (
                  <button
                    type="button"
                    onClick={() => setShowDirtyOnly((v) => !v)}
                    className={cn(
                      "rounded-lg border px-2.5 py-1 font-medium transition-colors cursor-pointer",
                      showDirtyOnly
                        ? "border-warn/60 bg-warn-soft/50 text-warn"
                        : "border-border bg-elevated/60 text-fg-dim hover:text-fg",
                    )}
                  >
                    仅看已改 ({draftCount})
                  </button>
                )}
                <span>共 {visibleFields.length} 项</span>
              </div>
            </div>

            {groupedFields.length === 0 ? (
              <div className="rounded-2xl border border-border bg-surface p-8 text-center text-xs text-fg-dim">
                {loading
                  ? "正在读取配置…"
                  : fieldSearch || showDirtyOnly
                    ? "没有匹配当前筛选条件的配置项。"
                    : "当前分组暂无可编辑配置项。"}
              </div>
            ) : (
              groupedFields.map((group) => {
                const open = isGroupExpanded(group);
                const groupDirtyCount = group.fields.filter((f) =>
                  Object.prototype.hasOwnProperty.call(draft, f.path),
                ).length;

                return (
                  <div
                    key={group.id}
                    data-config-group={group.id}
                    className="overflow-hidden rounded-2xl border border-border bg-surface"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-soft bg-elevated/30 px-4 py-2.5">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <h3 className="text-xs font-semibold text-fg">{group.title}</h3>
                          <Badge variant="default" className="text-[10px]">
                            {group.fields.length} 项
                          </Badge>
                          {groupDirtyCount > 0 && (
                            <Badge variant="warn" className="text-[10px]">
                              {groupDirtyCount} 项已改
                            </Badge>
                          )}
                        </div>
                        {group.subtitle && (
                          <p className="mt-0.5 text-[11px] text-fg-dim">{group.subtitle}</p>
                        )}
                      </div>

                      {group.collapsible && (
                        <Button
                          size="sm"
                          variant="ghost"
                          className="h-7 px-2.5 text-[11px]"
                          onClick={() => toggleGroupCollapsed(group)}
                        >
                          {open ? (
                            <>
                              <ChevronDown className="h-3.5 w-3.5" />
                              收起
                            </>
                          ) : (
                            <>
                              <ChevronRight className="h-3.5 w-3.5" />
                              展开 ({group.fields.length})
                            </>
                          )}
                        </Button>
                      )}
                    </div>

                    {open ? (
                      <div className="grid grid-cols-1 gap-3 p-4 md:grid-cols-2 md:gap-4">
                        {group.fields.map((field) => {
                          const isDirty = Object.prototype.hasOwnProperty.call(draft, field.path);
                          const effectiveValue = isDirty ? draft[field.path] : field.value;
                          return (
                            <ConfigFieldControl
                              key={field.path}
                              field={field}
                              effectiveValue={effectiveValue}
                              isDirty={isDirty}
                              snapshot={snapshot}
                              onChange={(val) => setFieldDraft(field.path, field.value, val)}
                              onOpenSecretModal={(target) => {
                                setSecretTarget(target);
                                setSecretValue("");
                              }}
                            />
                          );
                        })}
                      </div>
                    ) : (
                      <div className="px-4 py-2.5 text-[11px] text-fg-faint">
                        已按需折叠：包含 {group.fields.map((f) => f.label).join("、")}
                      </div>
                    )}
                  </div>
                );
              })
            )}
              </div>
            </section>
          </div>
        )}
      </div>

      {/* Preview / Diff Confirm Dialog */}
      <Dialog
        open={diffState !== null}
        onOpenChange={(o) => !o && setDiffState(null)}
        title="确认配置变更预览"
        description="核对实际将写入 config.yaml 的差异，保存时将自动生成配置备份"
        className="w-[min(94vw,640px)]"
      >
        <div className="max-h-80 space-y-2 overflow-y-auto text-xs">
          {(diffState?.changes || []).map((c, idx) => (
            <div
              key={`${c.path}-${idx}`}
              className="rounded-xl border border-border-soft bg-elevated/50 p-3"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium text-fg">{c.label}</span>
                <span className="font-mono text-[10px] text-fg-faint">{c.path}</span>
              </div>
              <div className="mt-1 grid grid-cols-2 gap-2 font-mono text-[11px]">
                <div className="truncate rounded bg-danger-soft/40 px-2 py-1 text-danger">
                  - {JSON.stringify(c.before)}
                </div>
                <div className="truncate rounded bg-ok-soft/40 px-2 py-1 text-ok">
                  + {JSON.stringify(c.after)}
                </div>
              </div>
            </div>
          ))}
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setDiffState(null)}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={busy}
            data-testid="config-save-btn"
            onClick={() => void handleConfirmSave()}
          >
            <Check className="h-3.5 w-3.5" />
            确认保存并应用
          </Button>
        </div>
      </Dialog>

      {/* Write-Only .env Secret Dialog */}
      <Dialog
        open={secretTarget !== null}
        onOpenChange={(o) => !o && setSecretTarget(null)}
        title={`设置凭据 ${secretTarget?.envName || ""}`}
        description="仅以纯写入模式保存到应用目录 .env；保存后网页端永不回显密钥值。"
      >
        <form onSubmit={(e) => void handleSaveSecret(e)} className="space-y-3">
          <div className="flex items-center gap-2 rounded-lg border border-warn/30 bg-warn-soft/30 px-3 py-2 text-[11px] text-fg-dim">
            <AlertTriangle className="h-4 w-4 shrink-0 text-warn" />
            <span>请确保上述环境变量名已在字段中先保存；本弹窗仅写入其实际密钥值。</span>
          </div>
          <Input
            type="password"
            value={secretValue}
            onChange={(e) => setSecretValue(e.target.value)}
            placeholder="粘贴 API Key / 凭据文本（提交后立即清空）"
            autoComplete="off"
          />
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setSecretTarget(null)}>
              取消
            </Button>
            <Button type="submit" variant="primary" disabled={savingSecret}>
              {savingSecret ? "写入中…" : "安全写入 .env"}
            </Button>
          </div>
        </form>
      </Dialog>

      {/* Create Provider / Model / Role Dialog */}
      <Dialog
        open={createKind !== null}
        onOpenChange={(o) => !o && setCreateKind(null)}
        title={
          createKind === "providers"
            ? "新增供应商连接"
            : createKind === "models"
              ? "新增模型定义"
              : "新增模型角色"
        }
      >
        <form onSubmit={(e) => void handleSubmitCreate(e)} className="space-y-3 text-xs">
          {createKind === "providers" && (
            <>
              <div>
                <label className="mb-1 block text-fg-dim">接入类型</label>
                <select
                  value={createProviderType}
                  onChange={(e) => {
                    const t = e.target.value;
                    setCreateProviderType(t);
                    const spec = snapshot?.provider_types?.[t];
                    if (spec) setCreateBaseUrl(spec.base_url);
                    const used = new Set((snapshot?.entities?.providers || []).map((p) => p.id));
                    setCreateId(automaticEntityId("providers", used, { type: t }));
                  }}
                  className="h-9 w-full rounded-lg border border-border bg-input px-3 text-fg"
                >
                  {Object.entries(snapshot?.provider_types || {}).map(([k, spec]) => (
                    <option key={k} value={k}>
                      {spec.label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-fg-dim">API Base URL</label>
                <Input
                  type="url"
                  required
                  value={createBaseUrl}
                  onChange={(e) => setCreateBaseUrl(e.target.value)}
                />
              </div>
              {snapshot?.provider_types?.[createProviderType]?.auth === "adc" ? (
                <div className="grid grid-cols-2 gap-2">
                  <Input
                    value={createProject}
                    onChange={(e) => setCreateProject(e.target.value)}
                    placeholder="GCP Project"
                  />
                  <Input
                    value={createLocation}
                    onChange={(e) => setCreateLocation(e.target.value)}
                    placeholder="Location (global)"
                  />
                  <Input
                    value={createCredsEnv}
                    onChange={(e) => setCreateCredsEnv(e.target.value)}
                    placeholder="凭据路径环境变量（可选）"
                    className="col-span-2"
                  />
                </div>
              ) : (
                <div>
                  <label className="mb-1 block text-fg-dim">密钥环境变量名</label>
                  <Input
                    value={createApiKeyEnv}
                    onChange={(e) => setCreateApiKeyEnv(e.target.value)}
                    placeholder="如 DEEPSEEK_API_KEY"
                  />
                </div>
              )}
            </>
          )}

          {createKind === "models" && (
            <>
              <div>
                <label className="mb-1 block text-fg-dim">所属供应商</label>
                <select
                  value={createModelProvider}
                  onChange={(e) => setCreateModelProvider(e.target.value)}
                  className="h-9 w-full rounded-lg border border-border bg-input px-3 text-fg"
                >
                  {(snapshot?.entities?.providers || []).map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label || p.id}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="mb-1 block text-fg-dim">远端模型名称</label>
                <Input
                  required
                  value={createModelName}
                  onChange={(e) => {
                    const name = e.target.value;
                    setCreateModelName(name);
                    const used = new Set((snapshot?.entities?.models || []).map((m) => m.id));
                    setCreateId(
                      automaticEntityId("models", used, {
                        provider: createModelProvider,
                        name,
                      }),
                    );
                  }}
                  placeholder="例如 gpt-4.1 / deepseek-chat"
                />
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="mb-1 block text-fg-dim">API 协议</label>
                  <Input
                    value={createApiStyle}
                    onChange={(e) => setCreateApiStyle(e.target.value)}
                  />
                </div>
                <div>
                  <label className="mb-1 block text-fg-dim">上下文窗口</label>
                  <Input
                    type="number"
                    value={createContextWindow}
                    onChange={(e) => setCreateContextWindow(Number(e.target.value) || 131072)}
                  />
                </div>
              </div>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={createVision}
                  onChange={(e) => setCreateVision(e.target.checked)}
                  className="accent-accent"
                />
                <span>支持视觉图片输入</span>
              </label>
            </>
          )}

          {createKind === "roles" && (
            <div>
              <label className="mb-1 block text-fg-dim">绑定模型</label>
              <select
                value={createRoleModel}
                onChange={(e) => setCreateRoleModel(e.target.value)}
                className="h-9 w-full rounded-lg border border-border bg-input px-3 text-fg"
              >
                {(snapshot?.entities?.models || []).map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label || m.name || m.id}
                  </option>
                ))}
              </select>
            </div>
          )}

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="mb-1 block text-fg-dim">内部标识 ID</label>
              <Input
                required
                value={createId}
                onChange={(e) => setCreateId(e.target.value)}
                className="font-mono"
              />
            </div>
            <div>
              <label className="mb-1 block text-fg-dim">显示备注</label>
              <Input
                value={createLabel}
                onChange={(e) => setCreateLabel(e.target.value)}
                placeholder="可选"
              />
            </div>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={() => setCreateKind(null)}>
              取消
            </Button>
            <Button type="submit" variant="primary" disabled={busy || !createId.trim()}>
              生成创建预览
            </Button>
          </div>
        </form>
      </Dialog>

      {/* Discover Models Catalog Dialog */}
      <Dialog
        open={catalog !== null}
        onOpenChange={(o) => !o && setCatalog(null)}
        title={`远端模型目录 · ${catalog?.provider || ""}`}
        description={catalog?.notice || "选择需要导入为配置的模型"}
        className="w-[min(94vw,640px)]"
      >
        {catalog && (
          <div className="space-y-3 text-xs">
            <Input
              value={catalogSearch}
              onChange={(e) => setCatalogSearch(e.target.value)}
              placeholder="过滤模型 ID 或名称…"
              className="h-8 text-xs"
            />
            <div className="max-h-72 space-y-1 overflow-y-auto rounded-xl border border-border-soft p-2">
              {catalog.models
                .filter((m) => {
                  const q = catalogSearch.trim().toLowerCase();
                  return (
                    !q || m.name.toLowerCase().includes(q) || m.import_id.toLowerCase().includes(q)
                  );
                })
                .map((m) => (
                  <label
                    key={m.import_id}
                    className="flex cursor-pointer items-center justify-between gap-2 rounded-lg px-2 py-1.5 hover:bg-hover"
                  >
                    <span className="flex min-w-0 items-center gap-2">
                      <input
                        type="checkbox"
                        disabled={Boolean(m.configured)}
                        checked={catalogSelected.has(m.import_id)}
                        onChange={(e) => {
                          setCatalogSelected((prev) => {
                            const next = new Set(prev);
                            if (e.target.checked && next.size < 50) next.add(m.import_id);
                            else next.delete(m.import_id);
                            return next;
                          });
                        }}
                        className="accent-accent"
                      />
                      <span className="truncate font-mono text-fg">{m.name}</span>
                    </span>
                    {m.configured && <Badge variant="default">已存在</Badge>}
                  </label>
                ))}
            </div>
            <div className="flex items-center justify-between">
              <span className="text-fg-dim">已选择 {catalogSelected.size} 个模型</span>
              <div className="flex gap-2">
                <Button variant="ghost" onClick={() => setCatalog(null)}>
                  关闭
                </Button>
                <Button
                  variant="primary"
                  disabled={catalogSelected.size === 0 || busy}
                  onClick={() => void handlePreviewCatalogImport()}
                >
                  生成导入预览
                </Button>
              </div>
            </div>
          </div>
        )}
      </Dialog>
    </div>
  );
}
