import { useCallback, useEffect, useState } from "react";
import { Activity, Clock, Cpu, Database, RefreshCw, RotateCw, ShieldCheck, UploadCloud } from "lucide-react";
import { apiFetch } from "../../api/client";
import { wsClient } from "../../api/ws";
import { Badge } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { toast } from "../../components/ui/Toast";

interface StatusSnapshot {
  agent_name?: string | null;
  session?: Record<string, unknown> | null;
  tokens?: { accumulated?: number };
  usage?: Record<string, unknown>;
  heartbeat?: { interval_seconds?: number | null; next_beat_at?: number | null };
  db?: Record<string, number>;
  connections?: number;
  time?: number;
}

interface MaintenanceSnapshot {
  available?: boolean;
  can_update?: boolean;
  web_allowed?: boolean;
  version?: { version?: string; commit?: string; built_at?: string } | null;
  check?: {
    ok?: boolean;
    available?: boolean;
    current?: string;
    latest?: string;
    message?: string;
    data_compatible?: boolean;
    checked_at?: number;
  } | null;
  operation?: {
    id?: string;
    action?: string;
    version?: string;
    state?: string;
    error?: string;
  } | null;
}

function formatEpoch(sec?: number | null): string {
  if (!sec || !Number.isFinite(sec)) return "—";
  return new Date(sec * 1000).toLocaleString();
}

export function StatusPanel() {
  const [status, setStatus] = useState<StatusSnapshot | null>(null);
  const [maintenance, setMaintenance] = useState<MaintenanceSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [operationNotice, setOperationNotice] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    setLoading(true);
    try {
      const [s, m] = await Promise.all([
        apiFetch<StatusSnapshot>("/api/status"),
        apiFetch<MaintenanceSnapshot>("/api/maintenance").catch(() => null),
      ]);
      setStatus(s);
      if (m) setMaintenance(m);
    } catch (err) {
      toast(err instanceof Error ? err.message : "获取系统状态失败", "error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  useEffect(() => {
    const u1 = wsClient.on("status.snapshot", (ev) => {
      if (ev.data) setStatus(ev.data as unknown as StatusSnapshot);
    });
    const u2 = wsClient.on("maintenance.updated", (ev) => {
      if (ev.data) {
        setMaintenance((prev) => ({ ...(prev || {}), ...(ev.data as unknown as MaintenanceSnapshot) }));
      }
    });
    return () => {
      u1();
      u2();
    };
  }, []);

  const pollMaintenanceOperation = async (operationId: string) => {
    for (let i = 0; i < 60; i++) {
      try {
        const snap = await apiFetch<MaintenanceSnapshot>(
          `/api/maintenance?operation_id=${encodeURIComponent(operationId)}`,
        );
        setMaintenance(snap);
        setOperationNotice(null);
        const state = snap.operation?.state;
        if (state && !["waiting", "submitting", "running"].includes(state)) {
          return;
        }
      } catch {
        setOperationNotice("连接暂时断开，正在等待服务恢复…");
      }
      await new Promise((r) => setTimeout(r, 800));
    }
  };

  const triggerMaintenance = async (action: "check" | "update" | "restart") => {
    if (action === "restart" && !window.confirm("确定请求宿主重启服务？当前进行中的对话完成后将短暂断开。")) {
      return;
    }
    if (
      action === "update" &&
      !window.confirm(
        `确定升级到版本 ${maintenance?.check?.latest || ""} 并重启服务？`,
      )
    ) {
      return;
    }
    setBusyAction(action);
    setOperationNotice(null);
    try {
      let revision: string | undefined;
      if (action === "restart") {
        const cfg = await apiFetch<{ revision?: string }>("/api/configuration");
        revision = cfg.revision;
      }
      const op = await apiFetch<{ id?: string; state?: string }>("/api/maintenance", {
        method: "POST",
        body: JSON.stringify({
          action,
          ...(action === "update" && maintenance?.check?.latest
            ? { version: maintenance.check.latest }
            : {}),
          ...(revision ? { revision } : {}),
        }),
      });
      toast(`已提交维护指令：${action}`, "ok");
      if (op?.id) {
        await pollMaintenanceOperation(op.id);
      } else {
        await loadAll();
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : "提交维护操作失败", "error");
    } finally {
      setBusyAction(null);
    }
  };

  const opState = maintenance?.operation?.state;
  const opBusy = opState === "waiting" || opState === "submitting" || opState === "running";

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      <div className="flex items-center justify-between border-b border-border bg-surface px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Activity className="h-4 w-4 text-accent" />
          <h2 className="text-sm font-semibold text-fg">运行状态与主机维护</h2>
          {status?.agent_name && <Badge variant="accent">{status.agent_name}</Badge>}
        </div>
        <Button size="sm" variant="secondary" onClick={() => void loadAll()} disabled={loading}>
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          刷新
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-4xl space-y-5">
          {/* Core Metrics */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-2xl border border-border bg-surface p-4">
              <div className="text-[11px] text-fg-faint">活跃 WebSocket 连接</div>
              <div className="mt-1 font-mono text-2xl font-semibold text-fg">
                {status?.connections ?? "—"}
              </div>
            </div>
            <div className="rounded-2xl border border-border bg-surface p-4">
              <div className="text-[11px] text-fg-faint">当前上下文累积 Token</div>
              <div className="mt-1 font-mono text-2xl font-semibold text-fg">
                {(status?.tokens?.accumulated ?? 0).toLocaleString()}
              </div>
            </div>
            <div className="rounded-2xl border border-border bg-surface p-4">
              <div className="text-[11px] text-fg-faint">心跳间隔</div>
              <div className="mt-1 font-mono text-xl font-semibold text-fg">
                {status?.heartbeat?.interval_seconds ? `${status.heartbeat.interval_seconds}s` : "未配置"}
              </div>
            </div>
            <div className="rounded-2xl border border-border bg-surface p-4">
              <div className="text-[11px] text-fg-faint">下次心跳时间</div>
              <div className="mt-1 text-xs font-medium text-fg">
                {formatEpoch(status?.heartbeat?.next_beat_at)}
              </div>
            </div>
          </div>

          {/* Active Session + Runtime Usage */}
          <div className="grid gap-4 md:grid-cols-2">
            <section className="rounded-2xl border border-border bg-surface p-4">
              <div className="mb-2.5 flex items-center gap-2">
                <Clock className="h-4 w-4 text-accent" />
                <h3 className="text-sm font-semibold text-fg">当前会话与事件信息</h3>
              </div>
              <dl className="space-y-1.5 text-xs">
                <div className="flex justify-between gap-3">
                  <dt className="text-fg-dim">会话 ID</dt>
                  <dd className="font-mono text-fg">
                    {String(status?.session?.session_id || "—")}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-fg-dim">开始时间</dt>
                  <dd className="text-fg">
                    {formatEpoch(Number(status?.session?.start_time || 0) || null)}
                  </dd>
                </div>
                <div className="flex justify-between gap-3">
                  <dt className="text-fg-dim">服务端快照时刻</dt>
                  <dd className="text-fg">{formatEpoch(status?.time)}</dd>
                </div>
              </dl>
              {status?.usage && Object.keys(status.usage).length > 0 && (
                <div className="mt-3 border-t border-border-soft pt-3">
                  <div className="mb-1 text-[11px] font-medium text-fg-faint">运行态用量摘要</div>
                  <pre className="max-h-36 overflow-auto rounded-lg bg-elevated/60 p-2 font-mono text-[11px] text-fg-dim">
                    {JSON.stringify(status.usage, null, 2)}
                  </pre>
                </div>
              )}
            </section>

            <section className="rounded-2xl border border-border bg-surface p-4">
              <div className="mb-2.5 flex items-center gap-2">
                <Database className="h-4 w-4 text-accent" />
                <h3 className="text-sm font-semibold text-fg">Chronicle 数据表统计</h3>
              </div>
              {!status?.db || Object.keys(status.db).length === 0 ? (
                <p className="text-xs text-fg-faint">暂无表计数数据。</p>
              ) : (
                <div className="grid grid-cols-2 gap-2 text-xs">
                  {Object.entries(status.db).map(([table, count]) => (
                    <div
                      key={table}
                      className="flex items-center justify-between rounded-xl border border-border-soft bg-elevated/50 px-3 py-2"
                    >
                      <span className="truncate font-mono text-fg-dim">{table}</span>
                      <span className="font-mono font-semibold text-fg">{count}</span>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>

          {/* Host Maintenance Controller */}
          <section className="rounded-2xl border border-border bg-surface p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <ShieldCheck className="h-4 w-4 text-accent" />
                <h3 className="text-sm font-semibold text-fg">主机维护与版本更新</h3>
                <Badge variant={maintenance?.available ? "ok" : "default"}>
                  {maintenance?.available ? "Linux 维护控制器就绪" : "当前环境未装配主机守护"}
                </Badge>
              </div>
              {maintenance?.version?.version && (
                <Badge variant="default" data-testid="host-version" className="font-mono">
                  {maintenance.version.version.startsWith("v")
                    ? maintenance.version.version
                    : `v${maintenance.version.version}`}
                </Badge>
              )}
            </div>

            {maintenance?.check && (
              <div className="mt-3 rounded-xl border border-border-soft bg-elevated/50 p-3 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-fg">
                    当前版本：{maintenance.check.current || maintenance.version?.version || "—"}
                  </span>
                  {maintenance.check.latest && (
                    <span className="font-mono text-accent">
                      最新版本：{maintenance.check.latest}
                    </span>
                  )}
                  {maintenance.can_update && <Badge variant="ok">可在线升级</Badge>}
                </div>
                {maintenance.check.message && (
                  <p className="mt-1 text-fg-dim">{maintenance.check.message}</p>
                )}
              </div>
            )}

            {(maintenance?.operation || operationNotice) && (
              <div
                data-testid="host-operation"
                className="mt-3 rounded-xl border border-border-soft bg-elevated/50 p-3 text-xs"
              >
                {operationNotice ? (
                  <p className="text-warn">{operationNotice}</p>
                ) : maintenance?.operation && (
                  <>
                    <div className="flex items-center gap-2">
                      <Cpu className="h-3.5 w-3.5 text-accent" />
                      <span className="font-medium text-fg">
                        最近操作：{maintenance.operation.action}
                        {maintenance.operation.version ? ` ${maintenance.operation.version}` : ""}
                      </span>
                      <Badge variant={opBusy ? "warn" : "default"}>
                        {maintenance.operation.state}
                      </Badge>
                    </div>
                    {maintenance.operation.error && (
                      <p className="mt-1 text-danger">{maintenance.operation.error}</p>
                    )}
                  </>
                )}
              </div>
            )}

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="secondary"
                disabled={
                  !maintenance?.available ||
                  !maintenance.web_allowed ||
                  opBusy ||
                  busyAction !== null
                }
                onClick={() => void triggerMaintenance("check")}
              >
                <RefreshCw className="h-3.5 w-3.5" />
                检查更新
              </Button>
              <Button
                size="sm"
                variant="primary"
                disabled={
                  !maintenance?.available ||
                  !maintenance.web_allowed ||
                  !maintenance.can_update ||
                  opBusy ||
                  busyAction !== null
                }
                onClick={() => void triggerMaintenance("update")}
              >
                <UploadCloud className="h-3.5 w-3.5" />
                应用更新
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={
                  !maintenance?.available ||
                  !maintenance.web_allowed ||
                  opBusy ||
                  busyAction !== null
                }
                onClick={() => void triggerMaintenance("restart")}
              >
                <RotateCw className="h-3.5 w-3.5" />
                请求宿主重启
              </Button>
              {maintenance?.available && !maintenance.web_allowed && (
                <span className="text-[11px] text-warn">
                  请先在「设置」中启用 Web 访问密码或设备验证后再通过 Web 操作维护接口。
                </span>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
