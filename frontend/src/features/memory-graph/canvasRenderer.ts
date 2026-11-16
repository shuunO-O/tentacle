import type { ResolvedTheme } from "../../stores/themeStore";
import {
  DARK_CLUSTER_TINTS,
  DARK_GRAPH_PALETTE,
  LIGHT_CLUSTER_TINTS,
  LIGHT_GRAPH_PALETTE,
  REL_TYPE_LABELS,
  type ComputedGraphResult,
  type EdgeSentiment,
  type EgoSubgraph,
  type EventSubTypeFilter,
  type GraphCluster,
  type GraphNode,
  type GraphNodeType,
  type GraphViewMode,
  type LeafFoldMode,
} from "./types";

export interface GraphRenderOptions {
  theme: ResolvedTheme;
  viewMode: GraphViewMode;
  egoSubgraph: EgoSubgraph | null;
  visibleTypes: Record<GraphNodeType, boolean>;
  visibleEventKinds: EventSubTypeFilter;
  highlightAttitudes: boolean;
  mergeEquivalenceGroups: boolean;
  searchQuery: string;
  showClusters: boolean;
  showCrossEdges: boolean;
  showLabels: boolean;
  showEdgeLabels: boolean;
  smoothMotion: boolean;
  foldLeaves: LeafFoldMode;
  minDegree: number;
  edgeOpacity: number;
  nodeSize: number;
  edgeBudget: number;
  selectedId: number | null;
  hoveredId: number | null;
}

export interface GraphCamera {
  tx: number;
  ty: number;
  scale: number;
}

export interface GraphRenderMetrics {
  scale: number;
  drawnEdges: number;
  totalEdges: number;
  visibleNodes: number;
  foldedNodes: number;
}

interface PaletteSurface {
  bg: string;
  bgInner: string;
  gridLine: string;
  labelText: string;
  labelActiveText: string;
  labelHalo: string;
  clusterTitle: string;
  edgeBase: string;
  edgeCross: string;
  edgeActive: string;
  edgeLabelBg: string;
  edgeLabelText: string;
  highlightRing: string;
  searchRing: string;
  coreTint: string;
  badgeBg: string;
  badgeText: string;
  milestoneGlow: string;
  travelAccent: string;
  studyAccent: string;
  attitudeLike: string;
  attitudeDislike: string;
  attitudeNeutral: string;
}

const DARK_SURFACE: PaletteSurface = {
  bg: "#191410",
  bgInner: "#221c16",
  gridLine: "rgba(236, 229, 218, 0.055)",
  labelText: "#cfc5b7",
  labelActiveText: "#f5efe6",
  labelHalo: "rgba(25, 20, 16, 0.92)",
  clusterTitle: "#e2d7c7",
  edgeBase: "#a3988a",
  edgeCross: "#786e62",
  edgeActive: "#efe8dc",
  edgeLabelBg: "rgba(31, 25, 20, 0.88)",
  edgeLabelText: "#bfb4a4",
  highlightRing: "#efe8dc",
  searchRing: "#e58e6e",
  coreTint: "#f7f1e7",
  badgeBg: "rgba(43, 36, 28, 0.92)",
  badgeText: "#e5d9c8",
  milestoneGlow: "#f5c76b",
  travelAccent: "#77bcd8",
  studyAccent: "#a4c77b",
  attitudeLike: "#ed8196",
  attitudeDislike: "#d66b56",
  attitudeNeutral: "#96a4b0",
};

const LIGHT_SURFACE: PaletteSurface = {
  bg: "#f7f3ec",
  bgInner: "#fcf9f3",
  gridLine: "rgba(36, 31, 26, 0.055)",
  labelText: "#453d35",
  labelActiveText: "#1f1914",
  labelHalo: "rgba(247, 243, 236, 0.94)",
  clusterTitle: "#4a4138",
  edgeBase: "#6f6559",
  edgeCross: "#978c7e",
  edgeActive: "#2b231d",
  edgeLabelBg: "rgba(250, 247, 242, 0.92)",
  edgeLabelText: "#5e5449",
  highlightRing: "#241f1a",
  searchRing: "#c85c3a",
  coreTint: "#fffdf5",
  badgeBg: "rgba(245, 239, 230, 0.95)",
  badgeText: "#3a3128",
  milestoneGlow: "#b87a1d",
  travelAccent: "#367696",
  studyAccent: "#527d32",
  attitudeLike: "#c24662",
  attitudeDislike: "#aa4232",
  attitudeNeutral: "#6d7b87",
};

interface PreparedEdge {
  id: number;
  a: number;
  b: number;
  relType: string;
  sentiment?: EdgeSentiment;
  cross: boolean;
  hub: boolean;
  selfLoop: boolean;
  cpx: number;
  cpy: number;
  ux: number;
  uy: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
  path2d: Path2D | null;
}

interface NodeSprite {
  canvas: HTMLCanvasElement;
  size: number;
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function hexToRgba(hex: string, alpha: number): string {
  const clean = hex.replace("#", "");
  const r = Number.parseInt(clean.slice(0, 2), 16);
  const g = Number.parseInt(clean.slice(2, 4), 16);
  const b = Number.parseInt(clean.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function shadeHex(hex: string, amount: number): string {
  const clean = hex.replace("#", "");
  const target = amount < 0 ? 0 : 255;
  const p = Math.abs(amount);
  const mix = (channel: number) => Math.round(channel + (target - channel) * p);
  const r = mix(Number.parseInt(clean.slice(0, 2), 16));
  const g = mix(Number.parseInt(clean.slice(2, 4), 16));
  const b = mix(Number.parseInt(clean.slice(4, 6), 16));
  return `rgb(${r}, ${g}, ${b})`;
}

function formatRelLabel(type: string, sentiment?: EdgeSentiment): string {
  if (type === "HAS_ATTITUDE" && sentiment) {
    return `态度·${sentiment}`;
  }
  return REL_TYPE_LABELS[type] || type;
}

function sentimentColor(
  relType: string,
  sentiment: EdgeSentiment | undefined,
  surface: PaletteSurface,
  fallback: string,
): string {
  if (relType !== "HAS_ATTITUDE") return fallback;
  if (sentiment === "喜欢") return surface.attitudeLike;
  if (sentiment === "讨厌") return surface.attitudeDislike;
  if (sentiment === "中立" || sentiment === "不明确") return surface.attitudeNeutral;
  return surface.attitudeLike;
}

function drawRoundedRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  ctx.beginPath();
  if (typeof ctx.roundRect === "function") {
    ctx.roundRect(x, y, w, h, r);
  } else {
    ctx.rect(x, y, w, h);
  }
}

export class GraphCanvasController {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D | null;
  private data: ComputedGraphResult | null = null;
  private options: GraphRenderOptions;
  private camera: GraphCamera = { tx: 0, ty: 0, scale: 1 };
  private width = 800;
  private height = 600;
  private dpr = 1;
  private hasMeasured = false;

  private gridCellSize = 48;
  private spatialGrid = new Map<string, number[]>();
  private preparedEdges: PreparedEdge[] = [];
  private edgeOrder: number[] = [];
  private adjacentEdges: number[][] = [];
  private labelOrder: number[] = [];
  private equivalencePairs: Array<{ a: number; b: number }> = [];
  private attitudeEndpoints = new Set<number>();
  private lastVisibleMask = new Uint8Array(0);

  private sprites = new Map<string, NodeSprite>();

  private nodeAlpha = new Float32Array(0);
  private ringAlpha = new Float32Array(0);
  private edgeAlpha = new Map<number, number>();
  private highlightTargets = new Set<number>();
  private focusDim = 0;
  private lastFocusedId: number | null = null;

  private rafId = 0;
  private animCancel: (() => void) | null = null;
  private animTargets: { start: GraphCamera; target: GraphCamera } | null = null;
  private focusAnimId = 0;
  private lastTick = 0;
  private dirty = true;

  private lastMetricsKey = "";
  private metricsCb?: (metrics: GraphRenderMetrics) => void;

  constructor(
    canvas: HTMLCanvasElement,
    initialOptions: GraphRenderOptions,
    onMetricsChange?: (metrics: GraphRenderMetrics) => void,
  ) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.options = initialOptions;
    this.metricsCb = onMetricsChange;
    this.resize();
  }

  resize(): void {
    const rect = this.canvas.getBoundingClientRect();
    const w = Math.max(240, Math.round(rect.width || this.canvas.clientWidth || 800));
    const h = Math.max(200, Math.round(rect.height || this.canvas.clientHeight || 600));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (w !== this.width || h !== this.height || dpr !== this.dpr) {
      if (this.hasMeasured) {
        const dx = (w - this.width) / 2;
        const dy = (h - this.height) / 2;
        this.camera.tx += dx;
        this.camera.ty += dy;
        if (this.animTargets) {
          this.animTargets.start.tx += dx;
          this.animTargets.start.ty += dy;
          this.animTargets.target.tx += dx;
          this.animTargets.target.ty += dy;
        }
      }
      this.hasMeasured = true;
      this.width = w;
      this.height = h;
      this.dpr = dpr;
      this.canvas.width = Math.round(w * dpr);
      this.canvas.height = Math.round(h * dpr);
      this.markDirty();
    } else {
      this.hasMeasured = true;
    }
  }

  setData(data: ComputedGraphResult | null, fit = true): void {
    this.data = data;
    this.sprites.clear();
    this.edgeAlpha.clear();
    this.highlightTargets.clear();
    this.focusDim = 0;
    this.lastFocusedId = null;

    if (data) {
      this.nodeAlpha = new Float32Array(data.nodes.length).fill(1);
      this.ringAlpha = new Float32Array(data.nodes.length).fill(0);
      this.rebuildSpatialGrid();
      this.prepareGeometry();
    } else {
      this.nodeAlpha = new Float32Array(0);
      this.ringAlpha = new Float32Array(0);
      this.spatialGrid.clear();
      this.preparedEdges = [];
      this.edgeOrder = [];
      this.adjacentEdges = [];
      this.labelOrder = [];
      this.equivalencePairs = [];
      this.attitudeEndpoints.clear();
      this.lastVisibleMask = new Uint8Array(0);
    }

    if (fit && data) {
      this.fitView(false);
    } else {
      this.markDirty();
    }
  }

  setOptions(next: Partial<GraphRenderOptions>): void {
    const prevMode = this.options.viewMode;
    const prevEgoRoot = this.options.egoSubgraph?.rootId;
    const prevEgoHops = this.options.egoSubgraph?.hops;
    const prevMergeEq = this.options.mergeEquivalenceGroups;
    const themeChanged = next.theme && next.theme !== this.options.theme;
    this.options = { ...this.options, ...next };
    if (themeChanged) {
      this.sprites.clear();
    }

    const activeId = this.options.hoveredId ?? this.options.selectedId;
    const mergeChanged = prevMergeEq !== this.options.mergeEquivalenceGroups;
    if (activeId !== this.lastFocusedId || mergeChanged) {
      this.lastFocusedId = activeId;
      const targets = new Set<number>();
      if (activeId !== null && this.adjacentEdges[activeId]) {
        for (const eid of this.adjacentEdges[activeId]!) targets.add(eid);
      }
      if (activeId !== null && this.options.mergeEquivalenceGroups && this.data) {
        const activeNode = this.data.nodes[activeId];
        if (activeNode?.eqRepId === activeId && (activeNode.eqGroupSize || 1) >= 2) {
          for (const group of this.data.analysis.equivalenceGroups || []) {
            if (group[0] !== activeId) continue;
            for (const mid of group) {
              for (const eid of this.adjacentEdges[mid] || []) targets.add(eid);
            }
          }
        }
      }
      this.highlightTargets = targets;
      for (const edgeId of this.highlightTargets) {
        if (!this.edgeAlpha.has(edgeId)) {
          this.edgeAlpha.set(edgeId, 0);
        }
      }
    }

    const modeChanged = next.viewMode !== undefined && next.viewMode !== prevMode;
    const egoChanged =
      this.options.viewMode === "ego" &&
      (this.options.egoSubgraph?.rootId !== prevEgoRoot ||
        this.options.egoSubgraph?.hops !== prevEgoHops);

    if (modeChanged || egoChanged) {
      this.fitView(true);
    }

    this.scheduleFocusTick();
    this.markDirty();
  }

  getCamera(): GraphCamera {
    return { ...this.camera };
  }

  panBy(dxScreen: number, dyScreen: number): void {
    this.cancelAnim();
    this.camera.tx += dxScreen;
    this.camera.ty += dyScreen;
    this.markDirty();
  }

  zoomAt(factor: number, screenX?: number, screenY?: number): void {
    this.cancelAnim();
    const sx = screenX ?? this.width / 2;
    const sy = screenY ?? this.height / 2;
    const oldScale = this.camera.scale;
    const nextScale = Math.min(8, Math.max(0.14, oldScale * factor));
    const wx = (sx - this.camera.tx) / oldScale;
    const wy = (sy - this.camera.ty) / oldScale;
    this.camera.scale = nextScale;
    this.camera.tx = sx - wx * nextScale;
    this.camera.ty = sy - wy * nextScale;
    this.markDirty();
  }

  fitView(animate = true): void {
    const applyCamera = (target: GraphCamera, duration = 300) => {
      if (animate && this.options.smoothMotion && !prefersReducedMotion()) {
        this.animateTo(target, duration);
      } else {
        this.cancelAnim();
        this.camera = target;
        this.markDirty();
      }
    };

    if (this.options.viewMode === "ego" && this.options.egoSubgraph) {
      const ego = this.options.egoSubgraph;
      let minX = 560 - ego.ring1Radius - 40;
      let maxX = 560 + ego.ring1Radius + 40;
      let minY = 400 - ego.ring1Radius - 40;
      let maxY = 400 + ego.ring1Radius + 40;
      for (const item of ego.nodes) {
        if (item.x - 40 < minX) minX = item.x - 40;
        if (item.x + 70 > maxX) maxX = item.x + 70;
        if (item.y - 30 < minY) minY = item.y - 30;
        if (item.y + 30 > maxY) maxY = item.y + 30;
      }
      const spanX = Math.max(220, maxX - minX);
      const spanY = Math.max(220, maxY - minY);
      const scale = Math.min(
        1.8,
        Math.max(0.25, Math.min((this.width * 0.86) / spanX, (this.height * 0.86) / spanY)),
      );
      applyCamera({
        scale,
        tx: this.width / 2 - ((minX + maxX) / 2) * scale,
        ty: this.height / 2 - ((minY + maxY) / 2) * scale,
      });
      return;
    }

    if (!this.data || this.data.nodes.length === 0) {
      this.camera = { tx: this.width / 2 - 560, ty: this.height / 2 - 400, scale: 1 };
      this.markDirty();
      return;
    }
    const { minX, maxX, minY, maxY } = this.data.analysis.bounds;
    const spanX = Math.max(200, maxX - minX);
    const spanY = Math.max(200, maxY - minY);
    const pad = 0.86;
    const scale = Math.min(
      2.2,
      Math.max(0.16, Math.min((this.width * pad) / spanX, (this.height * pad) / spanY)),
    );
    applyCamera(
      {
        scale,
        tx: this.width / 2 - ((minX + maxX) / 2) * scale,
        ty: this.height / 2 - ((minY + maxY) / 2) * scale,
      },
      320,
    );
  }

  focusNode(nodeId: number, minScale = 1.6): void {
    const glideTo = (x: number, y: number, targetScale: number) => {
      const target: GraphCamera = {
        scale: targetScale,
        tx: this.width / 2 - x * targetScale,
        ty: this.height / 2 - y * targetScale,
      };
      if (this.options.smoothMotion && !prefersReducedMotion()) this.animateTo(target, 300);
      else {
        this.cancelAnim();
        this.camera = target;
        this.markDirty();
      }
    };

    if (this.options.viewMode === "ego" && this.options.egoSubgraph) {
      const idx = this.options.egoSubgraph.nodeIndexById.get(nodeId);
      const item = idx !== undefined ? this.options.egoSubgraph.nodes[idx] : undefined;
      if (item) glideTo(item.x, item.y, Math.max(this.camera.scale, Math.min(minScale, 1.35)));
      return;
    }

    if (!this.data) return;
    const node = this.data.nodes[nodeId];
    if (!node) return;
    glideTo(node.x, node.y, Math.max(this.camera.scale, minScale));
  }

  hitTest(screenX: number, screenY: number): GraphNode | null {
    const wx = (screenX - this.camera.tx) / this.camera.scale;
    const wy = (screenY - this.camera.ty) / this.camera.scale;
    const radius = 11 / Math.max(0.35, this.camera.scale);

    if (this.options.viewMode === "ego" && this.options.egoSubgraph) {
      let best: GraphNode | null = null;
      let bestScore = Infinity;
      for (const item of this.options.egoSubgraph.nodes) {
        if (!this.options.visibleTypes[item.node.type]) continue;
        const dist = Math.hypot(item.x - wx, item.y - wy);
        const hitRadius = item.r * this.options.nodeSize + radius * 0.75;
        if (dist <= hitRadius) {
          const score = dist / Math.max(1, item.r);
          if (score < bestScore) {
            bestScore = score;
            best = item.node;
          }
        }
      }
      return best;
    }

    if (!this.data) return null;
    const range = Math.max(1, Math.ceil(radius / this.gridCellSize));
    const cellX = Math.floor(wx / this.gridCellSize);
    const cellY = Math.floor(wy / this.gridCellSize);
    const activeId = this.options.hoveredId ?? this.options.selectedId;
    const foldingActive = this.shouldFoldLeaves();

    let best: GraphNode | null = null;
    let bestScore = Infinity;

    for (let dx = -range; dx <= range; dx += 1) {
      for (let dy = -range; dy <= range; dy += 1) {
        const list = this.spatialGrid.get(`${cellX + dx}:${cellY + dy}`);
        if (!list) continue;
        for (const id of list) {
          const node = this.data.nodes[id];
          if (!node) continue;
          const visible =
            this.lastVisibleMask.length === this.data.nodes.length
              ? Boolean(this.lastVisibleMask[id])
              : this.isNodeCurrentlyVisible(node, activeId, foldingActive);
          if (!visible) continue;
          const dist = Math.hypot(node.x - wx, node.y - wy);
          const hitRadius = node.r * this.options.nodeSize + radius * 0.72;
          if (dist <= hitRadius) {
            const priorityBoost = node.protagonist ? 0.6 : node.anchor ? 0.85 : 1;
            const score = (dist / Math.max(1, node.r * this.options.nodeSize)) * priorityBoost;
            if (score < bestScore) {
              bestScore = score;
              best = node;
            }
          }
        }
      }
    }
    return best;
  }

  dispose(): void {
    this.cancelAnim();
    if (this.rafId) {
      cancelAnimationFrame(this.rafId);
      this.rafId = 0;
    }
    if (this.focusAnimId) {
      cancelAnimationFrame(this.focusAnimId);
      this.focusAnimId = 0;
    }
  }

  private shouldFoldLeaves(): boolean {
    if (!this.data) return false;
    if (this.options.foldLeaves === "always") return true;
    if (this.options.foldLeaves === "off") return false;
    // "auto": fold single-occurrence leaf concepts on overview zoom when graph is dense (>= 40 nodes)
    return this.data.nodes.length >= 40 && this.camera.scale < 1.15;
  }

  private isNodeCurrentlyVisible(
    node: GraphNode,
    activeId: number | null,
    foldingActive: boolean,
  ): boolean {
    if (!this.options.visibleTypes[node.type]) return false;
    if (node.type === "Event" && !this.options.visibleEventKinds[node.eventKind ?? "other"]) {
      return false;
    }
    const query = this.options.searchQuery.trim().toLowerCase();
    const isMatch = query.length > 0 && node.name.toLowerCase().includes(query);
    if (node.id === this.options.selectedId || node.id === this.options.hoveredId || isMatch) {
      return true;
    }
    if (
      this.options.mergeEquivalenceGroups &&
      (node.eqGroupSize || 1) >= 2 &&
      node.eqRepId !== null &&
      node.eqRepId !== undefined &&
      node.eqRepId !== node.id
    ) {
      return false;
    }
    if (node.protagonist) return true;
    if (this.options.highlightAttitudes && this.attitudeEndpoints.has(node.id)) {
      return true;
    }
    if (this.options.minDegree > 1 && node.degree < this.options.minDegree) {
      if (activeId !== null && (this.data?.analysis.adj[activeId]?.includes(node.id) ?? false)) {
        return true;
      }
      return false;
    }
    if (foldingActive && node.isSingleLeaf) {
      if (this.attitudeEndpoints.has(node.id)) return true;
      if (activeId !== null && node.leafParentId === activeId) return true;
      return false;
    }
    return true;
  }

  private rebuildSpatialGrid(): void {
    this.spatialGrid.clear();
    if (!this.data) return;
    for (const node of this.data.nodes) {
      const key = `${Math.floor(node.x / this.gridCellSize)}:${Math.floor(node.y / this.gridCellSize)}`;
      const bucket = this.spatialGrid.get(key);
      if (bucket) bucket.push(node.id);
      else this.spatialGrid.set(key, [node.id]);
    }
  }

  private prepareGeometry(): void {
    if (!this.data) return;
    const { nodes, edges } = this.data;
    const hasPath2D = typeof Path2D !== "undefined";

    this.adjacentEdges = Array.from({ length: nodes.length }, () => []);
    const priorityRank = (n: GraphNode) =>
      n.protagonist
        ? 4
        : n.eventKind === "milestone"
          ? 3
          : n.anchor
            ? 2
            : n.eventKind === "travel" || n.eventKind === "study"
              ? 1
              : 0;
    this.labelOrder = [...nodes]
      .sort(
        (a, b) =>
          priorityRank(b) - priorityRank(a) ||
          b.degree - a.degree ||
          a.id - b.id,
      )
      .map((n) => n.id);

    this.attitudeEndpoints.clear();
    const eqMap = new Map<string, number[]>();
    for (const node of nodes) {
      if (node.type === "Concept" && node.equivalenceGroup) {
        const list = eqMap.get(node.equivalenceGroup);
        if (list) list.push(node.id);
        else eqMap.set(node.equivalenceGroup, [node.id]);
      }
    }
    this.equivalencePairs = [];
    for (const members of eqMap.values()) {
      for (let i = 1; i < members.length && i <= 12; i += 1) {
        this.equivalencePairs.push({ a: members[i - 1]!, b: members[i]! });
      }
    }

    const localIds: number[] = [];
    const crossIds: number[] = [];
    const hubIds: number[] = [];

    this.preparedEdges = edges.map((e, idx) => {
      this.adjacentEdges[e.a]?.push(idx);
      if (e.a !== e.b) {
        this.adjacentEdges[e.b]?.push(idx);
      }
      if (e.type === "HAS_ATTITUDE") {
        this.attitudeEndpoints.add(e.a);
        this.attitudeEndpoints.add(e.b);
      }

      const a = nodes[e.a]!;
      const b = nodes[e.b]!;
      let cpx = (a.x + b.x) / 2;
      let cpy = (a.y + b.y) / 2;
      let ux = 1;
      let uy = 0;
      let left = Math.min(a.x, b.x);
      let right = Math.max(a.x, b.x);
      let top = Math.min(a.y, b.y);
      let bottom = Math.max(a.y, b.y);
      let path2d: Path2D | null = null;

      if (e.a === e.b) {
        const radius = 18 + e.loop * 10;
        left = a.x - radius * 2;
        right = a.x + radius * 2;
        top = a.y - radius * 2;
        bottom = a.y + 4;
        if (hasPath2D) {
          path2d = new Path2D(
            `M${a.x - 3},${a.y - 2} C${a.x - radius * 2},${a.y - radius * 2} ${a.x + radius * 2},${a.y - radius * 2} ${a.x + 3},${a.y - 2}`,
          );
        }
      } else {
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const len = Math.hypot(dx, dy) || 1;
        const bend = (e.cross ? 0.055 : 0.025) + (e.curve / len) * (e.a <= e.b ? 1 : -1);
        cpx = (a.x + b.x) / 2 - dy * bend;
        cpy = (a.y + b.y) / 2 + dx * bend;
        const tx = b.x - cpx;
        const ty = b.y - cpy;
        const tLen = Math.hypot(tx, ty) || 1;
        ux = tx / tLen;
        uy = ty / tLen;

        left = Math.min(a.x, b.x, cpx);
        right = Math.max(a.x, b.x, cpx);
        top = Math.min(a.y, b.y, cpy);
        bottom = Math.max(a.y, b.y, cpy);

        if (hasPath2D) {
          path2d = new Path2D(`M${a.x},${a.y} Q${cpx},${cpy} ${b.x},${b.y}`);
        }
      }

      if (e.hub) hubIds.push(idx);
      else if (e.cross) crossIds.push(idx);
      else localIds.push(idx);

      return {
        id: idx,
        a: e.a,
        b: e.b,
        relType: e.type,
        sentiment: e.sentiment,
        cross: e.cross,
        hub: e.hub,
        selfLoop: e.a === e.b,
        cpx,
        cpy,
        ux,
        uy,
        left,
        right,
        top,
        bottom,
        path2d,
      };
    });

    const hashSort = (list: number[]) =>
      list.sort(
        (a, b) =>
          ((Math.imul(a + 17, 73856093) ^ Math.imul(a + 3, 19349663)) >>> 0) -
          ((Math.imul(b + 17, 73856093) ^ Math.imul(b + 3, 19349663)) >>> 0),
      );

    hashSort(localIds);
    hashSort(crossIds);
    hashSort(hubIds);

    const ordered: number[] = [];
    let li = 0;
    let ci = 0;
    let hi = 0;
    while (li < localIds.length || ci < crossIds.length || hi < hubIds.length) {
      for (let k = 0; k < 7 && li < localIds.length; k += 1) ordered.push(localIds[li++]!);
      for (let k = 0; k < 2 && ci < crossIds.length; k += 1) ordered.push(crossIds[ci++]!);
      if (hi < hubIds.length) ordered.push(hubIds[hi++]!);
    }
    this.edgeOrder = ordered;
  }

  private scheduleFocusTick(): void {
    if (!this.data || !this.ctx) return;
    if (this.focusAnimId) return;
    this.lastTick = 0;
    this.focusAnimId = requestAnimationFrame((now) => this.tickFocus(now));
  }

  private getExpandedNeighborhood(activeId: number | null): Set<number> | null {
    if (activeId === null || !this.data) return null;
    const set = new Set<number>([activeId, ...(this.data.analysis.adj[activeId] || [])]);
    const activeNode = this.data.nodes[activeId];
    if (
      activeNode &&
      this.options.mergeEquivalenceGroups &&
      activeNode.eqRepId === activeId &&
      (activeNode.eqGroupSize || 1) >= 2
    ) {
      for (const group of this.data.analysis.equivalenceGroups || []) {
        if (group[0] !== activeId) continue;
        for (const mid of group) {
          set.add(mid);
          for (const nb of this.data.analysis.adj[mid] || []) set.add(nb);
        }
      }
    }
    return set;
  }

  private tickFocus(now: number): void {
    this.focusAnimId = 0;
    if (!this.data) return;

    const dt = this.lastTick ? Math.min(32, Math.max(0, now - this.lastTick)) : 16.7;
    this.lastTick = now;
    const smooth = this.options.smoothMotion && !prefersReducedMotion();
    const blend = smooth ? 1 - Math.exp(-dt / 85) : 1;
    let pending = false;

    const approach = (current: number, target: number): number => {
      if (blend >= 0.999) return target;
      const next = current + (target - current) * blend;
      if (Math.abs(next - target) > 0.004) {
        pending = true;
        return next;
      }
      return target;
    };

    const activeId = this.options.hoveredId ?? this.options.selectedId;
    const near = this.getExpandedNeighborhood(activeId);
    this.focusDim = approach(this.focusDim, activeId === null ? 0 : 1);

    for (const node of this.data.nodes) {
      const targetNodeAlpha = !near || near.has(node.id) ? 1 : 0.19;
      const targetRingAlpha = node.id === this.options.selectedId || node.id === this.options.hoveredId ? 1 : 0;
      this.nodeAlpha[node.id] = approach(this.nodeAlpha[node.id] ?? 1, targetNodeAlpha);
      this.ringAlpha[node.id] = approach(this.ringAlpha[node.id] ?? 0, targetRingAlpha);
    }

    for (const [edgeId, alpha] of this.edgeAlpha) {
      const target = this.highlightTargets.has(edgeId) ? 1 : 0;
      const next = approach(alpha, target);
      if (next <= 0.001 && target === 0) {
        this.edgeAlpha.delete(edgeId);
      } else {
        this.edgeAlpha.set(edgeId, next);
      }
    }

    this.markDirty();
    if (pending) {
      this.focusAnimId = requestAnimationFrame((ts) => this.tickFocus(ts));
    } else {
      this.lastTick = 0;
    }
  }

  private cancelAnim(): void {
    this.animTargets = null;
    if (this.animCancel) {
      this.animCancel();
      this.animCancel = null;
    }
  }

  private animateTo(target: GraphCamera, durationMs: number): void {
    this.cancelAnim();
    const start = { ...this.camera };
    const animGoal = { ...target };
    this.animTargets = { start, target: animGoal };
    const t0 = performance.now();
    let cancelled = false;
    let timer = 0;

    const tick = () => {
      if (cancelled) return;
      const elapsed = performance.now() - t0;
      const p = Math.min(1, elapsed / durationMs);
      const ease = 1 - Math.pow(1 - p, 3);
      this.camera = {
        tx: start.tx + (animGoal.tx - start.tx) * ease,
        ty: start.ty + (animGoal.ty - start.ty) * ease,
        scale: Math.exp(
          Math.log(start.scale) + (Math.log(animGoal.scale) - Math.log(start.scale)) * ease,
        ),
      };
      this.markDirty();
      if (p < 1) {
        timer = requestAnimationFrame(tick);
      } else {
        this.animTargets = null;
        this.animCancel = null;
      }
    };

    this.animCancel = () => {
      cancelled = true;
      this.animTargets = null;
      if (timer) cancelAnimationFrame(timer);
    };
    timer = requestAnimationFrame(tick);
  }

  private markDirty(): void {
    this.dirty = true;
    if (!this.rafId) {
      this.rafId = requestAnimationFrame(() => {
        this.rafId = 0;
        if (this.dirty) {
          this.dirty = false;
          this.render();
        }
      });
    }
  }

  private getNodeSprite(
    node: GraphNode,
    palette: Record<GraphNodeType, string>,
    surface: PaletteSurface,
  ): NodeSprite | null {
    if (typeof document === "undefined") return null;
    const key = `${this.options.theme}:${node.type}:${node.eventKind ?? ""}:${node.r.toFixed(2)}:${node.protagonist}:${node.anchor}`;
    const cached = this.sprites.get(key);
    if (cached) return cached;

    const size = Math.ceil(node.r * 7.2 + 28);
    const quality = 4;
    const canvas = document.createElement("canvas");
    canvas.width = size * quality;
    canvas.height = size * quality;
    const c = canvas.getContext("2d");
    if (!c) return null;

    const isMilestone = node.type === "Event" && node.eventKind === "milestone";
    const isTravel = node.type === "Event" && node.eventKind === "travel";
    const isStudy = node.type === "Event" && node.eventKind === "study";
    const baseColor = isMilestone
      ? surface.milestoneGlow
      : palette[node.type] || palette.Other;
    const isLight = this.options.theme === "light";
    c.scale(quality, quality);
    c.translate(size / 2, size / 2);

    const aura = c.createRadialGradient(0, 0, node.r * 0.4, 0, 0, node.r * (isMilestone ? 3.8 : 3.2));
    aura.addColorStop(0, hexToRgba(baseColor, isLight ? (isMilestone ? 0.24 : 0.14) : (isMilestone ? 0.32 : 0.2)));
    aura.addColorStop(0.55, hexToRgba(baseColor, isLight ? 0.05 : 0.07));
    aura.addColorStop(1, hexToRgba(baseColor, 0));
    c.fillStyle = aura;
    c.fillRect(-size / 2, -size / 2, size, size);

    const bloom = c.createRadialGradient(0, 0, 0, 0, 0, node.r * 1.9);
    bloom.addColorStop(0, hexToRgba(baseColor, isLight ? 0.26 : 0.4));
    bloom.addColorStop(0.55, hexToRgba(baseColor, isLight ? 0.08 : 0.12));
    bloom.addColorStop(1, hexToRgba(baseColor, 0));
    c.fillStyle = bloom;
    c.fillRect(-size / 2, -size / 2, size, size);

    if (node.type === "Person") {
      c.strokeStyle = baseColor;
      c.globalAlpha = node.protagonist ? 0.9 : 0.72;
      c.lineWidth = 0.75;
      c.setLineDash(node.protagonist ? [] : [1.2, 1.6]);
      c.beginPath();
      c.arc(0, 0, node.r + 3.8, 0, Math.PI * 2);
      c.stroke();
      c.setLineDash([]);
      c.globalAlpha = 1;
    } else if (isMilestone) {
      c.strokeStyle = surface.milestoneGlow;
      c.globalAlpha = 0.78;
      c.lineWidth = 0.9;
      c.beginPath();
      c.arc(0, 0, node.r + 3.6, 0, Math.PI * 2);
      c.stroke();
      c.globalAlpha = 0.35;
      c.setLineDash([1.8, 3.2]);
      c.beginPath();
      c.arc(0, 0, node.r + 6.4, 0, Math.PI * 2);
      c.stroke();
      c.setLineDash([]);
      c.globalAlpha = 1;
    } else if (isTravel) {
      c.strokeStyle = surface.travelAccent;
      c.globalAlpha = 0.7;
      c.lineWidth = 0.75;
      c.setLineDash([2.2, 2.2]);
      c.beginPath();
      c.arc(0, 0, node.r + 2.8, 0, Math.PI * 2);
      c.stroke();
      c.setLineDash([]);
      c.globalAlpha = 1;
    } else if (isStudy) {
      c.strokeStyle = surface.studyAccent;
      c.globalAlpha = 0.68;
      c.lineWidth = 0.75;
      c.beginPath();
      c.arc(0, 0, node.r + 2.5, -Math.PI * 0.25, Math.PI * 0.75);
      c.stroke();
      c.globalAlpha = 1;
    }
    if (node.protagonist) {
      c.strokeStyle = baseColor;
      c.lineWidth = 0.55;
      c.globalAlpha = 0.36;
      c.setLineDash([1.2, 6.5]);
      c.beginPath();
      c.arc(0, 0, node.r + 14, 0, Math.PI * 2);
      c.stroke();
      c.setLineDash([]);
      c.globalAlpha = 1;
    }

    const bodyR = node.type === "Person" ? node.r * 0.65 : node.r;
    const body = c.createRadialGradient(-bodyR * 0.35, -bodyR * 0.4, bodyR * 0.1, 0, 0, bodyR * 1.05);
    body.addColorStop(0, shadeHex(baseColor, isLight ? 0.32 : 0.52));
    body.addColorStop(0.55, baseColor);
    body.addColorStop(1, shadeHex(baseColor, isLight ? -0.38 : -0.26));

    c.fillStyle = body;
    c.strokeStyle = hexToRgba(baseColor, 0.92);
    c.lineWidth = 0.4;
    c.beginPath();
    if (node.type === "Location") {
      const d = node.r * 1.13;
      c.moveTo(0, -d);
      c.lineTo(d, 0);
      c.lineTo(0, d);
      c.lineTo(-d, 0);
      c.closePath();
    } else if (isMilestone) {
      // Crisp 4-point faceted milestone emblem
      const outerR = bodyR * 1.18;
      const innerR = bodyR * 0.56;
      for (let i = 0; i < 8; i += 1) {
        const ang = (i * Math.PI) / 4 - Math.PI / 2;
        const rad = i % 2 === 0 ? outerR : innerR;
        const px = Math.cos(ang) * rad;
        const py = Math.sin(ang) * rad;
        if (i === 0) c.moveTo(px, py);
        else c.lineTo(px, py);
      }
      c.closePath();
    } else {
      c.arc(0, 0, bodyR, 0, Math.PI * 2);
    }
    c.fill();
    c.stroke();

    if (node.r >= 1.6 && !isMilestone) {
      c.fillStyle = "rgba(255, 255, 255, 0.38)";
      c.beginPath();
      c.ellipse(-node.r * 0.28, -node.r * 0.34, node.r * 0.22, node.r * 0.14, -0.6, 0, Math.PI * 2);
      c.fill();
    }

    c.fillStyle = surface.coreTint;
    c.globalAlpha = isMilestone ? 0.92 : 0.78;
    c.beginPath();
    c.arc(0, 0, node.protagonist ? 2 : isMilestone ? 1.55 : Math.min(0.95, node.r * 0.28), 0, Math.PI * 2);
    c.fill();
    c.globalAlpha = 1;

    if ((node.anchor || node.type === "Event") && !isMilestone) {
      const ray = node.r + 2.6;
      c.strokeStyle = baseColor;
      c.globalAlpha = 0.36;
      c.lineWidth = 0.5;
      c.beginPath();
      c.moveTo(-ray, 0);
      c.lineTo(ray, 0);
      c.moveTo(0, -ray);
      c.lineTo(0, ray);
      c.stroke();
      c.globalAlpha = 1;
    }

    const result = { canvas, size };
    this.sprites.set(key, result);
    return result;
  }

  render(): void {
    const ctx = this.ctx;
    if (!ctx) return;

    const isLight = this.options.theme === "light";
    const palette = isLight ? LIGHT_GRAPH_PALETTE : DARK_GRAPH_PALETTE;
    const regionTints = isLight ? LIGHT_CLUSTER_TINTS : DARK_CLUSTER_TINTS;
    const surface = isLight ? LIGHT_SURFACE : DARK_SURFACE;

    ctx.save();
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);

    const bgGrad = ctx.createRadialGradient(
      this.width * 0.5,
      this.height * 0.42,
      20,
      this.width * 0.5,
      this.height * 0.5,
      Math.max(this.width, this.height) * 0.72,
    );
    bgGrad.addColorStop(0, surface.bgInner);
    bgGrad.addColorStop(1, surface.bg);
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, this.width, this.height);

    if (this.options.viewMode === "ego" && this.options.egoSubgraph) {
      this.renderEgoSubgraph(ctx, this.options.egoSubgraph, palette, surface);
      ctx.restore();
      return;
    }

    if (!this.data || this.data.nodes.length === 0) {
      ctx.restore();
      this.reportMetrics(0, 0, 0, 0);
      return;
    }

    const scale = this.camera.scale;
    const left = -this.camera.tx / scale;
    const right = (this.width - this.camera.tx) / scale;
    const top = -this.camera.ty / scale;
    const bottom = (this.height - this.camera.ty) / scale;
    const inViewport = (n: GraphNode) =>
      n.x >= left - 28 && n.x <= right + 28 && n.y >= top - 28 && n.y <= bottom + 28;

    ctx.translate(this.camera.tx, this.camera.ty);
    ctx.scale(scale, scale);

    // Concentric guide rings around protagonist / center
    const centerNode = this.data.nodes.find((n) => n.protagonist);
    const ringCx = centerNode ? centerNode.x : this.data.analysis.bounds.cx;
    const ringCy = centerNode ? centerNode.y : this.data.analysis.bounds.cy;
    const maxRing = Math.max(310, this.data.analysis.bounds.radius);
    ctx.strokeStyle = surface.gridLine;
    ctx.lineWidth = 1 / scale;
    for (const [frac, alpha] of [
      [0.34, 0.75],
      [0.67, 0.58],
      [1.0, 0.42],
    ] as const) {
      ctx.globalAlpha = alpha * (1 - 0.45 * this.focusDim);
      ctx.beginPath();
      ctx.arc(ringCx, ringCy, maxRing * frac, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // Community island halos
    if (this.options.showClusters && this.data.analysis.clear && this.data.clusters.length > 0) {
      const dim = 1 - 0.45 * this.focusDim;
      for (let ci = 0; ci < this.data.clusters.length; ci += 1) {
        const cluster = this.data.clusters[ci] as GraphCluster;
        if (cluster.loose || cluster.size < 4) continue;
        if (
          cluster.x + cluster.rx < left ||
          cluster.x - cluster.rx > right ||
          cluster.y + cluster.ry < top ||
          cluster.y - cluster.ry > bottom
        ) {
          continue;
        }

        const tint = regionTints[ci % regionTints.length]!;
        const rHalo = cluster.rx + 6;
        const mist = ctx.createRadialGradient(cluster.x, cluster.y, 0, cluster.x, cluster.y, rHalo);
        mist.addColorStop(0, hexToRgba(tint, isLight ? 0.08 : 0.1));
        mist.addColorStop(0.72, hexToRgba(tint, isLight ? 0.025 : 0.032));
        mist.addColorStop(1, hexToRgba(tint, 0));
        ctx.globalAlpha = dim;
        ctx.fillStyle = mist;
        ctx.beginPath();
        ctx.arc(cluster.x, cluster.y, rHalo, 0, Math.PI * 2);
        ctx.fill();

        ctx.globalAlpha = dim * 0.24;
        ctx.strokeStyle = tint;
        ctx.lineWidth = 0.75 / scale;
        ctx.setLineDash([2.5 / scale, 5.5 / scale]);
        ctx.beginPath();
        ctx.arc(cluster.x, cluster.y, cluster.rx + 4, 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.globalAlpha = 1;
    }

    const { nodes } = this.data;
    const activeId = this.options.hoveredId ?? this.options.selectedId;
    const activeNeighborhood = this.getExpandedNeighborhood(activeId);
    const query = this.options.searchQuery.trim().toLowerCase();
    const hasSearch = query.length > 0;
    const foldingActive = this.shouldFoldLeaves();

    // Determine currently visible nodes and count folded leaves + equivalence group aliases
    const visibleMask = new Uint8Array(nodes.length);
    const hiddenLeavesByParent = new Map<number, number>();
    const hiddenEqByRep = new Map<number, number>();
    const eventSubFilterActive = Object.values(this.options.visibleEventKinds).some((v) => !v);
    let visibleNodeCount = 0;
    let foldedNodeCount = 0;

    for (const node of nodes) {
      if (this.isNodeCurrentlyVisible(node, activeId, foldingActive)) {
        visibleMask[node.id] = 1;
      }
    }

    // When filtering by Event subtype (e.g., Milestones only), trim disconnected leaf/concept clutter
    if (eventSubFilterActive) {
      for (const node of nodes) {
        if (!visibleMask[node.id]) continue;
        if (
          node.isSingleLeaf &&
          node.leafParentId !== null &&
          node.leafParentId !== undefined &&
          !visibleMask[node.leafParentId] &&
          !this.attitudeEndpoints.has(node.id)
        ) {
          visibleMask[node.id] = 0;
          continue;
        }
        if (
          node.type !== "Person" &&
          node.type !== "Event" &&
          node.id !== this.options.selectedId &&
          node.id !== this.options.hoveredId &&
          !(hasSearch && node.name.toLowerCase().includes(query))
        ) {
          const checkIds =
            this.options.mergeEquivalenceGroups &&
            node.eqRepId === node.id &&
            (node.eqGroupSize || 1) >= 2
              ? (this.data.analysis.equivalenceGroups || []).find((g) => g[0] === node.id) || [node.id]
              : [node.id];
          const hasVisibleNeighbor = checkIds.some((cid) =>
            (this.data!.analysis.adj[cid] || []).some(
              (nbId) => nbId !== cid && Boolean(visibleMask[nbId]),
            ),
          );
          if (!hasVisibleNeighbor) {
            visibleMask[node.id] = 0;
          }
        }
      }
    }

    for (const node of nodes) {
      if (visibleMask[node.id]) {
        visibleNodeCount += 1;
      } else if (
        this.options.mergeEquivalenceGroups &&
        (node.eqGroupSize || 1) >= 2 &&
        node.eqRepId !== null &&
        node.eqRepId !== undefined &&
        node.eqRepId !== node.id &&
        visibleMask[node.eqRepId]
      ) {
        hiddenEqByRep.set(node.eqRepId, (hiddenEqByRep.get(node.eqRepId) || 0) + 1);
      } else if (
        node.isSingleLeaf &&
        node.leafParentId !== null &&
        node.leafParentId !== undefined &&
        visibleMask[node.leafParentId]
      ) {
        foldedNodeCount += 1;
        hiddenLeavesByParent.set(
          node.leafParentId,
          (hiddenLeavesByParent.get(node.leafParentId) || 0) + 1,
        );
      }
    }
    this.lastVisibleMask = visibleMask;

    const resolveEndpoint = (origId: number): number | null => {
      if (visibleMask[origId]) return origId;
      if (this.options.mergeEquivalenceGroups) {
        const rep = nodes[origId]?.eqRepId;
        if (rep !== null && rep !== undefined && visibleMask[rep]) {
          return rep;
        }
      }
      return null;
    };

    const proxyQuad = (a: GraphNode, b: GraphNode, pe: PreparedEdge) => {
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const bend = pe.cross ? 0.055 : 0.025;
      const cpx = (a.x + b.x) / 2 - dy * bend;
      const cpy = (a.y + b.y) / 2 + dx * bend;
      const tx = b.x - cpx;
      const ty = b.y - cpy;
      const tLen = Math.hypot(tx, ty) || 1;
      return {
        cpx,
        cpy,
        ux: tx / tLen,
        uy: ty / tLen,
        path: new Path2D(`M${a.x},${a.y} Q${cpx},${cpy} ${b.x},${b.y}`),
      };
    };

    // Background edges (separating HAS_ATTITUDE sentiments + routing through equivalence proxies)
    const budget = this.options.edgeBudget > 0 ? this.options.edgeBudget : Infinity;
    const hasPath2D = typeof Path2D !== "undefined";
    const batchLocal = hasPath2D ? new Path2D() : null;
    const batchCross = hasPath2D ? new Path2D() : null;
    const batchHub = hasPath2D ? new Path2D() : null;
    const batchAttLike = hasPath2D ? new Path2D() : null;
    const batchAttDislike = hasPath2D ? new Path2D() : null;
    const batchAttNeutral = hasPath2D ? new Path2D() : null;
    const drawnEdgeIds = new Set<number>();
    const activeAttitudeReps = new Set<number>();
    let backgroundDrawn = 0;

    for (const edgeId of this.edgeOrder) {
      const pe = this.preparedEdges[edgeId]!;
      const isAtt = pe.relType === "HAS_ATTITUDE";
      if (!isAtt && backgroundDrawn >= budget) continue;
      if (pe.cross && !this.options.showCrossEdges && !(isAtt && this.options.highlightAttitudes)) continue;
      const va = resolveEndpoint(pe.a);
      const vb = resolveEndpoint(pe.b);
      if (va === null || vb === null) continue;
      if (va === vb && pe.a !== pe.b) continue;
      const a = nodes[va]!;
      const b = nodes[vb]!;
      if (isAtt) {
        activeAttitudeReps.add(va);
        activeAttitudeReps.add(vb);
      }
      if (!inViewport(a) || !inViewport(b)) continue;
      if (
        !isAtt &&
        scale > 1.65 &&
        (pe.cross || Math.hypot(a.x - b.x, a.y - b.y) * scale > 220)
      ) {
        continue;
      }

      if (hasPath2D) {
        const redirected = va !== pe.a || vb !== pe.b;
        const p2d = redirected ? proxyQuad(a, b, pe).path : pe.path2d;
        if (p2d) {
          if (isAtt) {
            if (pe.sentiment === "讨厌") batchAttDislike?.addPath(p2d);
            else if (pe.sentiment === "中立" || pe.sentiment === "不明确") {
              batchAttNeutral?.addPath(p2d);
            } else batchAttLike?.addPath(p2d);
          } else if (pe.hub) {
            batchHub?.addPath(p2d);
          } else if (pe.cross) {
            batchCross?.addPath(p2d);
          } else {
            batchLocal?.addPath(p2d);
          }
        }
      }
      drawnEdgeIds.add(edgeId);
      if (!isAtt) backgroundDrawn += 1;
    }

    const baseStrength = Math.min(1, Math.max(0.05, this.options.edgeOpacity));
    const attDim = this.options.highlightAttitudes ? 0.16 : 1;
    const dimFactor = (1 - 0.86 * this.focusDim) * (hasSearch ? 0.35 : 1) * attDim;
    ctx.lineWidth = 0.65 / scale;

    const strokeBatch = (path: Path2D | null, color: string, mult: number) => {
      if (!path) return;
      ctx.strokeStyle = color;
      ctx.globalAlpha = baseStrength * mult * dimFactor;
      ctx.stroke(path);
    };

    if (hasPath2D) {
      strokeBatch(batchHub, surface.edgeBase, 0.14);
      strokeBatch(batchCross, surface.edgeCross, 0.36);
      strokeBatch(batchLocal, surface.edgeBase, 1.0);

      const attFocusAlpha = this.options.highlightAttitudes
        ? Math.min(0.92, (1 - 0.72 * this.focusDim) * (hasSearch ? 0.4 : 0.88))
        : Math.min(0.85, baseStrength * 1.45 * (1 - 0.8 * this.focusDim));
      ctx.lineWidth = (this.options.highlightAttitudes ? 1.25 : 0.82) / scale;
      if (batchAttNeutral) {
        ctx.strokeStyle = surface.attitudeNeutral;
        ctx.globalAlpha = attFocusAlpha * 0.72;
        ctx.stroke(batchAttNeutral);
      }
      if (batchAttLike) {
        ctx.strokeStyle = surface.attitudeLike;
        ctx.globalAlpha = attFocusAlpha;
        ctx.stroke(batchAttLike);
      }
      if (batchAttDislike) {
        ctx.strokeStyle = surface.attitudeDislike;
        ctx.globalAlpha = attFocusAlpha;
        ctx.setLineDash([4 / scale, 3 / scale]);
        ctx.stroke(batchAttDislike);
        ctx.setLineDash([]);
      }
      ctx.globalAlpha = 1;
    }

    // Dotted equivalence-group pairings between visible sibling Concepts
    if (this.equivalencePairs.length > 0 && (scale >= 1.05 || activeId !== null || hasSearch || !this.options.mergeEquivalenceGroups)) {
      ctx.strokeStyle = surface.milestoneGlow;
      ctx.lineWidth = 0.8 / scale;
      ctx.setLineDash([2.2 / scale, 3 / scale]);
      for (const pair of this.equivalencePairs) {
        if (!visibleMask[pair.a] || !visibleMask[pair.b]) continue;
        const a = nodes[pair.a]!;
        const b = nodes[pair.b]!;
        if (!inViewport(a) && !inViewport(b)) continue;
        const touchesFocus =
          activeId !== null && (pair.a === activeId || pair.b === activeId);
        if (scale < 1.05 && this.options.mergeEquivalenceGroups && !touchesFocus && !hasSearch) continue;
        ctx.globalAlpha = touchesFocus ? 0.8 : 0.38;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
    }

    // Highlighted edges for selected / hovered node + directional arrowheads + sentiment badges
    const sizeScale = this.options.nodeSize;
    const activeAttitudeEdges: Array<{
      pe: PreparedEdge;
      a: GraphNode;
      b: GraphNode;
      cpx: number;
      cpy: number;
      ux: number;
      uy: number;
      path: Path2D;
      alpha: number;
    }> = [];
    if (this.edgeAlpha.size > 0 && hasPath2D) {
      const buckets = Array.from({ length: 12 }, () => ({
        path: new Path2D(),
        arrows: new Path2D(),
        count: 0,
      }));
      for (const [edgeId, alpha] of this.edgeAlpha) {
        if (alpha < 0.04) continue;
        const pe = this.preparedEdges[edgeId];
        if (!pe || !pe.path2d) continue;
        const va = resolveEndpoint(pe.a);
        const vb = resolveEndpoint(pe.b);
        if (va === null || vb === null) continue;
        if (va === vb && pe.a !== pe.b) continue;
        const a = nodes[va]!;
        const b = nodes[vb]!;
        const redirected = va !== pe.a || vb !== pe.b;
        const q = redirected
          ? proxyQuad(a, b, pe)
          : { cpx: pe.cpx, cpy: pe.cpy, ux: pe.ux, uy: pe.uy, path: pe.path2d };
        if (pe.relType === "HAS_ATTITUDE") {
          activeAttitudeEdges.push({ pe, a, b, ...q, alpha });
          drawnEdgeIds.add(edgeId);
          continue;
        }
        if (this.options.highlightAttitudes) continue;
        if (!redirected && (pe.right < left || pe.left > right || pe.bottom < top || pe.top > bottom)) {
          continue;
        }
        const bucket = buckets[Math.min(11, Math.round(alpha * 11))]!;
        bucket.path.addPath(q.path);
        if (!pe.selfLoop) {
          const gap = b.r * sizeScale + 4 / Math.max(0.75, scale);
          const ax = b.x - q.ux * gap;
          const ay = b.y - q.uy * gap;
          const head = 4.2 / Math.max(0.75, Math.min(1.6, scale));
          const wing = 2.1 / Math.max(0.75, Math.min(1.6, scale));
          bucket.arrows.addPath(
            new Path2D(
              `M${ax - q.ux * head - q.uy * wing},${ay - q.uy * head + q.ux * wing} L${ax},${ay} L${ax - q.ux * head + q.uy * wing},${ay - q.uy * head - q.ux * wing}`,
            ),
          );
        }
        bucket.count += 1;
        drawnEdgeIds.add(edgeId);
      }

      ctx.strokeStyle = surface.edgeActive;
      ctx.lineWidth = 1.05 / scale;
      for (let i = 0; i < buckets.length; i += 1) {
        const bucket = buckets[i]!;
        if (bucket.count === 0) continue;
        ctx.globalAlpha = (i / 11) * 0.56;
        ctx.stroke(bucket.path);
        ctx.stroke(bucket.arrows);
      }

      // Highlighted HAS_ATTITUDE edges rendered in their sentiment color + collision-free micro pills
      const placedAttBoxes: Array<{ x: number; y: number; w: number; h: number }> = [];
      for (const { pe, a, b, cpx, cpy, ux, uy, path, alpha } of activeAttitudeEdges) {
        const attColor = sentimentColor(pe.relType, pe.sentiment, surface, surface.edgeActive);
        ctx.strokeStyle = attColor;
        ctx.lineWidth = 1.45 / scale;
        ctx.globalAlpha = alpha * 0.9;
        if (pe.sentiment === "讨厌") ctx.setLineDash([4 / scale, 3 / scale]);
        ctx.stroke(path);
        ctx.setLineDash([]);
        if (!pe.selfLoop) {
          const gap = b.r * sizeScale + 4 / Math.max(0.75, scale);
          const ax = b.x - ux * gap;
          const ay = b.y - uy * gap;
          const head = 4.6 / Math.max(0.75, Math.min(1.6, scale));
          const wing = 2.3 / Math.max(0.75, Math.min(1.6, scale));
          ctx.beginPath();
          ctx.moveTo(ax - ux * head - uy * wing, ay - uy * head + ux * wing);
          ctx.lineTo(ax, ay);
          ctx.lineTo(ax - ux * head + uy * wing, ay - uy * head - ux * wing);
          ctx.stroke();
        }
        if (this.options.showEdgeLabels && pe.sentiment && scale >= 0.72) {
          const mx = 0.25 * a.x + 0.5 * cpx + 0.25 * b.x;
          const my = 0.25 * a.y + 0.5 * cpy + 0.25 * b.y;
          ctx.font = `600 ${9 / scale}px "Inter", "Segoe UI", "Microsoft YaHei", sans-serif`;
          const tw = ctx.measureText(pe.sentiment).width + 8 / scale;
          const th = 12 / scale;
          const pBox = { x: mx - tw / 2, y: my - th / 2, w: tw, h: th };
          const overlaps = placedAttBoxes.some(
            (o) =>
              pBox.x < o.x + o.w &&
              pBox.x + pBox.w > o.x &&
              pBox.y < o.y + o.h &&
              pBox.y + pBox.h > o.y,
          );
          if (!overlaps) {
            placedAttBoxes.push(pBox);
            ctx.globalAlpha = Math.min(0.95, alpha);
            ctx.fillStyle = surface.edgeLabelBg;
            drawRoundedRect(ctx, pBox.x, pBox.y, pBox.w, pBox.h, 3 / scale);
            ctx.fill();
            ctx.fillStyle = attColor;
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(pe.sentiment, mx, my);
          }
        }
      }
    }
    ctx.globalAlpha = 1;

    // Nodes
    const matchedIds = new Set<number>();
    for (const node of nodes) {
      if (!visibleMask[node.id]) continue;
      const isMatch = hasSearch && node.name.toLowerCase().includes(query);
      if (isMatch) matchedIds.add(node.id);
      if (!inViewport(node)) continue;

      const isSelected = node.id === this.options.selectedId;
      const isHovered = node.id === this.options.hoveredId;
      const inNeighborhood = activeNeighborhood ? activeNeighborhood.has(node.id) : true;
      const dimBySearch = hasSearch && !isMatch && !isSelected && !isHovered;

      let alpha = this.nodeAlpha[node.id] ?? 1;
      if (dimBySearch) alpha = Math.min(alpha, 0.16);
      else if (
        this.options.highlightAttitudes &&
        !this.attitudeEndpoints.has(node.id) &&
        !activeAttitudeReps.has(node.id) &&
        !isSelected &&
        !isHovered
      ) {
        alpha = Math.min(alpha, 0.22);
      } else if (isMatch || isSelected || isHovered || (activeNeighborhood && inNeighborhood)) {
        alpha = 1;
      }

      const r = node.r * sizeScale;
      const ring = this.ringAlpha[node.id] ?? 0;
      ctx.globalAlpha = alpha;

      if (node.anchor || node.type === "Event" || ring > 0.05 || isMatch) {
        const sprite = this.getNodeSprite(node, palette, surface);
        if (sprite) {
          const drawSize = sprite.size * sizeScale;
          ctx.drawImage(sprite.canvas, node.x - drawSize / 2, node.y - drawSize / 2, drawSize, drawSize);
        }
      } else {
        ctx.fillStyle = palette[node.type] || palette.Other;
        ctx.beginPath();
        if (node.type === "Location") {
          const d = Math.max(0.8 / scale, r);
          ctx.moveTo(node.x, node.y - d);
          ctx.lineTo(node.x + d, node.y);
          ctx.lineTo(node.x, node.y + d);
          ctx.lineTo(node.x - d, node.y);
          ctx.closePath();
        } else {
          ctx.arc(node.x, node.y, Math.max(0.6 / scale, r), 0, Math.PI * 2);
        }
        ctx.fill();
      }

      if (ring > 0.01 || isMatch) {
        ctx.globalAlpha = isMatch && ring <= 0.01 ? 0.85 : Math.max(ring, isMatch ? 0.85 : 0);
        ctx.strokeStyle = isMatch && !isSelected && !isHovered ? surface.searchRing : surface.highlightRing;
        ctx.lineWidth = (isSelected ? 1.5 : 1.1) / scale;
        ctx.beginPath();
        ctx.arc(node.x, node.y, r + 4.5 / scale, 0, Math.PI * 2);
        ctx.stroke();
      }

      // Draw crisp equivalence group halo + "≡N" pill on merged representative Concepts
      const hiddenEqCount = hiddenEqByRep.get(node.id) || 0;
      if ((hiddenEqCount > 0 || ((node.eqGroupSize || 1) >= 2 && node.eqRepId === node.id)) && !this.options.highlightAttitudes) {
        ctx.globalAlpha = alpha * (hiddenEqCount > 0 ? 0.72 : 0.42);
        ctx.strokeStyle = surface.milestoneGlow;
        ctx.lineWidth = 0.85 / scale;
        ctx.beginPath();
        ctx.arc(node.x, node.y, r + 3.4 / Math.max(0.7, scale), 0, Math.PI * 2);
        ctx.stroke();

        if (hiddenEqCount > 0 && (scale >= 0.55 || isHovered || isSelected)) {
          const tag = `≡${node.eqGroupSize}`;
          const bx = node.x - r - 4 / scale;
          const by = node.y - r - 2 / scale;
          ctx.globalAlpha = Math.min(0.95, alpha * 0.92);
          ctx.font = `600 ${8.5 / scale}px ui-monospace, monospace`;
          const tw = ctx.measureText(tag).width;
          const pw = tw + 5 / scale;
          const ph = 11 / scale;
          ctx.fillStyle = surface.badgeBg;
          drawRoundedRect(ctx, bx - pw / 2, by - ph / 2, pw, ph, 3 / scale);
          ctx.fill();
          ctx.fillStyle = surface.milestoneGlow;
          ctx.textBaseline = "middle";
          ctx.textAlign = "center";
          ctx.fillText(tag, bx, by);
        }
      }

      // Draw subtle "+N" leaf-cluster ring or pill on Events with folded single leaves
      const hiddenCount = hiddenLeavesByParent.get(node.id) || 0;
      if (hiddenCount > 0) {
        ctx.globalAlpha = alpha * 0.55;
        ctx.strokeStyle = palette[node.type] || palette.Event;
        ctx.lineWidth = 0.8 / scale;
        ctx.setLineDash([1.6 / scale, 2.2 / scale]);
        ctx.beginPath();
        ctx.arc(node.x, node.y, r + 3.2 / Math.max(0.7, scale), 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);

        if (scale >= 0.72 && (hiddenCount >= 2 || isHovered || isSelected)) {
          const tag = `+${hiddenCount}`;
          const bx = node.x + r + 3 / scale;
          const by = node.y - r - 2 / scale;
          ctx.globalAlpha = Math.min(0.95, alpha * 0.9);
          ctx.font = `600 ${8.5 / scale}px ui-monospace, monospace`;
          const tw = ctx.measureText(tag).width;
          const pw = tw + 5 / scale;
          const ph = 11 / scale;
          ctx.fillStyle = surface.badgeBg;
          drawRoundedRect(ctx, bx - pw / 2, by - ph / 2, pw, ph, 3 / scale);
          ctx.fill();
          ctx.fillStyle = surface.badgeText;
          ctx.textBaseline = "middle";
          ctx.textAlign = "center";
          ctx.fillText(tag, bx, by);
        }
      }
    }
    ctx.globalAlpha = 1;

    // Shared spatial grid for collision-free cluster titles & node labels
    const labelCell = 30 / scale;
    const placedBoxes = new Map<string, Array<{ x: number; y: number; w: number; h: number }>>();
    const cellKeys = (box: { x: number; y: number; w: number; h: number }) => {
      const keys: string[] = [];
      const x0 = Math.floor(box.x / labelCell);
      const x1 = Math.floor((box.x + box.w) / labelCell);
      const y0 = Math.floor(box.y / labelCell);
      const y1 = Math.floor((box.y + box.h) / labelCell);
      for (let x = x0; x <= x1; x += 1) {
        for (let y = y0; y <= y1; y += 1) {
          keys.push(`${x}:${y}`);
        }
      }
      return keys;
    };
    const boxCollides = (box: { x: number; y: number; w: number; h: number }, keys: string[]) => {
      for (const k of keys) {
        const bucket = placedBoxes.get(k);
        if (!bucket) continue;
        for (const other of bucket) {
          if (
            box.x < other.x + other.w &&
            box.x + box.w > other.x &&
            box.y < other.y + other.h &&
            box.y + box.h > other.y
          ) {
            return true;
          }
        }
      }
      return false;
    };
    const recordBox = (box: { x: number; y: number; w: number; h: number }, keys: string[]) => {
      for (const k of keys) {
        const bucket = placedBoxes.get(k);
        if (bucket) bucket.push(box);
        else placedBoxes.set(k, [box]);
      }
    };

    // Semantic Cluster Watermarks (collision-checked, fade out smoothly past scale 1.35)
    if (
      this.options.showClusters &&
      this.data.analysis.clear &&
      scale < 1.35 &&
      !hasSearch &&
      activeId === null
    ) {
      const titleFade = Math.max(0, Math.min(1, (1.35 - scale) / 0.55));
      if (titleFade > 0.04) {
        const titleFontPx = 11 / scale;
        ctx.font = `600 ${titleFontPx}px "Inter", "Segoe UI", "Microsoft YaHei", sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.lineWidth = 3.5 / scale;
        ctx.lineJoin = "round";
        ctx.strokeStyle = surface.labelHalo;

        for (const cluster of this.data.clusters) {
          if (cluster.loose || !cluster.title || cluster.size < 6) continue;
          if (
            cluster.x + cluster.rx < left ||
            cluster.x - cluster.rx > right ||
            cluster.y + cluster.ry < top ||
            cluster.y - cluster.ry > bottom
          ) {
            continue;
          }
          const ty = cluster.y - Math.min(cluster.ry * 0.72, 22 / scale);
          const tw = ctx.measureText(cluster.title).width + 10 / scale;
          const th = 16 / scale;
          const box = { x: cluster.x - tw / 2, y: ty - th / 2, w: tw, h: th };
          const keys = cellKeys(box);
          if (boxCollides(box, keys)) continue;
          recordBox(box, keys);

          ctx.globalAlpha = titleFade * 0.68;
          ctx.strokeText(cluster.title, cluster.x, ty);
          ctx.fillStyle = surface.clusterTitle;
          ctx.fillText(cluster.title, cluster.x, ty);
        }
        ctx.globalAlpha = 1;
      }
    }

    // Spatial-grid collision-free node labels
    if (this.options.showLabels) {
      const fontPx = 11 / scale;
      ctx.font = `500 ${fontPx}px "Inter", "Segoe UI", "Microsoft YaHei", sans-serif`;
      ctx.textBaseline = "middle";
      ctx.textAlign = "left";
      ctx.lineWidth = 3.2 / scale;
      ctx.lineJoin = "round";
      ctx.strokeStyle = surface.labelHalo;

      const prioritySet = new Set<number>();
      const candidateOrder: number[] = [];
      const pushUnique = (id: number | null) => {
        if (id === null || prioritySet.has(id)) return;
        prioritySet.add(id);
        candidateOrder.push(id);
      };

      pushUnique(this.options.hoveredId);
      pushUnique(this.options.selectedId);
      for (const id of matchedIds) pushUnique(id);
      if (activeNeighborhood) {
        for (const id of activeNeighborhood) pushUnique(id);
      }
      for (const id of this.labelOrder) pushUnique(id);

      let placedCount = 0;
      const maxLabels = 135;

      for (const id of candidateOrder) {
        if (placedCount >= maxLabels) break;
        const node = nodes[id];
        if (!node || !visibleMask[id] || !inViewport(node)) continue;

        const isActive = id === activeId || id === this.options.selectedId;
        const isMatch = matchedIds.has(id);
        const isNeighbor = activeNeighborhood ? activeNeighborhood.has(id) : false;

        let eligible = false;
        if (this.options.highlightAttitudes) {
          eligible =
            isActive ||
            isMatch ||
            (this.attitudeEndpoints.has(node.id) &&
              (!activeNeighborhood || activeNeighborhood.has(node.id)));
        } else if (hasSearch) {
          eligible = isMatch || isActive || isNeighbor;
        } else if (activeNeighborhood) {
          eligible = isActive || isNeighbor;
        } else if (!this.options.showClusters || !this.data.analysis.clear || scale >= 1.15) {
          eligible =
            node.protagonist ||
            node.eventKind === "milestone" ||
            node.anchor ||
            (scale >= 1.25 && node.degree >= 3) ||
            scale >= 2.0;
        } else {
          // Overview zoom: surface protagonist, People, and key life Milestones right away
          eligible = node.protagonist || node.type === "Person" || node.eventKind === "milestone";
        }

        if (!eligible) continue;

        const text = node.name.length > 26 ? `${node.name.slice(0, 25)}…` : node.name;
        const x = node.x + node.r * sizeScale + 6 / scale;
        const y = node.y;
        const estW = ctx.measureText(text).width + 8 / scale;
        const estH = 15 / scale;
        const box = { x, y: y - estH / 2, w: estW, h: estH };
        const keys = cellKeys(box);

        if (!isActive && boxCollides(box, keys)) continue;
        recordBox(box, keys);

        ctx.globalAlpha = isActive || isMatch ? 1 : isNeighbor ? 0.92 : 0.88;
        ctx.fillStyle = isActive || isMatch ? surface.labelActiveText : surface.labelText;
        ctx.strokeText(text, x, y);
        ctx.fillText(text, x, y);
        placedCount += 1;
      }
      ctx.globalAlpha = 1;
    }

    ctx.restore();
    this.reportMetrics(
      drawnEdgeIds.size,
      this.data.edges.length,
      visibleNodeCount,
      foldedNodeCount,
    );
  }

  private renderEgoSubgraph(
    ctx: CanvasRenderingContext2D,
    ego: EgoSubgraph,
    palette: Record<GraphNodeType, string>,
    surface: PaletteSurface,
  ): void {
    const scale = this.camera.scale;
    const sizeScale = this.options.nodeSize;
    ctx.translate(this.camera.tx, this.camera.ty);
    ctx.scale(scale, scale);

    // Hop concentric guide circles
    ctx.strokeStyle = surface.gridLine;
    ctx.lineWidth = 1 / scale;
    ctx.setLineDash([4 / scale, 5 / scale]);
    ctx.beginPath();
    ctx.arc(560, 400, ego.ring1Radius, 0, Math.PI * 2);
    ctx.stroke();
    if (ego.hops === 2 && ego.ring2Radius > ego.ring1Radius) {
      ctx.beginPath();
      ctx.arc(560, 400, ego.ring2Radius, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    const activeId = this.options.hoveredId ?? this.options.selectedId;
    const query = this.options.searchQuery.trim().toLowerCase();
    const hasSearch = query.length > 0;

    // Edges with arrowheads
    const baseOpacity = Math.min(1, Math.max(0.15, this.options.edgeOpacity + 0.18));
    const placedEdgeLabelBoxes: Array<{ x: number; y: number; w: number; h: number }> = [];

    for (const item of ego.edges) {
      const aItem = ego.nodes[item.aIdx]!;
      const bItem = ego.nodes[item.bIdx]!;
      if (!this.options.visibleTypes[aItem.node.type] || !this.options.visibleTypes[bItem.node.type]) {
        continue;
      }

      const touchesActive =
        activeId !== null && (aItem.node.id === activeId || bItem.node.id === activeId);
      const isRootEdge = aItem.hop === 0 || bItem.hop === 0;
      const isAttEdge = item.edge.type === "HAS_ATTITUDE";
      const isEqEdge = item.edge.type === "EQUIVALENCE_SIBLING";
      const baseStroke = isEqEdge
        ? surface.milestoneGlow
        : isAttEdge
          ? sentimentColor(item.edge.type, item.edge.sentiment, surface, surface.edgeBase)
          : touchesActive
            ? surface.edgeActive
            : isRootEdge
              ? surface.edgeBase
              : surface.edgeCross;

      ctx.globalAlpha =
        activeId !== null
          ? touchesActive
            ? 0.94
            : 0.18
          : isAttEdge || isEqEdge
            ? 0.88
            : isRootEdge
              ? Math.min(0.85, baseOpacity * 1.15)
              : baseOpacity * 0.72;
      ctx.strokeStyle = baseStroke;
      ctx.lineWidth = (touchesActive ? 1.5 : isAttEdge || isEqEdge ? 1.25 : isRootEdge ? 1.05 : 0.8) / scale;
      if (isEqEdge || (isAttEdge && item.edge.sentiment === "讨厌")) {
        ctx.setLineDash([4 / scale, 3 / scale]);
      }

      const dx = bItem.x - aItem.x;
      const dy = bItem.y - aItem.y;
      const len = Math.hypot(dx, dy) || 1;
      const bend = 0.04 + (item.curve / len) * (item.aIdx <= item.bIdx ? 1 : -1);
      const cpx = (aItem.x + bItem.x) / 2 - dy * bend;
      const cpy = (aItem.y + bItem.y) / 2 + dx * bend;

      ctx.beginPath();
      if (item.aIdx === item.bIdx) {
        ctx.arc(aItem.x + aItem.r * 1.8, aItem.y - aItem.r * 1.8, aItem.r * 1.5, 0, Math.PI * 2);
      } else {
        ctx.moveTo(aItem.x, aItem.y);
        ctx.quadraticCurveTo(cpx, cpy, bItem.x, bItem.y);
      }
      ctx.stroke();
      ctx.setLineDash([]);

      // Arrowhead (omitted on symmetric equivalence links)
      if (item.aIdx !== item.bIdx && !isEqEdge) {
        const tx = bItem.x - cpx;
        const ty = bItem.y - cpy;
        const tLen = Math.hypot(tx, ty) || 1;
        const ux = tx / tLen;
        const uy = ty / tLen;
        const gap = bItem.r * sizeScale + 4 / Math.max(0.7, scale);
        const ax = bItem.x - ux * gap;
        const ay = bItem.y - uy * gap;
        const head = 5 / Math.max(0.75, Math.min(1.5, scale));
        const wing = 2.5 / Math.max(0.75, Math.min(1.5, scale));
        ctx.beginPath();
        ctx.moveTo(ax - ux * head - uy * wing, ay - uy * head + ux * wing);
        ctx.lineTo(ax, ay);
        ctx.lineTo(ax - ux * head + uy * wing, ay - uy * head - ux * wing);
        ctx.stroke();
      }

      // Edge relation label at quadratic bezier midpoint (with collision avoidance)
      if (this.options.showEdgeLabels && item.aIdx !== item.bIdx && (touchesActive || isRootEdge || isAttEdge || isEqEdge || ego.edges.length <= 24)) {
        const mx = 0.25 * aItem.x + 0.5 * cpx + 0.25 * bItem.x;
        const my = 0.25 * aItem.y + 0.5 * cpy + 0.25 * bItem.y;
        const relText = isEqEdge
          ? "≡ 同义等价"
          : formatRelLabel(item.edge.type, item.edge.sentiment);
        const fontSize = 9.5 / scale;
        ctx.font = `500 ${fontSize}px "Inter", "Segoe UI", "Microsoft YaHei", sans-serif`;
        const w = ctx.measureText(relText).width + 10 / scale;
        const h = 13 / scale;
        const box = { x: mx - w / 2, y: my - h / 2, w, h };

        const overlaps =
          !touchesActive &&
          !isAttEdge &&
          !isEqEdge &&
          placedEdgeLabelBoxes.some(
            (other) =>
              box.x < other.x + other.w &&
              box.x + box.w > other.x &&
              box.y < other.y + other.h &&
              box.y + box.h > other.y,
          );
        if (!overlaps) {
          placedEdgeLabelBoxes.push(box);
          ctx.globalAlpha = touchesActive || isAttEdge || isEqEdge ? 0.96 : activeId !== null ? 0.22 : 0.85;
          ctx.fillStyle = surface.edgeLabelBg;
          drawRoundedRect(ctx, box.x, box.y, box.w, box.h, 3 / scale);
          ctx.fill();
          ctx.fillStyle =
            isAttEdge || isEqEdge
              ? baseStroke
              : touchesActive
                ? surface.labelActiveText
                : surface.edgeLabelText;
          ctx.textBaseline = "middle";
          ctx.textAlign = "center";
          ctx.fillText(relText, mx, my);
        }
      }
    }
    ctx.globalAlpha = 1;

    // Nodes
    const placedLabels: Array<{ x: number; y: number; w: number; h: number }> = [];
    for (const item of ego.nodes) {
      const node = item.node;
      if (!this.options.visibleTypes[node.type]) continue;
      const isSelected = node.id === this.options.selectedId;
      const isHovered = node.id === this.options.hoveredId;
      const isRoot = item.hop === 0;
      const isMatch = hasSearch && node.name.toLowerCase().includes(query);

      const dimmed =
        (hasSearch && !isMatch && !isSelected && !isHovered && !isRoot) ||
        (activeId !== null &&
          node.id !== activeId &&
          !ego.edges.some(
            (e) =>
              (ego.nodes[e.aIdx]!.node.id === activeId && ego.nodes[e.bIdx]!.node.id === node.id) ||
              (ego.nodes[e.bIdx]!.node.id === activeId && ego.nodes[e.aIdx]!.node.id === node.id),
          ));

      ctx.globalAlpha = dimmed ? 0.24 : 1;
      const drawNode: GraphNode = { ...node, x: item.x, y: item.y, r: item.r, anchor: true };
      const sprite = this.getNodeSprite(drawNode, palette, surface);
      if (sprite) {
        const drawSize = sprite.size * sizeScale;
        ctx.drawImage(sprite.canvas, item.x - drawSize / 2, item.y - drawSize / 2, drawSize, drawSize);
      }

      if (isSelected || isHovered || isRoot || isMatch) {
        ctx.strokeStyle = isMatch && !isSelected && !isHovered ? surface.searchRing : surface.highlightRing;
        ctx.lineWidth = (isSelected || isRoot ? 1.7 : 1.2) / scale;
        ctx.beginPath();
        ctx.arc(item.x, item.y, item.r * sizeScale + 4.5 / scale, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    // Outward-radiating labels in Ego mode (left-side nodes align right, top/bottom poles align center)
    if (this.options.showLabels) {
      const fontPx = 11.5 / scale;
      ctx.font = `500 ${fontPx}px "Inter", "Segoe UI", "Microsoft YaHei", sans-serif`;
      ctx.textBaseline = "middle";
      ctx.lineWidth = 3.2 / scale;
      ctx.lineJoin = "round";
      ctx.strokeStyle = surface.labelHalo;

      for (const item of ego.nodes) {
        const node = item.node;
        if (!this.options.visibleTypes[node.type]) continue;
        const isActive =
          node.id === this.options.selectedId ||
          node.id === this.options.hoveredId ||
          item.hop === 0;
        const isMatch = hasSearch && node.name.toLowerCase().includes(query);
        const text = node.name.length > 26 ? `${node.name.slice(0, 25)}…` : node.name;
        const w = ctx.measureText(text).width + 8 / scale;
        const h = 15 / scale;
        const pad = item.r * sizeScale + 7 / scale;

        let align: CanvasTextAlign = "left";
        let lx = item.x + pad;
        let ly = item.y;
        let boxX = lx;

        if (item.hop > 0) {
          if (Math.abs(item.x - 560) <= 22) {
            align = "center";
            lx = item.x;
            ly = item.y + (item.y < 400 ? -pad - 4 / scale : pad + 4 / scale);
            boxX = lx - w / 2;
          } else if (item.x < 560) {
            align = "right";
            lx = item.x - pad;
            boxX = lx - w;
          }
        }

        const box = { x: boxX, y: ly - h / 2, w, h };

        const collides =
          !isActive &&
          !isMatch &&
          placedLabels.some(
            (other) =>
              box.x < other.x + other.w &&
              box.x + box.w > other.x &&
              box.y < other.y + other.h &&
              box.y + box.h > other.y,
          );
        if (collides) continue;
        placedLabels.push(box);

        ctx.textAlign = align;
        ctx.globalAlpha = isActive || isMatch ? 1 : 0.92;
        ctx.fillStyle = isActive || isMatch ? surface.labelActiveText : surface.labelText;
        ctx.strokeText(text, lx, ly);
        ctx.fillText(text, lx, ly);
      }
      ctx.textAlign = "left";
    }

    ctx.globalAlpha = 1;
    this.reportMetrics(ego.edges.length, ego.edges.length, ego.nodes.length, 0);
  }

  private reportMetrics(
    drawnEdges: number,
    totalEdges: number,
    visibleNodes: number,
    foldedNodes: number,
  ): void {
    const scale = this.camera.scale;
    const key = `${scale.toFixed(2)}:${drawnEdges}:${totalEdges}:${visibleNodes}:${foldedNodes}`;
    if (key === this.lastMetricsKey) return;
    this.lastMetricsKey = key;
    this.metricsCb?.({ scale, drawnEdges, totalEdges, visibleNodes, foldedNodes });
  }
}
