import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Chart from "chart.js/auto";
import { BarChart3, RefreshCw, Wallet } from "lucide-react";
import { apiFetch } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { toast } from "../../components/ui/Toast";
import { useThemeStore } from "../../stores/themeStore";
import { useIsCompactLayout } from "../workbench/panels";

interface UsagePoint {
  bucket: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  cache_write_tokens: number;
  requests: number;
  cache_hit_rate: number | null;
}

interface UsageSummaryBucket {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
  cache_write_tokens: number;
  requests: number;
  cache_hit_rate: number | null;
}

interface UsageFilterOptions {
  providers: Array<{ id: string; label: string }>;
  models: Array<{ id: string; label: string; provider_ids?: string[] }>;
  roles: Array<{ id: string; label: string }>;
}

interface UsagePayload {
  granularity: "day" | "hour";
  points: UsagePoint[];
  summary: Record<"today" | "week" | "month" | "all", UsageSummaryBucket>;
  options: UsageFilterOptions;
  range?: { start_date?: string | null; end_date?: string | null; days?: number | null } | null;
}

interface BalanceItem {
  provider_id: string;
  label: string;
  ok: boolean;
  error?: string;
  is_available?: boolean;
  balances?: Array<{
    currency?: string;
    total_balance?: string | number;
    granted_balance?: string | number;
    topped_up_balance?: string | number;
  }>;
  fetched_at?: number;
}

export function UsagePanel() {
  const compact = useIsCompactLayout();
  const theme = useThemeStore((s) => s.resolved);

  const tokenCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const cacheCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const tokenChartRef = useRef<Chart | null>(null);
  const cacheChartRef = useRef<Chart | null>(null);

  const [granularity, setGranularity] = useState<"day" | "hour">("day");
  const [providerId, setProviderId] = useState("");
  const [modelId, setModelId] = useState("");
  const [role, setRole] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");

  const [usage, setUsage] = useState<UsagePayload | null>(null);
  const [balances, setBalances] = useState<BalanceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingBalances, setLoadingBalances] = useState(false);

  const loadUsage = useCallback(async () => {
    setLoading(true);
    try {
      const qs = new URLSearchParams({ granularity });
      if (providerId) qs.set("provider_id", providerId);
      if (modelId) qs.set("model_id", modelId);
      if (role) qs.set("role", role);
      if (startDate) qs.set("start_date", startDate);
      if (endDate) qs.set("end_date", endDate);

      const data = await apiFetch<UsagePayload>(`/api/usage?${qs.toString()}`);
      setUsage(data);
    } catch (err) {
      toast(err instanceof Error ? err.message : "加载用量统计失败", "error");
    } finally {
      setLoading(false);
    }
  }, [granularity, providerId, modelId, role, startDate, endDate]);

  const loadBalances = useCallback(
    async (force = false) => {
      setLoadingBalances(true);
      try {
        const qs = new URLSearchParams();
        if (providerId) qs.set("provider_id", providerId);
        if (force) qs.set("force", "true");
        const q = qs.toString();
        const res = await apiFetch<{ items?: BalanceItem[] }>(
          `/api/balance${q ? `?${q}` : ""}`,
        );
        setBalances(res.items || []);
      } catch (err) {
        toast(err instanceof Error ? err.message : "查询账户余额失败", "error");
      } finally {
        setLoadingBalances(false);
      }
    },
    [providerId],
  );

  useEffect(() => {
    void loadUsage();
  }, [loadUsage]);

  useEffect(() => {
    void loadBalances(false);
  }, [loadBalances]);

  const filteredModelOptions = useMemo(() => {
    const models = usage?.options?.models || [];
    if (!providerId) return models;
    return models.filter(
      (m) => !m.provider_ids || m.provider_ids.includes(providerId),
    );
  }, [usage?.options?.models, providerId]);

  // Render Chart.js canvases synced with [data-theme]
  useEffect(() => {
    tokenChartRef.current?.destroy();
    cacheChartRef.current?.destroy();
    tokenChartRef.current = null;
    cacheChartRef.current = null;

    if (!usage) return;
    const points = usage.points || [];
    const labels = points.map((p) => p.bucket);

    const textColor = theme === "light" ? "#574a3f" : "#c7baa9";
    const gridColor = theme === "light" ? "rgba(40,32,25,0.08)" : "rgba(255,235,215,0.08)";

    if (tokenCanvasRef.current) {
      tokenChartRef.current = new Chart(tokenCanvasRef.current, {
        type: "bar",
        data: {
          labels,
          datasets: [
            {
              label: "未缓存输入",
              data: points.map((p) =>
                Math.max(0, p.prompt_tokens - p.cached_tokens - p.cache_write_tokens),
              ),
              backgroundColor: theme === "light" ? "#528977" : "#7fd6ba",
              stack: "tokens",
            },
            {
              label: "缓存命中",
              data: points.map((p) => p.cached_tokens),
              backgroundColor: theme === "light" ? "#3b76b0" : "#6ea8e0",
              stack: "tokens",
            },
            {
              label: "缓存写入",
              data: points.map((p) => p.cache_write_tokens),
              backgroundColor: theme === "light" ? "#7e6ab8" : "#b6a9ef",
              stack: "tokens",
            },
            {
              label: "输出完成",
              data: points.map((p) => p.completion_tokens),
              backgroundColor: theme === "light" ? "#b55334" : "#d97757",
              stack: "tokens",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: textColor, boxWidth: 12, font: { size: 11 } } },
          },
          scales: {
            x: { ticks: { color: textColor, maxRotation: 40 }, grid: { color: gridColor } },
            y: { stacked: true, ticks: { color: textColor }, grid: { color: gridColor } },
          },
        },
      });
    }

    if (cacheCanvasRef.current) {
      cacheChartRef.current = new Chart(cacheCanvasRef.current, {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              label: "Prompt 缓存命中率 (%)",
              data: points.map((p) => p.cache_hit_rate),
              borderColor: theme === "light" ? "#b55334" : "#d97757",
              backgroundColor:
                theme === "light" ? "rgba(181,83,52,0.14)" : "rgba(217,119,87,0.16)",
              fill: true,
              tension: 0.28,
              spanGaps: true,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: textColor, boxWidth: 12, font: { size: 11 } } },
          },
          scales: {
            x: { ticks: { color: textColor, maxRotation: 40 }, grid: { color: gridColor } },
            y: {
              min: 0,
              max: 100,
              ticks: { color: textColor },
              grid: { color: gridColor },
            },
          },
        },
      });
    }

    return () => {
      tokenChartRef.current?.destroy();
      cacheChartRef.current?.destroy();
      tokenChartRef.current = null;
      cacheChartRef.current = null;
    };
  }, [usage, theme]);

  const cards = [
    { key: "today" as const, title: "今日用量" },
    { key: "week" as const, title: "近 7 天" },
    { key: "month" as const, title: "近 30 天" },
    { key: "all" as const, title: "筛选 / 累计总计" },
  ];

  const filterControls = (
    <>
      <select
        value={providerId}
        onChange={(e) => {
          setProviderId(e.target.value);
          setModelId("");
        }}
        className="h-8 rounded-lg border border-border bg-input px-2.5 text-xs text-fg"
        aria-label="按供应商筛选"
      >
        <option value="">全部供应商</option>
        {(usage?.options?.providers || []).map((p) => (
          <option key={p.id} value={p.id}>
            {p.label}
          </option>
        ))}
      </select>

      <select
        value={modelId}
        onChange={(e) => setModelId(e.target.value)}
        className="h-8 rounded-lg border border-border bg-input px-2.5 text-xs text-fg"
        aria-label="按模型筛选"
      >
        <option value="">全部模型</option>
        {filteredModelOptions.map((m) => (
          <option key={m.id} value={m.id}>
            {m.label}
          </option>
        ))}
      </select>

      <select
        value={role}
        onChange={(e) => setRole(e.target.value)}
        className="h-8 rounded-lg border border-border bg-input px-2.5 text-xs text-fg"
        aria-label="按角色筛选"
      >
        <option value="">全部角色</option>
        {(usage?.options?.roles || []).map((r) => (
          <option key={r.id} value={r.id}>
            {r.label}
          </option>
        ))}
      </select>

      <Input
        type="date"
        value={startDate}
        onChange={(e) => setStartDate(e.target.value)}
        className="h-8 w-36 text-xs"
        aria-label="起始日期"
      />
      <span className="text-fg-faint">至</span>
      <Input
        type="date"
        value={endDate}
        onChange={(e) => setEndDate(e.target.value)}
        className="h-8 w-36 text-xs"
        aria-label="结束日期"
      />
      {(providerId || modelId || role || startDate || endDate) && (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => {
            setProviderId("");
            setModelId("");
            setRole("");
            setStartDate("");
            setEndDate("");
          }}
        >
          重置筛选
        </Button>
      )}
    </>
  );

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-3 py-2 md:px-4 md:py-2.5">
        <div className="flex items-center gap-2">
          <BarChart3 className="h-4 w-4 text-accent" />
          <h2 className="text-sm font-semibold text-fg">模型用量与账户余额</h2>
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            size="sm"
            variant={granularity === "day" ? "primary" : "secondary"}
            onClick={() => setGranularity("day")}
          >
            按天
          </Button>
          <Button
            size="sm"
            variant={granularity === "hour" ? "primary" : "secondary"}
            onClick={() => setGranularity("hour")}
          >
            按小时
          </Button>
          <Button size="sm" variant="secondary" onClick={() => void loadUsage()} disabled={loading}>
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            刷新图表
          </Button>
        </div>
      </div>

      {/* Filters: Fixed below header on desktop, part of single scroll flow on mobile */}
      {!compact && (
        <div className="flex flex-wrap items-center gap-2 border-b border-border-soft bg-surface/60 px-4 py-2 text-xs">
          {filterControls}
        </div>
      )}

      {/* Main */}
      <div className="min-h-0 flex-1 overflow-y-auto p-3 md:p-4">
        <div className="mx-auto max-w-5xl space-y-4 md:space-y-5">
          {compact && (
            <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-border bg-surface p-3 text-xs">
              {filterControls}
            </div>
          )}
          {/* KPI Summary Cards */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {cards.map(({ key, title }) => {
              const b = usage?.summary?.[key];
              return (
                <div key={key} className="rounded-2xl border border-border bg-surface p-4">
                  <div className="text-[11px] text-fg-faint">{title}</div>
                  <div className="mt-1 font-mono text-xl font-semibold text-fg">
                    {(b?.total_tokens ?? 0).toLocaleString()}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-fg-dim">
                    <span>{b?.requests ?? 0} 次请求</span>
                    <span>
                      命中率 {b?.cache_hit_rate != null ? `${b.cache_hit_rate}%` : "—"}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Charts */}
          <div className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-2xl border border-border bg-surface p-4">
              <h3 className="mb-3 text-xs font-semibold text-fg">Token 构成趋势</h3>
              <div className="h-64">
                <canvas ref={tokenCanvasRef} />
              </div>
            </div>
            <div className="rounded-2xl border border-border bg-surface p-4">
              <h3 className="mb-3 text-xs font-semibold text-fg">缓存命中率变化</h3>
              <div className="h-64">
                <canvas ref={cacheCanvasRef} />
              </div>
            </div>
          </div>

          {/* Provider Balances */}
          <section className="rounded-2xl border border-border bg-surface p-4">
            <div className="mb-3 flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <Wallet className="h-4 w-4 text-accent" />
                <h3 className="text-sm font-semibold text-fg">供应商账户余额</h3>
              </div>
              <Button
                size="sm"
                variant="secondary"
                disabled={loadingBalances}
                onClick={() => void loadBalances(true)}
              >
                <RefreshCw className={cn("h-3.5 w-3.5", loadingBalances && "animate-spin")} />
                强制刷新余额
              </Button>
            </div>

            {balances.length === 0 ? (
              <p className="text-xs text-fg-faint">暂无余额信息。</p>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2">
                {balances.map((item) => (
                  <div
                    key={item.provider_id || item.label}
                    className="rounded-xl border border-border-soft bg-elevated/50 p-3 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-fg">{item.label}</span>
                      <Badge variant={item.ok ? "ok" : "default"}>
                        {item.ok ? "查询成功" : "未查询/不支持"}
                      </Badge>
                    </div>
                    {!item.ok ? (
                      <p className="mt-1.5 text-fg-dim">{item.error || "暂无可用余额数据"}</p>
                    ) : (
                      <div className="mt-2 space-y-1">
                        {(item.balances || []).map((b, idx) => (
                          <div key={idx} className="flex items-baseline justify-between gap-2">
                            <span className="font-mono text-fg-dim">{b.currency || "CNY"}</span>
                            <span className="font-mono text-base font-semibold text-fg">
                              {b.total_balance ?? "0"}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
