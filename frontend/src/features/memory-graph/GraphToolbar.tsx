import { useRef, useState, type RefObject } from "react";
import {
  ChevronDown,
  Download,
  Eye,
  RefreshCw,
  Search,
  Upload,
  X,
} from "lucide-react";
import { Button, cn } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Switch } from "../../components/ui/Switch";
import {
  EVENT_SUBTYPE_SHORT_NAMES,
  TYPE_DISPLAY_NAMES,
  type ComputedGraphResult,
  type EventSubType,
  type EventSubTypeFilter,
  type GraphNode,
  type GraphNodeType,
  type GraphPalette,
  type LeafFoldMode,
} from "./types";

const ALL_TYPES: GraphNodeType[] = ["Person", "Concept", "Event", "Location", "Other"];
const EVENT_SUBTYPE_ORDER: EventSubType[] = ["milestone", "travel", "study", "interaction"];

export interface GraphToolbarProps {
  graph: ComputedGraphResult | null;
  palette: GraphPalette;
  loading: boolean;
  hasRawDoc: boolean;
  searchInputRef: RefObject<HTMLInputElement | null>;
  searchWrapRef: RefObject<HTMLDivElement | null>;
  settingsWrapRef: RefObject<HTMLDivElement | null>;
  searchQuery: string;
  onSearchQueryChange: (q: string) => void;
  searchOpen: boolean;
  onSearchOpenChange: (open: boolean) => void;
  searchMatches: GraphNode[];
  onSelectNode: (node: GraphNode) => void;
  visibleTypes: Record<GraphNodeType, boolean>;
  onToggleType: (t: GraphNodeType) => void;
  typeCounts: Record<GraphNodeType, number>;
  visibleEventKinds: EventSubTypeFilter;
  eventKindCounts: Record<EventSubType | "other", number>;
  onToggleEventKind: (kind: EventSubType | "other") => void;
  onResetEventKinds: () => void;
  showViewSettings: boolean;
  onToggleViewSettings: () => void;
  correctHubs: boolean;
  centerNodeId: number | null;
  personNodes: GraphNode[];
  onReLayout: (correctHubs: boolean, centerId: number | null) => void;
  showClusters: boolean;
  onShowClustersChange: (v: boolean) => void;
  showCrossEdges: boolean;
  onShowCrossEdgesChange: (v: boolean) => void;
  showLabels: boolean;
  onShowLabelsChange: (v: boolean) => void;
  showEdgeLabels: boolean;
  onShowEdgeLabelsChange: (v: boolean) => void;
  smoothMotion: boolean;
  onSmoothMotionChange: (v: boolean) => void;
  foldLeaves: LeafFoldMode;
  onFoldLeavesChange: (v: LeafFoldMode) => void;
  mergeEquivalenceGroups: boolean;
  onMergeEquivalenceGroupsChange: (v: boolean) => void;
  equivalenceGroupCount: number;
  minDegree: number;
  onMinDegreeChange: (v: number) => void;
  edgeOpacity: number;
  onEdgeOpacityChange: (v: number) => void;
  nodeSize: number;
  onNodeSizeChange: (v: number) => void;
  edgeBudget: number;
  onEdgeBudgetChange: (v: number) => void;
  islandCount: number;
  onImportFile: (file: File | undefined) => void;
  onExportJson: () => void;
  onRefresh: () => void;
}

export function GraphToolbar({
  graph,
  palette,
  loading,
  hasRawDoc,
  searchInputRef,
  searchWrapRef,
  settingsWrapRef,
  searchQuery,
  onSearchQueryChange,
  searchOpen,
  onSearchOpenChange,
  searchMatches,
  onSelectNode,
  visibleTypes,
  onToggleType,
  typeCounts,
  visibleEventKinds,
  eventKindCounts,
  onToggleEventKind,
  onResetEventKinds,
  showViewSettings,
  onToggleViewSettings,
  correctHubs,
  centerNodeId,
  personNodes,
  onReLayout,
  showClusters,
  onShowClustersChange,
  showCrossEdges,
  onShowCrossEdgesChange,
  showLabels,
  onShowLabelsChange,
  showEdgeLabels,
  onShowEdgeLabelsChange,
  smoothMotion,
  onSmoothMotionChange,
  foldLeaves,
  onFoldLeavesChange,
  mergeEquivalenceGroups,
  onMergeEquivalenceGroupsChange,
  equivalenceGroupCount,
  minDegree,
  onMinDegreeChange,
  edgeOpacity,
  onEdgeOpacityChange,
  nodeSize,
  onNodeSizeChange,
  edgeBudget,
  onEdgeBudgetChange,
  islandCount,
  onImportFile,
  onExportJson,
  onRefresh,
}: GraphToolbarProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const hasEventSubtypes = EVENT_SUBTYPE_ORDER.some((k) => eventKindCounts[k] > 0);
  const anyEventKindHidden = Object.values(visibleEventKinds).some((v) => !v);

  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-surface px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <div ref={searchWrapRef} className="relative w-56 sm:w-64">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-3.5 w-3.5 text-fg-faint" />
          <Input
            ref={searchInputRef}
            data-testid="memory-graph-search"
            value={searchQuery}
            onFocus={() => onSearchOpenChange(true)}
            onChange={(e) => {
              onSearchQueryChange(e.target.value);
              onSearchOpenChange(true);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && searchMatches.length > 0) {
                e.preventDefault();
                onSelectNode(searchMatches[0]!);
              }
            }}
            placeholder="搜索节点 (/ 聚焦)..."
            className="h-8 pl-8 pr-7 text-xs"
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => {
                onSearchQueryChange("");
                onSearchOpenChange(false);
              }}
              className="absolute right-2 top-2 text-fg-faint hover:text-fg"
              aria-label="清除搜索"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          )}

          {searchOpen && searchQuery.trim() !== "" && (
            <div className="absolute left-0 right-0 top-9 z-30 max-h-72 overflow-y-auto rounded-xl border border-border bg-surface/95 p-1 shadow-xl backdrop-blur-md">
              {searchMatches.length === 0 ? (
                <p className="px-2.5 py-2 text-xs text-fg-faint">没有找到匹配节点</p>
              ) : (
                searchMatches.map((node) => (
                  <button
                    key={node.id}
                    type="button"
                    onClick={() => onSelectNode(node)}
                    className="flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs hover:bg-hover"
                  >
                    <span className="flex min-w-0 items-center gap-2">
                      <span
                        className="h-2 w-2 shrink-0 rounded-full"
                        style={{ backgroundColor: palette[node.type] }}
                      />
                      <span className="truncate text-fg">{node.name}</span>
                    </span>
                    <span className="shrink-0 font-mono text-[10px] text-fg-faint">
                      {TYPE_DISPLAY_NAMES[node.type]} · {node.degree}
                    </span>
                  </button>
                ))
              )}
            </div>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1">
          {ALL_TYPES.map((t) => {
            const active = visibleTypes[t];
            return (
              <button
                key={t}
                type="button"
                data-filter-type={t}
                aria-pressed={active}
                onClick={() => onToggleType(t)}
                className={cn(
                  "inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] transition-colors",
                  active
                    ? "border-border bg-elevated text-fg"
                    : "border-transparent bg-transparent text-fg-faint opacity-60 hover:opacity-100",
                )}
              >
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ backgroundColor: palette[t] }}
                />
                <span>{TYPE_DISPLAY_NAMES[t]}</span>
                <span className="font-mono text-[10px] text-fg-dim">{typeCounts[t]}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div ref={settingsWrapRef} className="relative flex flex-wrap items-center gap-1.5">
        <Button
          size="sm"
          variant="secondary"
          data-testid="memory-graph-view-settings"
          onClick={onToggleViewSettings}
        >
          <Eye className="h-3.5 w-3.5" />
          视图设置
        </Button>
        <Button size="sm" variant="secondary" onClick={() => fileInputRef.current?.click()}>
          <Download className="h-3.5 w-3.5" />
          导入
        </Button>
        <input
          ref={fileInputRef}
          data-testid="memory-graph-file-input"
          type="file"
          accept=".json,application/json"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            onImportFile(file);
            e.target.value = "";
          }}
        />
        <Button
          size="sm"
          variant="secondary"
          data-testid="memory-graph-export"
          onClick={onExportJson}
          disabled={!hasRawDoc}
        >
          <Upload className="h-3.5 w-3.5" />
          导出
        </Button>
        <Button size="sm" variant="secondary" data-testid="memory-graph-refresh" onClick={onRefresh}>
          <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
          刷新
        </Button>

        {showViewSettings && (
          <div className="absolute right-0 top-10 z-30 max-h-[80vh] w-80 space-y-3 overflow-y-auto rounded-xl border border-border bg-surface/95 p-3.5 shadow-xl backdrop-blur-md">
            <div className="text-[11px] font-medium uppercase tracking-wider text-fg-faint">
              分层显示与骨架过滤
            </div>

            <div>
              <label className="mb-1 block text-xs text-fg-dim">一次性概念叶节点</label>
              <select
                value={foldLeaves}
                onChange={(e) => onFoldLeavesChange(e.target.value as LeafFoldMode)}
                className="w-full rounded-lg border border-border bg-input px-2.5 py-1.5 text-xs text-fg"
              >
                <option value="auto">缩略时自动折叠，放大展开（推荐）</option>
                <option value="always">始终折叠叶节点（最精简骨架）</option>
                <option value="off">始终展开全部节点</option>
              </select>
            </div>

            {equivalenceGroupCount > 0 && (
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-xs font-medium text-fg">合并概念等价组</div>
                  <div className="text-[10px] text-fg-faint">
                    收束 {equivalenceGroupCount} 组同义概念至代表节点
                  </div>
                </div>
                <Switch
                  checked={mergeEquivalenceGroups}
                  onCheckedChange={onMergeEquivalenceGroupsChange}
                  aria-label="合并概念等价组"
                />
              </div>
            )}

            <div>
              <div className="mb-1 flex items-center justify-between text-xs text-fg-dim">
                <span>最小连接数过滤</span>
                <span className="font-mono">
                  {minDegree <= 1 ? "全部" : `≥ ${minDegree} 连接`}
                </span>
              </div>
              <input
                type="range"
                min={1}
                max={5}
                step={1}
                value={minDegree}
                onChange={(e) => onMinDegreeChange(Number(e.target.value))}
                className="w-full accent-accent"
              />
            </div>

            {hasEventSubtypes && (
              <div className="space-y-1.5 border-t border-border-soft pt-2.5">
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-medium uppercase tracking-wider text-fg-faint">
                    事件子类型
                  </span>
                  {anyEventKindHidden && (
                    <button
                      type="button"
                      onClick={onResetEventKinds}
                      className="cursor-pointer text-[11px] text-accent hover:underline"
                    >
                      全部显示
                    </button>
                  )}
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {([...EVENT_SUBTYPE_ORDER, "other"] as const).map((kind) => {
                    const count = eventKindCounts[kind];
                    if (count === 0) return null;
                    const active = visibleEventKinds[kind];
                    const label =
                      kind === "other" ? "未分类" : EVENT_SUBTYPE_SHORT_NAMES[kind];
                    return (
                      <button
                        key={kind}
                        type="button"
                        onClick={() => onToggleEventKind(kind)}
                        className={cn(
                          "inline-flex cursor-pointer items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-colors",
                          active
                            ? "border-border bg-elevated text-fg"
                            : "border-transparent bg-transparent text-fg-faint opacity-55 hover:opacity-100",
                        )}
                      >
                        {kind === "milestone" && <span className="text-warn">✦</span>}
                        <span>{label}</span>
                        <span className="font-mono text-[10px] text-fg-dim">{count}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            <div className="border-t border-border-soft pt-2.5 text-[11px] font-medium uppercase tracking-wider text-fg-faint">
              布局与渲染细节
            </div>

            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-fg">Hub 度数纠偏</span>
              <Switch
                checked={correctHubs}
                onCheckedChange={(v) => onReLayout(v, centerNodeId)}
                aria-label="Hub 度数纠偏"
              />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-fg">显示社群光环与主题</span>
              <Switch
                checked={showClusters}
                onCheckedChange={onShowClustersChange}
                aria-label="显示社群光环"
              />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-fg">显示跨簇连线</span>
              <Switch
                checked={showCrossEdges}
                onCheckedChange={onShowCrossEdgesChange}
                aria-label="显示跨簇连线"
              />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-fg">显示节点标签</span>
              <Switch
                checked={showLabels}
                onCheckedChange={onShowLabelsChange}
                aria-label="显示节点标签"
              />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-fg">局部视图显示关系名</span>
              <Switch
                checked={showEdgeLabels}
                onCheckedChange={onShowEdgeLabelsChange}
                aria-label="局部视图显示关系名"
              />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-fg">焦点平滑过渡</span>
              <Switch
                checked={smoothMotion}
                onCheckedChange={onSmoothMotionChange}
                aria-label="焦点平滑过渡"
              />
            </div>

            <div>
              <div className="mb-1 flex items-center justify-between text-xs text-fg-dim">
                <span>连线强度</span>
                <span className="font-mono">{Math.round(edgeOpacity * 100)}%</span>
              </div>
              <input
                type="range"
                min={0.08}
                max={0.75}
                step={0.02}
                value={edgeOpacity}
                onChange={(e) => onEdgeOpacityChange(Number(e.target.value))}
                className="w-full accent-accent"
              />
            </div>

            <div>
              <div className="mb-1 flex items-center justify-between text-xs text-fg-dim">
                <span>节点大小</span>
                <span className="font-mono">{Math.round(nodeSize * 100)}%</span>
              </div>
              <input
                type="range"
                min={0.4}
                max={1.4}
                step={0.05}
                value={nodeSize}
                onChange={(e) => onNodeSizeChange(Number(e.target.value))}
                className="w-full accent-accent"
              />
            </div>

            <div>
              <label className="mb-1 block text-xs text-fg-dim">概览连线预算</label>
              <select
                value={edgeBudget}
                onChange={(e) => onEdgeBudgetChange(Number(e.target.value))}
                className="w-full rounded-lg border border-border bg-input px-2.5 py-1.5 text-xs text-fg"
              >
                <option value={2000}>2,000 条（清晰推荐）</option>
                <option value={6000}>6,000 条</option>
                <option value={0}>全部显示</option>
              </select>
            </div>

            {personNodes.length > 0 && (
              <div>
                <label className="mb-1 block text-xs text-fg-dim">中心人物视角</label>
                <select
                  value={centerNodeId ?? ""}
                  onChange={(e) =>
                    onReLayout(correctHubs, e.target.value === "" ? null : Number(e.target.value))
                  }
                  className="w-full rounded-lg border border-border bg-input px-2.5 py-1.5 text-xs text-fg"
                >
                  <option value="">自动选择主人物</option>
                  {personNodes.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {graph && (
              <div className="border-t border-border-soft pt-2.5">
                <button
                  type="button"
                  onClick={() => setShowDiagnostics((v) => !v)}
                  className="flex w-full cursor-pointer items-center justify-between text-xs text-fg-dim hover:text-fg"
                >
                  <span>分区计算信息</span>
                  <ChevronDown
                    className={cn("h-3.5 w-3.5 transition-transform", showDiagnostics && "rotate-180")}
                  />
                </button>
                {showDiagnostics && (
                  <div className="mt-2 space-y-1.5 rounded-lg bg-elevated/60 p-2 text-[11px] text-fg-dim">
                    <p className="leading-relaxed">
                      {graph.analysis.clear
                        ? `已划分为 ${islandCount} 个语义群落，含 ${graph.analysis.singleLeafCount || 0} 个单次叶概念。`
                        : "当前连接交织较均匀，保持统一网络视图。"}
                    </p>
                    <dl className="grid grid-cols-2 gap-y-1 font-mono text-[10px]">
                      <dt className="text-fg-faint">结构分数 Q</dt>
                      <dd className="text-right text-fg">{graph.analysis.q.toFixed(3)}</dd>
                      <dt className="text-fg-faint">随机参照</dt>
                      <dd className="text-right text-fg">{graph.analysis.baseline.toFixed(3)}</dd>
                      <dt className="text-fg-faint">结构差值</dt>
                      <dd className="text-right text-fg">{graph.analysis.margin.toFixed(3)}</dd>
                    </dl>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
