import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ChevronRight,
  Layers,
  Maximize2,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { apiFetch, ApiError } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { toast } from "../../components/ui/Toast";
import { useThemeStore } from "../../stores/themeStore";
import { useLayoutStore } from "../workbench/layoutStore";
import { GraphCanvasController, type GraphRenderMetrics } from "./canvasRenderer";
import { buildEgoSubgraph } from "./egoLayout";
import { analyzeGraph } from "./graphEngine";
import {
  GRAPH_IMPORT_LIMITS,
  getNodeProperty,
  normalizeGraphDocument,
  parseGraphJsonText,
  serializeGraphDocument,
  unwrapFieldValue,
} from "./graphIo";
import { GraphInspector } from "./GraphInspector";
import { GraphToolbar } from "./GraphToolbar";
import { layoutRadialGraph } from "./radialLayout";
import {
  DARK_GRAPH_PALETTE,
  EVENT_SUBTYPE_NAMES,
  LIGHT_GRAPH_PALETTE,
  TYPE_DISPLAY_NAMES,
  type ComputedGraphResult,
  type EventSubType,
  type EventSubTypeFilter,
  type GraphNode,
  type GraphNodeType,
  type GraphViewMode,
  type LeafFoldMode,
  type RawGraphDocument,
  type RawGraphEdge,
  type RawGraphNode,
} from "./types";

const DEFAULT_VISIBLE_TYPES: Record<GraphNodeType, boolean> = {
  Person: true,
  Concept: true,
  Event: true,
  Location: true,
  Other: true,
};

const DEFAULT_EVENT_KINDS: EventSubTypeFilter = {
  milestone: true,
  travel: true,
  study: true,
  interaction: true,
  other: true,
};

export function MemoryGraphPanel() {
  const theme = useThemeStore((s) => s.resolved);
  const openPanel = useLayoutStore((s) => s.openPanel);
  const focusConceptId = useLayoutStore((s) => s.graphFocusConceptId);
  const clearFocusConcept = useLayoutStore((s) => s.clearGraphFocusConcept);

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const controllerRef = useRef<GraphCanvasController | null>(null);
  const searchInputRef = useRef<HTMLInputElement | null>(null);
  const searchWrapRef = useRef<HTMLDivElement | null>(null);
  const settingsWrapRef = useRef<HTMLDivElement | null>(null);
  const workerRef = useRef<Worker | null>(null);
  const hasGraphRef = useRef(false);
  const dragStateRef = useRef<{ active: boolean; moved: boolean; lastX: number; lastY: number }>({
    active: false,
    moved: false,
    lastX: 0,
    lastY: 0,
  });

  const [rawDoc, setRawDoc] = useState<{ nodes: RawGraphNode[]; relationships: RawGraphEdge[] } | null>(
    null,
  );
  const [graph, setGraph] = useState<ComputedGraphResult | null>(null);
  const [sourceLabel, setSourceLabel] = useState("记忆库实时视图");
  const [loading, setLoading] = useState(true);
  const [computing, setComputing] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const [viewMode, setViewMode] = useState<GraphViewMode>("global");
  const [egoTrail, setEgoTrail] = useState<number[]>([]);
  const [egoHops, setEgoHops] = useState<1 | 2>(1);

  const [searchQuery, setSearchQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [visibleTypes, setVisibleTypes] = useState<Record<GraphNodeType, boolean>>(DEFAULT_VISIBLE_TYPES);
  const [visibleEventKinds, setVisibleEventKinds] = useState<EventSubTypeFilter>(DEFAULT_EVENT_KINDS);
  const [highlightAttitudes, setHighlightAttitudes] = useState(false);
  const [mergeEquivalenceGroups, setMergeEquivalenceGroups] = useState(true);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [hoveredId, setHoveredId] = useState<number | null>(null);
  const [hoverPos, setHoverPos] = useState<{ x: number; y: number } | null>(null);
  const [showViewSettings, setShowViewSettings] = useState(false);

  const [correctHubs, setCorrectHubs] = useState(true);
  const [centerNodeId, setCenterNodeId] = useState<number | null>(null);
  const [showClusters, setShowClusters] = useState(true);
  const [showCrossEdges, setShowCrossEdges] = useState(true);
  const [showLabels, setShowLabels] = useState(true);
  const [showEdgeLabels, setShowEdgeLabels] = useState(true);
  const [smoothMotion, setSmoothMotion] = useState(true);
  const [foldLeaves, setFoldLeaves] = useState<LeafFoldMode>("auto");
  const [minDegree, setMinDegree] = useState(1);
  const [edgeOpacity, setEdgeOpacity] = useState(0.36);
  const [nodeSize, setNodeSize] = useState(0.75);
  const [edgeBudget, setEdgeBudget] = useState(2000);
  const [renderMetrics, setRenderMetrics] = useState<GraphRenderMetrics>({
    scale: 1,
    drawnEdges: 0,
    totalEdges: 0,
    visibleNodes: 0,
    foldedNodes: 0,
  });

  const [detaching, setDetaching] = useState(false);
  const [detachUncertain, setDetachUncertain] = useState(false);
  const [isLocalImport, setIsLocalImport] = useState(false);

  const cancelWorker = useCallback(() => {
    if (workerRef.current) {
      workerRef.current.terminate();
      workerRef.current = null;
    }
    setComputing(false);
  }, []);

  const egoRootId = egoTrail.length > 0 ? egoTrail[egoTrail.length - 1]! : null;
  const egoSubgraph = useMemo(() => {
    if (viewMode !== "ego" || !graph || egoRootId === null) return null;
    return buildEgoSubgraph(graph, egoRootId, egoHops, visibleTypes);
  }, [viewMode, graph, egoRootId, egoHops, visibleTypes]);

  const runLayout = useCallback(
    async (
      nodesInput: GraphNode[],
      edgesInput: ComputedGraphResult["edges"],
      opts: { correctHubs: boolean; centerId: number | null; fitAfter?: boolean },
    ) => {
      cancelWorker();
      setComputing(true);

      const finish = (result: ComputedGraphResult) => {
        hasGraphRef.current = true;
        setGraph(result);
        setComputing(false);
        controllerRef.current?.setData(result, opts.fitAfter ?? true);
      };

      if (typeof Worker !== "undefined") {
        try {
          const worker = new Worker(new URL("./graph.worker.ts", import.meta.url), {
            type: "module",
          });
          workerRef.current = worker;
          worker.onmessage = (ev: MessageEvent<ComputedGraphResult | { error?: string }>) => {
            if (workerRef.current !== worker) return;
            worker.terminate();
            workerRef.current = null;
            if ("error" in ev.data && ev.data.error) {
              setErrorMsg(ev.data.error);
              setComputing(false);
              return;
            }
            finish(ev.data as ComputedGraphResult);
          };
          worker.onerror = () => {
            if (workerRef.current !== worker) return;
            worker.terminate();
            workerRef.current = null;
            const analysis = analyzeGraph(nodesInput, edgesInput, opts.correctHubs);
            const clusters = layoutRadialGraph(nodesInput, edgesInput, analysis, {
              centerId: opts.centerId,
            });
            finish({ nodes: nodesInput, edges: edgesInput, analysis, clusters });
          };
          worker.postMessage({
            nodes: nodesInput,
            edges: edgesInput,
            cachedAnalysis: null,
            correctHubs: opts.correctHubs,
            centerId: opts.centerId,
          });
          return;
        } catch {
          // Fallback synchronously below when module workers are unavailable (e.g. jsdom).
        }
      }

      const analysis = analyzeGraph(nodesInput, edgesInput, opts.correctHubs);
      const clusters = layoutRadialGraph(nodesInput, edgesInput, analysis, {
        centerId: opts.centerId,
      });
      finish({ nodes: nodesInput, edges: edgesInput, analysis, clusters });
    },
    [cancelWorker],
  );

  const loadFromDatabase = useCallback(async () => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const payload = await apiFetch<
        RawGraphDocument | { ok?: boolean; data?: RawGraphDocument; error?: string | null }
      >("/api/memory/graph?labels=Event,Person,Concept,Location");
      if (
        payload &&
        typeof payload === "object" &&
        "ok" in payload &&
        payload.ok === false
      ) {
        throw new Error(("error" in payload && payload.error) || "记忆图谱暂不可用");
      }
      const graphDoc =
        payload && typeof payload === "object" && "data" in payload && payload.data
          ? payload.data
          : (payload as RawGraphDocument);
      const normalized = normalizeGraphDocument(graphDoc);
      setRawDoc(normalized.document);
      setSourceLabel("记忆库实时视图");
      setIsLocalImport(false);
      setViewMode("global");
      setEgoTrail([]);
      setVisibleEventKinds(DEFAULT_EVENT_KINDS);
      setHighlightAttitudes(false);
      setSelectedId(null);
      setHoveredId(null);
      setHoverPos(null);
      setCenterNodeId(null);
      setDetachUncertain(false);
      await runLayout(normalized.nodes, normalized.edges, {
        correctHubs,
        centerId: null,
        fitAfter: true,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : "加载记忆图谱失败";
      if (hasGraphRef.current) {
        toast(message, "error");
      } else {
        setErrorMsg(message);
      }
    } finally {
      setLoading(false);
    }
  }, [correctHubs, runLayout]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctrl = new GraphCanvasController(
      canvas,
      {
        theme,
        viewMode,
        egoSubgraph,
        visibleTypes,
        visibleEventKinds,
        highlightAttitudes,
        mergeEquivalenceGroups,
        searchQuery,
        showClusters,
        showCrossEdges,
        showLabels,
        showEdgeLabels,
        smoothMotion,
        foldLeaves,
        minDegree,
        edgeOpacity,
        nodeSize,
        edgeBudget,
        selectedId,
        hoveredId,
      },
      (metrics) => setRenderMetrics(metrics),
    );
    controllerRef.current = ctrl;

    let ro: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => ctrl.resize());
      ro.observe(canvas);
    }
    const onResize = () => ctrl.resize();
    window.addEventListener("resize", onResize);

    return () => {
      window.removeEventListener("resize", onResize);
      ro?.disconnect();
      ctrl.dispose();
      controllerRef.current = null;
    };
    // Intentionally initialize once and sync reactive options in separate effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    controllerRef.current?.setOptions({
      theme,
      viewMode,
      egoSubgraph,
      visibleTypes,
      visibleEventKinds,
      highlightAttitudes,
      mergeEquivalenceGroups,
      searchQuery,
      showClusters,
      showCrossEdges,
      showLabels,
      showEdgeLabels,
      smoothMotion,
      foldLeaves,
      minDegree,
      edgeOpacity,
      nodeSize,
      edgeBudget,
      selectedId,
      hoveredId,
    });
  }, [
    theme,
    viewMode,
    egoSubgraph,
    visibleTypes,
    visibleEventKinds,
    highlightAttitudes,
    mergeEquivalenceGroups,
    searchQuery,
    showClusters,
    showCrossEdges,
    showLabels,
    showEdgeLabels,
    smoothMotion,
    foldLeaves,
    minDegree,
    edgeOpacity,
    nodeSize,
    edgeBudget,
    selectedId,
    hoveredId,
  ]);

  useEffect(() => {
    void loadFromDatabase();
    return () => cancelWorker();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleReLayout = useCallback(
    async (nextCorrectHubs: boolean, nextCenterId: number | null) => {
      if (!rawDoc) return;
      const normalized = normalizeGraphDocument(rawDoc);
      setViewMode("global");
      setEgoTrail([]);
      setCenterNodeId(nextCenterId);
      setCorrectHubs(nextCorrectHubs);
      await runLayout(normalized.nodes, normalized.edges, {
        correctHubs: nextCorrectHubs,
        centerId: nextCenterId,
        fitAfter: true,
      });
    },
    [rawDoc, runLayout],
  );

  useEffect(() => {
    if (!focusConceptId || !graph) return;
    const match = graph.nodes.find(
      (n) =>
        n.originalId === focusConceptId ||
        String(getNodeProperty(n, "id") ?? "") === focusConceptId ||
        n.name === focusConceptId,
    );
    if (match) {
      setViewMode("global");
      setSelectedId(match.id);
      setDetachUncertain(false);
      controllerRef.current?.focusNode(match.id, 1.65);
      clearFocusConcept();
    }
  }, [focusConceptId, graph, clearFocusConcept]);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const target = ev.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT");
      if (ev.key === "/" && !typing) {
        ev.preventDefault();
        searchInputRef.current?.focus();
      } else if (ev.key === "Escape") {
        if (searchOpen) setSearchOpen(false);
        else if (showViewSettings) setShowViewSettings(false);
        else if (searchQuery) setSearchQuery("");
        else if (viewMode === "ego") {
          setViewMode("global");
          setEgoTrail([]);
        } else if (selectedId !== null) setSelectedId(null);
      }
    };
    const onDocPointerDown = (ev: MouseEvent) => {
      const node = ev.target as Node | null;
      if (node && searchWrapRef.current && !searchWrapRef.current.contains(node)) {
        setSearchOpen(false);
      }
      if (node && settingsWrapRef.current && !settingsWrapRef.current.contains(node)) {
        setShowViewSettings(false);
      }
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDocPointerDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDocPointerDown);
    };
  }, [searchQuery, searchOpen, selectedId, showViewSettings, viewMode]);

  const typeCounts = useMemo(() => {
    const counts: Record<GraphNodeType, number> = {
      Person: 0,
      Concept: 0,
      Event: 0,
      Location: 0,
      Other: 0,
    };
    for (const n of graph?.nodes || []) {
      counts[n.type] += 1;
    }
    return counts;
  }, [graph]);

  const eventKindCounts = useMemo(() => {
    const counts: Record<EventSubType | "other", number> = {
      milestone: 0,
      travel: 0,
      study: 0,
      interaction: 0,
      other: 0,
    };
    for (const n of graph?.nodes || []) {
      if (n.type === "Event") {
        counts[n.eventKind ?? "other"] += 1;
      }
    }
    return counts;
  }, [graph]);

  const attitudeEdgeCount = useMemo(
    () => (graph?.edges || []).filter((e) => e.type === "HAS_ATTITUDE").length,
    [graph],
  );

  const onlyMilestonesActive = useMemo(
    () =>
      visibleEventKinds.milestone &&
      !visibleEventKinds.travel &&
      !visibleEventKinds.study &&
      !visibleEventKinds.interaction &&
      !visibleEventKinds.other,
    [visibleEventKinds],
  );

  const personNodes = useMemo(
    () =>
      (graph?.nodes || [])
        .filter((n) => n.type === "Person")
        .sort((a, b) => b.degree - a.degree || a.id - b.id),
    [graph],
  );

  const islandCount = useMemo(() => {
    if (!graph?.analysis.clear) return 0;
    return graph.clusters.filter((c) => !c.loose && c.size >= 4).length;
  }, [graph]);

  const searchMatches = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q || !graph) return [];
    const scored: Array<{ node: GraphNode; rank: number }> = [];
    for (const n of graph.nodes) {
      if (!visibleTypes[n.type]) continue;
      const lowerName = n.name.toLowerCase();
      if (lowerName === q) {
        scored.push({ node: n, rank: 0 });
        continue;
      }
      if (lowerName.includes(q)) {
        scored.push({ node: n, rank: lowerName.startsWith(q) ? 1 : 2 });
        continue;
      }
      const hay = `${n.originalId} ${unwrapFieldValue(getNodeProperty(n, "summary"))} ${unwrapFieldValue(getNodeProperty(n, "content"))}`.toLowerCase();
      if (hay.includes(q)) {
        scored.push({ node: n, rank: 3 });
      }
    }
    scored.sort((a, b) => a.rank - b.rank || b.node.degree - a.node.degree || a.node.id - b.node.id);
    return scored.slice(0, 20).map((s) => s.node);
  }, [graph, searchQuery, visibleTypes]);

  const selectedNode = useMemo(
    () => (selectedId !== null && graph ? graph.nodes[selectedId] ?? null : null),
    [graph, selectedId],
  );

  const hoveredNode = useMemo(
    () => (hoveredId !== null && graph ? graph.nodes[hoveredId] ?? null : null),
    [graph, hoveredId],
  );

  const selectAndFocusNode = useCallback(
    (node: GraphNode) => {
      setSelectedId(node.id);
      setDetachUncertain(false);
      setSearchOpen(false);
      if (node.type === "Event" && !visibleEventKinds[node.eventKind ?? "other"]) {
        setVisibleEventKinds((prev) => ({
          ...prev,
          [node.eventKind ?? "other"]: true,
        }));
      }
      controllerRef.current?.focusNode(node.id, 1.65);
    },
    [visibleEventKinds],
  );

  const enterEgoMode = useCallback((node: GraphNode) => {
    setViewMode("ego");
    setSelectedId(node.id);
    setDetachUncertain(false);
    setSearchOpen(false);
    setEgoTrail((prev) => {
      if (prev.length > 0 && prev[prev.length - 1] === node.id) return prev;
      const existingIdx = prev.indexOf(node.id);
      if (existingIdx >= 0) return prev.slice(0, existingIdx + 1);
      return [...prev, node.id].slice(-6);
    });
  }, []);

  const handleImportFile = async (file: File | undefined) => {
    if (!file) return;
    if (/\.dump$/i.test(file.name)) {
      toast(".dump 不能直接导入，请先导出 JSON 文件。", "error");
      return;
    }
    if (file.size > GRAPH_IMPORT_LIMITS.bytes) {
      toast("图谱 JSON 超过 20 MB 上限。", "error");
      return;
    }
    try {
      const text = await file.text();
      const parsed = parseGraphJsonText(text, file.name);
      setRawDoc(parsed.document);
      setSourceLabel(file.name);
      setIsLocalImport(true);
      setViewMode("global");
      setEgoTrail([]);
      setVisibleEventKinds(DEFAULT_EVENT_KINDS);
      setHighlightAttitudes(false);
      setSelectedId(null);
      setHoveredId(null);
      setHoverPos(null);
      setCenterNodeId(null);
      setDetachUncertain(false);
      await runLayout(parsed.nodes, parsed.edges, {
        correctHubs,
        centerId: null,
        fitAfter: true,
      });
      toast(`已导入 ${parsed.nodes.length} 个节点`, "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "导入图谱失败", "error");
    }
  };

  const handleExportJson = () => {
    if (!rawDoc) return;
    const json = serializeGraphDocument(rawDoc);
    const blob = new Blob([json], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `tentacle-memory-graph-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleDetachConcept = async () => {
    if (!selectedNode || selectedNode.type !== "Concept" || isLocalImport) return;
    if (!window.confirm(`确定将概念「${selectedNode.name}」从等价合并组拆出为独立概念？`)) return;
    setDetaching(true);
    setDetachUncertain(false);
    try {
      const res = await apiFetch<{ ok?: boolean; group_id?: string }>("/api/memory/concepts/detach", {
        method: "POST",
        body: JSON.stringify({ concept_id: selectedNode.originalId }),
      });
      setRawDoc((prev) =>
        prev
          ? {
              ...prev,
              nodes: prev.nodes.map((n) =>
                n.id === selectedNode.originalId
                  ? {
                      ...n,
                      equivalence_group: res.group_id || undefined,
                      properties: n.properties
                        ? { ...n.properties, equivalence_group: res.group_id || undefined }
                        : n.properties,
                    }
                  : n,
              ),
            }
          : prev,
      );
      setGraph((prev) =>
        prev
          ? {
              ...prev,
              nodes: prev.nodes.map((n) =>
                n.id === selectedNode.id
                  ? {
                      ...n,
                      equivalenceGroup: res.group_id || undefined,
                      eqRepId: null,
                      eqGroupSize: 1,
                      raw: {
                        ...n.raw,
                        equivalence_group: res.group_id || undefined,
                        properties: n.raw.properties
                          ? { ...n.raw.properties, equivalence_group: res.group_id || undefined }
                          : n.raw.properties,
                      },
                    }
                  : n,
              ),
            }
          : prev,
      );
      toast(`已将该概念从合并组 ${res.group_id || ""} 独立拆出`, "ok");
      await loadFromDatabase();
    } catch (err) {
      if (err instanceof ApiError && err.status >= 500) {
        setDetachUncertain(true);
        toast("服务端拆分结果未确认，请先刷新确认图谱状态再重试。", "error");
      } else if (!(err instanceof ApiError)) {
        setDetachUncertain(true);
        toast("网络连接中断，拆分状态未知，请刷新图谱确认。", "error");
      } else {
        toast(err.message, "error");
      }
    } finally {
      setDetaching(false);
    }
  };

  const palette = theme === "light" ? LIGHT_GRAPH_PALETTE : DARK_GRAPH_PALETTE;

  return (
    <div data-testid="memory-graph-panel" className="flex h-full min-h-0 flex-col bg-bg">
      <h2 className="sr-only">记忆图谱</h2>

      <GraphToolbar
        graph={graph}
        palette={palette}
        loading={loading}
        hasRawDoc={Boolean(rawDoc)}
        searchInputRef={searchInputRef}
        searchWrapRef={searchWrapRef}
        settingsWrapRef={settingsWrapRef}
        searchQuery={searchQuery}
        onSearchQueryChange={setSearchQuery}
        searchOpen={searchOpen}
        onSearchOpenChange={setSearchOpen}
        searchMatches={searchMatches}
        onSelectNode={selectAndFocusNode}
        visibleTypes={visibleTypes}
        onToggleType={(t) => setVisibleTypes((prev) => ({ ...prev, [t]: !prev[t] }))}
        typeCounts={typeCounts}
        visibleEventKinds={visibleEventKinds}
        eventKindCounts={eventKindCounts}
        onToggleEventKind={(kind) =>
          setVisibleEventKinds((prev) => ({ ...prev, [kind]: !prev[kind] }))
        }
        onResetEventKinds={() => setVisibleEventKinds(DEFAULT_EVENT_KINDS)}
        showViewSettings={showViewSettings}
        onToggleViewSettings={() => setShowViewSettings((v) => !v)}
        correctHubs={correctHubs}
        centerNodeId={centerNodeId}
        personNodes={personNodes}
        onReLayout={(h, cid) => void handleReLayout(h, cid)}
        showClusters={showClusters}
        onShowClustersChange={setShowClusters}
        showCrossEdges={showCrossEdges}
        onShowCrossEdgesChange={setShowCrossEdges}
        showLabels={showLabels}
        onShowLabelsChange={setShowLabels}
        showEdgeLabels={showEdgeLabels}
        onShowEdgeLabelsChange={setShowEdgeLabels}
        smoothMotion={smoothMotion}
        onSmoothMotionChange={setSmoothMotion}
        foldLeaves={foldLeaves}
        onFoldLeavesChange={setFoldLeaves}
        mergeEquivalenceGroups={mergeEquivalenceGroups}
        onMergeEquivalenceGroupsChange={setMergeEquivalenceGroups}
        equivalenceGroupCount={graph?.analysis.equivalenceGroupCount || 0}
        minDegree={minDegree}
        onMinDegreeChange={setMinDegree}
        edgeOpacity={edgeOpacity}
        onEdgeOpacityChange={setEdgeOpacity}
        nodeSize={nodeSize}
        onNodeSizeChange={setNodeSize}
        edgeBudget={edgeBudget}
        onEdgeBudgetChange={setEdgeBudget}
        islandCount={islandCount}
        onImportFile={(file) => void handleImportFile(file)}
        onExportJson={handleExportJson}
        onRefresh={() => void loadFromDatabase()}
      />

      {/* Main Canvas + Inspector */}
      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        <div ref={stageRef} className="relative min-w-0 flex-1">
          <canvas
            ref={canvasRef}
            tabIndex={0}
            data-testid="memory-graph-canvas"
            aria-label="记忆关系图画布，拖动平移、滚轮缩放，双击节点进入局部关系网"
            className="h-full w-full cursor-grab outline-none active:cursor-grabbing"
            onPointerDown={(e) => {
              dragStateRef.current = {
                active: true,
                moved: false,
                lastX: e.clientX,
                lastY: e.clientY,
              };
            }}
            onPointerMove={(e) => {
              const drag = dragStateRef.current;
              const rect = e.currentTarget.getBoundingClientRect();
              const sx = e.clientX - rect.left;
              const sy = e.clientY - rect.top;
              if (drag.active) {
                const dx = e.clientX - drag.lastX;
                const dy = e.clientY - drag.lastY;
                if (Math.abs(dx) > 2 || Math.abs(dy) > 2) {
                  drag.moved = true;
                  setHoveredId(null);
                  setHoverPos(null);
                }
                drag.lastX = e.clientX;
                drag.lastY = e.clientY;
                controllerRef.current?.panBy(dx, dy);
                return;
              }
              const hit = controllerRef.current?.hitTest(sx, sy);
              setHoveredId(hit ? hit.id : null);
              setHoverPos(hit ? { x: sx, y: sy } : null);
            }}
            onPointerUp={(e) => {
              const drag = dragStateRef.current;
              drag.active = false;
              if (!drag.moved) {
                const rect = e.currentTarget.getBoundingClientRect();
                const hit = controllerRef.current?.hitTest(
                  e.clientX - rect.left,
                  e.clientY - rect.top,
                );
                setSelectedId(hit ? hit.id : null);
                setDetachUncertain(false);
              }
            }}
            onDoubleClick={(e) => {
              const rect = e.currentTarget.getBoundingClientRect();
              const hit = controllerRef.current?.hitTest(
                e.clientX - rect.left,
                e.clientY - rect.top,
              );
              if (hit) {
                enterEgoMode(hit);
              }
            }}
            onPointerLeave={() => {
              dragStateRef.current.active = false;
              setHoveredId(null);
              setHoverPos(null);
            }}
            onKeyDown={(e) => {
              if (!graph || graph.nodes.length === 0) return;
              if (e.key === "Enter" && selectedNode) {
                e.preventDefault();
                enterEgoMode(selectedNode);
                return;
              }
              if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
              e.preventDefault();
              const fromId = selectedId ?? graph.analysis.centerId ?? 0;
              const neighbors = [...new Set(graph.analysis.adj[fromId] || [])].filter(
                (id) => visibleTypes[graph.nodes[id]?.type || "Other"],
              );
              const nextId =
                neighbors.length > 0
                  ? neighbors[e.key === "ArrowLeft" || e.key === "ArrowUp" ? neighbors.length - 1 : 0]!
                  : (fromId + 1) % graph.nodes.length;
              const targetNode = graph.nodes[nextId];
              if (targetNode) selectAndFocusNode(targetNode);
            }}
            onWheel={(e) => {
              e.preventDefault();
              const rect = e.currentTarget.getBoundingClientRect();
              const factor = e.deltaY < 0 ? 1.12 : 0.89;
              controllerRef.current?.zoomAt(factor, e.clientX - rect.left, e.clientY - rect.top);
            }}
          />

          {/* Hover Tooltip */}
          {hoveredNode && hoverPos && (
            <div
              className="pointer-events-none absolute z-20 max-w-64 rounded-xl border border-border/80 bg-surface/92 px-3 py-2 text-xs shadow-lg backdrop-blur-md"
              style={{
                left: Math.min(
                  Math.max(12, hoverPos.x + 14),
                  Math.max(12, (stageRef.current?.clientWidth ?? 400) - 240),
                ),
                top: Math.min(
                  Math.max(44, hoverPos.y + 14),
                  Math.max(44, (stageRef.current?.clientHeight ?? 300) - 72),
                ),
              }}
            >
              <div className="flex items-center gap-1.5 font-medium text-fg">
                <span
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{ backgroundColor: palette[hoveredNode.type] }}
                />
                <span className="truncate">{hoveredNode.name}</span>
              </div>
              <div className="mt-0.5 text-[10px] text-fg-dim">
                {hoveredNode.eventKind
                  ? `事件 · ${hoveredNode.eventKind === "milestone" ? "✦ " : ""}${EVENT_SUBTYPE_NAMES[hoveredNode.eventKind]}`
                  : TYPE_DISPLAY_NAMES[hoveredNode.type]}{" "}
                · {hoveredNode.degree} 条连接
                {(hoveredNode.foldedLeafCount || 0) > 0 && viewMode === "global"
                  ? ` · 含 ${hoveredNode.foldedLeafCount} 个叶概念`
                  : ""}
                {(hoveredNode.eqGroupSize || 1) >= 2 && viewMode === "global"
                  ? ` · 等价组含 ${hoveredNode.eqGroupSize} 个词条`
                  : ""}
              </div>
              <div className="mt-0.5 text-[9px] text-fg-faint">双击进入局部关系网</div>
            </div>
          )}

          {/* Top-left floating HUD (Global vs Ego Breadcrumb bar) */}
          <div className="pointer-events-none absolute left-3 right-3 top-3 flex flex-wrap items-center gap-1.5">
            {viewMode === "ego" && egoSubgraph && graph ? (
              <div className="pointer-events-auto flex flex-wrap items-center gap-1.5 rounded-xl border border-border bg-surface/92 px-2.5 py-1.5 text-xs shadow-md backdrop-blur-md">
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 px-2 text-xs"
                  onClick={() => {
                    setViewMode("global");
                    setEgoTrail([]);
                  }}
                >
                  <ArrowLeft className="h-3.5 w-3.5" />
                  返回星群全局
                </Button>
                <span className="h-3.5 w-px bg-border" />
                <div className="flex flex-wrap items-center gap-1">
                  {egoTrail.map((id, idx) => {
                    const item = graph.nodes[id];
                    if (!item) return null;
                    const isLast = idx === egoTrail.length - 1;
                    return (
                      <span key={`${id}-${idx}`} className="inline-flex items-center gap-1">
                        {idx > 0 && <ChevronRight className="h-3 w-3 text-fg-faint" />}
                        <button
                          type="button"
                          onClick={() => {
                            setEgoTrail((prev) => prev.slice(0, idx + 1));
                            setSelectedId(id);
                          }}
                          className={cn(
                            "cursor-pointer rounded-md px-1.5 py-0.5 text-xs transition-colors",
                            isLast
                              ? "bg-accent-soft font-medium text-fg"
                              : "text-fg-dim hover:bg-hover hover:text-fg",
                          )}
                        >
                          {item.name}
                        </button>
                      </span>
                    );
                  })}
                </div>
                <span className="h-3.5 w-px bg-border" />
                <div className="flex items-center gap-1 rounded-lg bg-elevated/80 p-0.5">
                  {([1, 2] as const).map((h) => (
                    <button
                      key={h}
                      type="button"
                      onClick={() => setEgoHops(h)}
                      className={cn(
                        "cursor-pointer rounded-md px-2 py-0.5 text-[11px] transition-colors",
                        egoHops === h ? "bg-surface font-medium text-fg shadow-xs" : "text-fg-dim hover:text-fg",
                      )}
                    >
                      {h} 跳
                    </button>
                  ))}
                </div>
                {egoSubgraph.truncated && (
                  <span className="text-[10px] text-fg-faint">
                    显示核心 {egoSubgraph.nodes.length - 1} / {egoSubgraph.totalReachable} 个关联
                  </span>
                )}
              </div>
            ) : (
              <>
                <Badge variant="default" data-testid="memory-graph-source" className="bg-surface/85 backdrop-blur-xs">
                  {sourceLabel}
                </Badge>
                {graph && (
                  <Badge variant="default" data-testid="memory-graph-count" className="bg-surface/85 font-mono backdrop-blur-xs">
                    {graph.nodes.length} 节点 · {graph.edges.length} 边 · Q=
                    {graph.analysis.q.toFixed(2)}
                  </Badge>
                )}
                {viewMode === "global" && graph && graph.analysis.clear && islandCount > 0 && (
                  <Badge variant="default" className="bg-surface/85 backdrop-blur-xs">
                    {islandCount} 个语义群落
                  </Badge>
                )}
                {viewMode === "global" && eventKindCounts.milestone > 0 && (
                  <button
                    type="button"
                    data-testid="memory-graph-milestone-toggle"
                    onClick={() =>
                      setVisibleEventKinds(
                        onlyMilestonesActive
                          ? DEFAULT_EVENT_KINDS
                          : {
                              milestone: true,
                              travel: false,
                              study: false,
                              interaction: false,
                              other: false,
                            },
                      )
                    }
                    className={cn(
                      "pointer-events-auto inline-flex cursor-pointer items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] backdrop-blur-xs transition-colors",
                      onlyMilestonesActive
                        ? "border-warn/40 bg-warn-soft font-medium text-warn"
                        : "border-border bg-surface/85 text-fg-dim hover:bg-hover hover:text-fg",
                    )}
                    title="只显示人生里程碑及其关联人物、地点与核心概念"
                  >
                    <span>✦</span>
                    <span>
                      {onlyMilestonesActive
                        ? `仅里程碑 (${eventKindCounts.milestone})`
                        : `里程碑 ${eventKindCounts.milestone}`}
                    </span>
                  </button>
                )}
                {viewMode === "global" && attitudeEdgeCount > 0 && (
                  <button
                    type="button"
                    data-testid="memory-graph-attitude-toggle"
                    onClick={() => setHighlightAttitudes((v) => !v)}
                    className={cn(
                      "pointer-events-auto inline-flex cursor-pointer items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] backdrop-blur-xs transition-colors",
                      highlightAttitudes
                        ? "border-accent/40 bg-accent-soft font-medium text-accent"
                        : "border-border bg-surface/85 text-fg-dim hover:bg-hover hover:text-fg",
                    )}
                    title="高亮人物对概念/事物的好恶情感连线"
                  >
                    <span>♡</span>
                    <span>态度关系 {attitudeEdgeCount}</span>
                  </button>
                )}
                {viewMode === "global" && (graph?.analysis.equivalenceGroupCount || 0) > 0 && (
                  <button
                    type="button"
                    data-testid="memory-graph-eq-toggle"
                    onClick={() => setMergeEquivalenceGroups((v) => !v)}
                    className={cn(
                      "pointer-events-auto inline-flex cursor-pointer items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] backdrop-blur-xs transition-colors",
                      mergeEquivalenceGroups
                        ? "border-warn/40 bg-warn-soft font-medium text-warn"
                        : "border-border bg-surface/85 text-fg-dim hover:bg-hover hover:text-fg",
                    )}
                    title="合并显示已归入同义等价组的概念节点，点击切换展开全部成员"
                  >
                    <span>≡</span>
                    <span>
                      {mergeEquivalenceGroups
                        ? `已合并等价组 ${graph!.analysis.equivalenceGroupCount}`
                        : `等价组 ${graph!.analysis.equivalenceGroupCount}（点击合并）`}
                    </span>
                  </button>
                )}
                {viewMode === "global" && graph && renderMetrics.foldedNodes > 0 && (
                  <button
                    type="button"
                    onClick={() => setFoldLeaves("off")}
                    className="pointer-events-auto inline-flex cursor-pointer items-center gap-1 rounded-full border border-border bg-surface/85 px-2.5 py-0.5 text-[11px] text-fg-dim backdrop-blur-xs transition-colors hover:bg-hover hover:text-fg"
                    title="缩略视图自动折叠了只出现一次的叶概念，点击立即全部展开"
                  >
                    <Layers className="h-3 w-3" />
                    <span>已折叠 {renderMetrics.foldedNodes} 个单次叶概念（点击展开）</span>
                  </button>
                )}
                {viewMode === "global" && graph && foldLeaves === "off" && (graph.analysis.singleLeafCount || 0) > 0 && (
                  <button
                    type="button"
                    onClick={() => setFoldLeaves("auto")}
                    className="pointer-events-auto inline-flex cursor-pointer items-center gap-1 rounded-full border border-border bg-surface/85 px-2.5 py-0.5 text-[11px] text-fg-dim backdrop-blur-xs transition-colors hover:bg-hover hover:text-fg"
                  >
                    <Layers className="h-3 w-3" />
                    <span>恢复自动精简</span>
                  </button>
                )}
                {computing && (
                  <Badge variant="accent" className="bg-surface/90">
                    布局计算中…
                  </Badge>
                )}
              </>
            )}
          </div>

          {/* Bottom zoom controls + legend */}
          <div className="pointer-events-none absolute bottom-3 left-3 right-3 flex items-center justify-between gap-2">
            <div className="pointer-events-auto flex items-center gap-1 rounded-xl border border-border bg-surface/88 p-1 shadow-md backdrop-blur-xs">
              <Button
                size="icon"
                variant="ghost"
                className="h-7 w-7"
                onClick={() => controllerRef.current?.zoomAt(1.22)}
                aria-label="放大"
              >
                <ZoomIn className="h-3.5 w-3.5" />
              </Button>
              <span className="min-w-10 text-center font-mono text-[10px] text-fg-dim">
                {Math.round(renderMetrics.scale * 100)}%
              </span>
              <Button
                size="icon"
                variant="ghost"
                className="h-7 w-7"
                onClick={() => controllerRef.current?.zoomAt(0.82)}
                aria-label="缩小"
              >
                <ZoomOut className="h-3.5 w-3.5" />
              </Button>
              <Button
                size="icon"
                variant="ghost"
                className="h-7 w-7"
                onClick={() => controllerRef.current?.fitView(true)}
                aria-label="适配画布"
              >
                <Maximize2 className="h-3.5 w-3.5" />
              </Button>
            </div>

            <div className="hidden items-center gap-3 rounded-xl border border-border bg-surface/85 px-3 py-1.5 text-[11px] text-fg-dim shadow-md backdrop-blur-xs sm:flex">
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full" style={{ backgroundColor: palette.Concept }} />
                概念
              </span>
              {eventKindCounts.milestone > 0 && (
                <span className="flex items-center gap-1 text-fg">
                  <span className="text-[11px] text-warn">✦</span>
                  里程碑
                </span>
              )}
              <span className="flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-full" style={{ backgroundColor: palette.Event }} />
                事件
              </span>
              <span className="flex items-center gap-1.5">
                <span
                  className="h-2.5 w-2.5 rounded-full border-2"
                  style={{ borderColor: palette.Person }}
                />
                人物
              </span>
              <span className="flex items-center gap-1.5">
                <span
                  className="h-2 w-2 rotate-45 rounded-[1px]"
                  style={{ backgroundColor: palette.Location }}
                />
                地点
              </span>
              {viewMode === "global" && attitudeEdgeCount > 0 && (
                <>
                  <span className="flex items-center gap-1 text-[10px]">
                    <span
                      className="inline-block h-0.5 w-3 rounded"
                      style={{ backgroundColor: theme === "light" ? "#c24662" : "#ed8196" }}
                    />
                    喜欢
                  </span>
                  <span className="flex items-center gap-1 text-[10px]">
                    <span
                      className="inline-block h-0.5 w-3 border-b border-dashed"
                      style={{ borderColor: theme === "light" ? "#aa4232" : "#d66b56" }}
                    />
                    讨厌
                  </span>
                </>
              )}
              {viewMode === "global" && (
                <span className="border-l border-border pl-2.5 text-[10px] text-fg-faint">
                  双击节点进入局部图
                </span>
              )}
            </div>
          </div>

          {(loading || errorMsg || (graph && graph.nodes.length === 0)) && (
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center p-6">
              <div className="pointer-events-auto max-w-md rounded-2xl border border-border bg-surface/95 px-5 py-4 text-center shadow-lg">
                {loading ? (
                  <p className="text-sm text-fg-dim">正在加载记忆图谱与语义群落…</p>
                ) : errorMsg ? (
                  <div className="space-y-2">
                    <p className="text-sm text-danger">{errorMsg}</p>
                    <Button size="sm" variant="secondary" onClick={() => void loadFromDatabase()}>
                      重试加载
                    </Button>
                  </div>
                ) : (
                  <p className="text-sm text-fg-dim">当前记忆图谱尚无实体或关系记录。</p>
                )}
              </div>
            </div>
          )}
        </div>

        {/* Selected Node Inspector */}
        {selectedNode && graph && (
          <GraphInspector
            graph={graph}
            selectedNode={selectedNode}
            palette={palette}
            viewMode={viewMode}
            isLocalImport={isLocalImport}
            detaching={detaching}
            detachUncertain={detachUncertain}
            onClose={() => setSelectedId(null)}
            onSelectAndFocus={selectAndFocusNode}
            onEnterEgoMode={enterEgoMode}
            onSetCenter={(id) => void handleReLayout(correctHubs, id)}
            onOpenDedup={() => openPanel("dedup")}
            onDetachConcept={() => void handleDetachConcept()}
            onRefreshGraph={() => void loadFromDatabase()}
          />
        )}
      </div>
    </div>
  );
}
