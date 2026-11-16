import { useMemo } from "react";
import {
  Focus,
  GitFork,
  GitMerge,
  Split,
  X,
} from "lucide-react";
import { Badge } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { getNodeProperty, unwrapFieldValue } from "./graphIo";
import {
  EVENT_SUBTYPE_NAMES,
  REL_TYPE_LABELS,
  TYPE_DISPLAY_NAMES,
  type ComputedGraphResult,
  type EdgeSentiment,
  type GraphNode,
  type GraphPalette,
  type GraphViewMode,
} from "./types";

const SENTIMENT_PILL_STYLE: Record<EdgeSentiment, string> = {
  喜欢: "border-ok/35 bg-ok-soft text-ok",
  讨厌: "border-danger/35 bg-danger-soft text-danger",
  中立: "border-border-soft bg-elevated/60 text-fg-dim",
  不明确: "border-border-soft bg-elevated/60 text-fg-faint",
};

const SENTIMENT_ICON: Record<EdgeSentiment, string> = {
  喜欢: "♥ ",
  讨厌: "✕ ",
  中立: "",
  不明确: "",
};

export interface GraphInspectorProps {
  graph: ComputedGraphResult;
  selectedNode: GraphNode;
  palette: GraphPalette;
  viewMode: GraphViewMode;
  isLocalImport: boolean;
  detaching: boolean;
  detachUncertain: boolean;
  onClose: () => void;
  onSelectAndFocus: (node: GraphNode) => void;
  onEnterEgoMode: (node: GraphNode) => void;
  onSetCenter: (nodeId: number) => void;
  onOpenDedup: () => void;
  onDetachConcept: () => void;
  onRefreshGraph: () => void;
}

export function GraphInspector({
  graph,
  selectedNode,
  palette,
  viewMode,
  isLocalImport,
  detaching,
  detachUncertain,
  onClose,
  onSelectAndFocus,
  onEnterEgoMode,
  onSetCenter,
  onOpenDedup,
  onDetachConcept,
  onRefreshGraph,
}: GraphInspectorProps) {
  const selectedNeighbors = useMemo(() => {
    const seen = new Set<string>();
    const items: Array<{
      node: GraphNode;
      relType: string;
      sentiment?: EdgeSentiment;
      dir: "→" | "←" | "↻";
    }> = [];
    for (const edge of graph.edges) {
      if (edge.a !== selectedNode.id && edge.b !== selectedNode.id) continue;
      const otherId = edge.a === selectedNode.id ? edge.b : edge.a;
      const dir = edge.a === edge.b ? "↻" : edge.a === selectedNode.id ? "→" : "←";
      const key = `${otherId}:${edge.type}:${edge.sentiment || ""}`;
      if (!seen.has(key)) {
        const nb = graph.nodes[otherId];
        if (nb) {
          seen.add(key);
          items.push({ node: nb, relType: edge.type, sentiment: edge.sentiment, dir });
        }
      }
    }
    return items;
  }, [graph, selectedNode]);

  const attitudeProfile = useMemo(() => {
    const outgoing: Array<{ node: GraphNode; sentiment: EdgeSentiment }> = [];
    const incoming: Array<{ node: GraphNode; sentiment: EdgeSentiment }> = [];
    const seen = new Set<string>();
    for (const edge of graph.edges) {
      if (edge.type !== "HAS_ATTITUDE") continue;
      if (edge.a !== selectedNode.id && edge.b !== selectedNode.id) continue;
      const sentiment: EdgeSentiment = edge.sentiment || "中立";
      const isOutgoing = edge.a === selectedNode.id;
      const otherId = isOutgoing ? edge.b : edge.a;
      const nb = graph.nodes[otherId];
      if (!nb) continue;
      const key = `${isOutgoing ? "out" : "in"}:${otherId}:${sentiment}`;
      if (seen.has(key)) continue;
      seen.add(key);
      if (isOutgoing) outgoing.push({ node: nb, sentiment });
      else incoming.push({ node: nb, sentiment });
    }
    const order: Record<EdgeSentiment, number> = { 喜欢: 0, 讨厌: 1, 中立: 2, 不明确: 3 };
    outgoing.sort((a, b) => order[a.sentiment] - order[b.sentiment] || b.node.degree - a.node.degree);
    incoming.sort((a, b) => order[a.sentiment] - order[b.sentiment] || b.node.degree - a.node.degree);
    return { outgoing, incoming };
  }, [graph, selectedNode]);

  const equivalenceGroup = unwrapFieldValue(getNodeProperty(selectedNode, "equivalence_group")).trim();

  const equivalenceMembers = useMemo(() => {
    if (selectedNode.type !== "Concept" || !equivalenceGroup) return [];
    return graph.nodes.filter(
      (m) =>
        m.type === "Concept" &&
        unwrapFieldValue(getNodeProperty(m, "equivalence_group")).trim() === equivalenceGroup,
    );
  }, [graph.nodes, selectedNode.type, equivalenceGroup]);

  const selectedSummary = unwrapFieldValue(getNodeProperty(selectedNode, "summary")).trim();
  const selectedContent = unwrapFieldValue(getNodeProperty(selectedNode, "content")).trim();
  const bareConceptId = selectedNode.originalId.replace(/^Concept:/, "");
  const showEquivalenceCard =
    selectedNode.type === "Concept" &&
    Boolean(equivalenceGroup) &&
    !isLocalImport &&
    (equivalenceMembers.length > 1 || equivalenceGroup !== bareConceptId);

  const equivalenceGroupNeighbors = useMemo(() => {
    if (equivalenceMembers.length <= 1) return [];
    const siblingIds = new Set(
      equivalenceMembers.filter((m) => m.id !== selectedNode.id).map((m) => m.id),
    );
    const directIds = new Set(selectedNeighbors.map((item) => item.node.id));
    const seen = new Set<number>();
    const items: Array<{ node: GraphNode; viaName: string; relType: string }> = [];
    for (const edge of graph.edges) {
      const aSib = siblingIds.has(edge.a);
      const bSib = siblingIds.has(edge.b);
      if (!aSib && !bSib) continue;
      const otherId = aSib ? edge.b : edge.a;
      const sibId = aSib ? edge.a : edge.b;
      if (otherId === selectedNode.id || siblingIds.has(otherId) || directIds.has(otherId)) continue;
      if (seen.has(otherId)) continue;
      const nb = graph.nodes[otherId];
      const via = graph.nodes[sibId];
      if (!nb || !via) continue;
      seen.add(otherId);
      items.push({ node: nb, viaName: via.name, relType: edge.type });
    }
    items.sort((a, b) => b.node.degree - a.node.degree || a.node.id - b.node.id);
    return items.slice(0, 40);
  }, [equivalenceMembers, graph, selectedNode.id, selectedNeighbors]);

  const inspectorProps = useMemo(() => {
    const merged: Record<string, unknown> = {
      ...(selectedNode.raw.properties || {}),
      ...selectedNode.raw,
    };
    delete merged.properties;
    return Object.entries(merged)
      .filter(
        ([k, v]) =>
          k !== "raw" &&
          k !== "summary" &&
          k !== "content" &&
          v !== null &&
          v !== undefined &&
          String(v).trim() !== "",
      )
      .slice(0, 14);
  }, [selectedNode]);

  return (
    <aside
      data-testid="memory-graph-inspector"
      className="flex w-80 shrink-0 flex-col border-l border-border bg-surface max-md:absolute max-md:inset-x-0 max-md:bottom-0 max-md:top-auto max-md:max-h-[55%] max-md:w-full max-md:border-l-0 max-md:border-t"
    >
      <div className="flex items-start justify-between gap-2 border-b border-border-soft px-4 py-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className="h-2.5 w-2.5 shrink-0 rounded-full"
              style={{ backgroundColor: palette[selectedNode.type] }}
            />
            <Badge variant="default">{TYPE_DISPLAY_NAMES[selectedNode.type]}</Badge>
            {selectedNode.eventKind && (
              <Badge variant={selectedNode.eventKind === "milestone" ? "accent" : "default"}>
                {selectedNode.eventKind === "milestone" ? "✦ " : ""}
                {EVENT_SUBTYPE_NAMES[selectedNode.eventKind]}
              </Badge>
            )}
            {selectedNode.protagonist && <Badge variant="accent">中心人物</Badge>}
            {!selectedNode.protagonist && selectedNode.bridge && (
              <Badge variant="default">共同人物</Badge>
            )}
          </div>
          <h3 className="mt-1 truncate text-sm font-semibold text-fg" title={selectedNode.name}>
            {selectedNode.name}
          </h3>
          <p className="truncate font-mono text-[11px] text-fg-faint">{selectedNode.originalId}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭检查器"
          className="rounded-md p-1 text-fg-dim hover:bg-hover hover:text-fg"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4 text-xs">
        <div className="flex flex-wrap gap-1.5">
          <Button
            size="sm"
            variant="primary"
            onClick={() => onEnterEgoMode(selectedNode)}
          >
            <GitFork className="h-3.5 w-3.5" />
            {viewMode === "ego" ? "以此下钻局部" : "进入局部视图"}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => onSetCenter(selectedNode.id)}
          >
            <Focus className="h-3.5 w-3.5" />
            设为中心重排
          </Button>
          {selectedNode.type === "Concept" && (
            <Button size="sm" variant="secondary" onClick={onOpenDedup}>
              <GitMerge className="h-3.5 w-3.5" />
              概念去重
            </Button>
          )}
        </div>

        {(attitudeProfile.outgoing.length > 0 || attitudeProfile.incoming.length > 0) && (
          <div className="space-y-2 rounded-xl border border-border-soft bg-elevated/55 p-3">
            <h4 className="font-medium text-fg-dim">情感态度 / 好恶画像</h4>
            {attitudeProfile.outgoing.length > 0 && (
              <div className="space-y-1.5">
                <div className="text-[11px] text-fg-faint">TA 的主观态度</div>
                <div className="flex flex-wrap gap-1.5">
                  {attitudeProfile.outgoing.slice(0, 18).map(({ node, sentiment }) => (
                    <button
                      key={`out-${node.id}-${sentiment}`}
                      type="button"
                      onClick={() => onSelectAndFocus(node)}
                      className={cn(
                        "inline-flex cursor-pointer items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-opacity hover:opacity-85",
                        SENTIMENT_PILL_STYLE[sentiment],
                      )}
                    >
                      <span>
                        {SENTIMENT_ICON[sentiment]}
                        {sentiment}
                      </span>
                      <span className="font-medium text-fg">{node.name}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
            {attitudeProfile.incoming.length > 0 && (
              <div className="space-y-1.5">
                <div className="text-[11px] text-fg-faint">来自他人的态度</div>
                <div className="flex flex-wrap gap-1.5">
                  {attitudeProfile.incoming.slice(0, 18).map(({ node, sentiment }) => (
                    <button
                      key={`in-${node.id}-${sentiment}`}
                      type="button"
                      onClick={() => onSelectAndFocus(node)}
                      className={cn(
                        "inline-flex cursor-pointer items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-opacity hover:opacity-85",
                        SENTIMENT_PILL_STYLE[sentiment],
                      )}
                    >
                      <span className="font-medium text-fg">{node.name}</span>
                      <span>· {sentiment}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {selectedSummary && (
          <div>
            <h4 className="mb-1.5 font-medium text-fg-dim">摘要</h4>
            <p className="whitespace-pre-wrap break-words rounded-xl border border-border-soft bg-elevated/60 p-2.5 leading-relaxed text-fg">
              {selectedSummary}
            </p>
          </div>
        )}

        {selectedContent && (
          <details className="rounded-xl border border-border-soft bg-elevated/40 p-2.5">
            <summary className="cursor-pointer font-medium text-fg-dim">正文内容</summary>
            <p className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap break-words leading-relaxed text-fg-dim">
              {selectedContent}
            </p>
          </details>
        )}

        {showEquivalenceCard && (
          <div className="rounded-xl border border-warn/30 bg-warn-soft/40 p-3">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium text-fg">
                {equivalenceMembers.length > 1
                  ? `等价合并组 · ${equivalenceMembers.length} 个`
                  : "已独立等价组"}
              </span>
              <Badge variant="warn" className="font-mono">
                {equivalenceGroup}
              </Badge>
            </div>
            {equivalenceMembers.length > 1 ? (
              <>
                <p className="mt-1.5 text-[11px] text-fg-dim">
                  以下同义概念在检索与图谱视图中共享关联。若某项被误合并，可单独拆分。
                </p>
                <div className="mt-2 max-h-32 space-y-1 overflow-y-auto">
                  {equivalenceMembers.map((member) => (
                    <button
                      key={member.id}
                      type="button"
                      onClick={() => onSelectAndFocus(member)}
                      className={cn(
                        "flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg border border-border-soft px-2 py-1 text-left text-[11px] hover:bg-hover",
                        member.id === selectedNode.id ? "bg-elevated text-fg" : "bg-surface/60 text-fg-dim",
                      )}
                    >
                      <span className="truncate">{member.name}</span>
                      <span className="shrink-0 font-mono text-[10px] text-fg-faint">
                        {member.id === selectedNode.id ? "当前" : `${member.degree} 连接`}
                      </span>
                    </button>
                  ))}
                </div>
                <div className="mt-2 flex items-center gap-2">
                  <Button
                    size="sm"
                    variant="danger"
                    data-testid="memory-graph-detach"
                    disabled={detaching || detachUncertain}
                    onClick={onDetachConcept}
                  >
                    <Split className="h-3.5 w-3.5" />
                    {detaching ? "拆分中…" : "拆出等价组"}
                  </Button>
                  {detachUncertain && (
                    <Button size="sm" variant="ghost" onClick={onRefreshGraph}>
                      先刷新确认
                    </Button>
                  )}
                </div>
              </>
            ) : (
              <p className="mt-1.5 text-[11px] text-fg-dim">
                该概念当前属于独立等价组，暂无其他共享同义概念。
              </p>
            )}
          </div>
        )}

        {inspectorProps.length > 0 && (
          <div>
            <h4 className="mb-1.5 font-medium text-fg-dim">节点属性</h4>
            <dl className="space-y-1.5 rounded-xl border border-border-soft bg-elevated/60 p-2.5">
              {inspectorProps.map(([key, val]) => (
                <div key={key} className="flex items-start justify-between gap-3">
                  <dt className="shrink-0 font-mono text-[11px] text-fg-faint">{key}</dt>
                  <dd className="break-all text-right text-fg">{unwrapFieldValue(val)}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}

        {equivalenceGroupNeighbors.length > 0 && (
          <div>
            <h4 className="mb-1.5 font-medium text-fg-dim">
              同组其他成员关联 ({equivalenceGroupNeighbors.length})
            </h4>
            <div className="space-y-1">
              {equivalenceGroupNeighbors.map(({ node, viaName }) => (
                <button
                  key={`eq-nb-${node.id}`}
                  type="button"
                  onClick={() => onSelectAndFocus(node)}
                  className="flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg border border-border-soft bg-elevated/40 px-2.5 py-1.5 text-left hover:bg-hover"
                >
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: palette[node.type] }}
                    />
                    <span className="truncate text-fg">{node.name}</span>
                  </span>
                  <span className="shrink-0 font-mono text-[10px] text-fg-faint">
                    经 {viaName}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}

        <div>
          <h4 className="mb-1.5 font-medium text-fg-dim">
            相邻节点 ({selectedNeighbors.length})
          </h4>
          {selectedNeighbors.length === 0 ? (
            <p className="text-fg-faint">暂无直接相连关系。</p>
          ) : (
            <div className="space-y-1">
              {selectedNeighbors.slice(0, 60).map(({ node, relType, sentiment, dir }) => (
                <button
                  key={`${node.id}-${relType}-${sentiment || ""}`}
                  type="button"
                  onClick={() => onSelectAndFocus(node)}
                  className="flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg border border-border-soft bg-elevated/40 px-2.5 py-1.5 text-left hover:bg-hover"
                >
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ backgroundColor: palette[node.type] }}
                    />
                    <span className="truncate text-fg">{node.name}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-1 font-mono text-[10px] text-fg-faint">
                    {sentiment ? (
                      <span
                        className={cn(
                          "rounded px-1 py-0.5 text-[9px] font-medium",
                          SENTIMENT_PILL_STYLE[sentiment],
                        )}
                      >
                        {dir} {SENTIMENT_ICON[sentiment]}
                        {sentiment}
                      </span>
                    ) : (
                      <span>
                        {dir} {REL_TYPE_LABELS[relType] || relType}
                      </span>
                    )}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}
