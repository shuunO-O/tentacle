export type GraphNodeType = "Concept" | "Event" | "Person" | "Location" | "Other";
export type EventSubType = "milestone" | "travel" | "study" | "interaction";
export type EventSubTypeFilter = Record<EventSubType | "other", boolean>;
export type EdgeSentiment = "喜欢" | "讨厌" | "中立" | "不明确";

export type GraphViewMode = "global" | "ego";
export type LeafFoldMode = "auto" | "always" | "off";

export interface RawGraphNode {
  id: string | number;
  label?: string;
  labels?: string[];
  name?: string;
  title?: string;
  type?: string;
  equivalence_group?: string;
  properties?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface RawGraphEdge {
  source?: string | number;
  target?: string | number;
  from?: string | number;
  to?: string | number;
  start?: string | number;
  end?: string | number;
  startNode?: string | number;
  endNode?: string | number;
  type?: string;
  label?: string;
  properties?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface RawGraphDocument {
  ok?: boolean;
  nodes?: RawGraphNode[];
  relationships?: RawGraphEdge[];
  edges?: RawGraphEdge[];
}

export interface GraphNode {
  id: number;
  originalId: string;
  name: string;
  type: GraphNodeType;
  eventKind?: EventSubType;
  equivalenceGroup?: string;
  c: number;
  x: number;
  y: number;
  r: number;
  degree: number;
  anchor: boolean;
  protagonist: boolean;
  bridge: boolean;
  isSingleLeaf?: boolean;
  leafParentId?: number | null;
  foldedLeafCount?: number;
  eqRepId?: number | null;
  eqGroupSize?: number;
  raw: RawGraphNode;
}

export interface GraphEdge {
  id: number;
  a: number;
  b: number;
  type: string;
  sentiment?: EdgeSentiment;
  cross: boolean;
  hub: boolean;
  curve: number;
  loop: number;
  raw: RawGraphEdge;
}

export interface GraphCluster {
  id: number;
  x: number;
  y: number;
  rx: number;
  ry: number;
  angle: number;
  loose: boolean;
  size: number;
  keywords?: string[];
  title?: string;
}

export interface GraphAnalysis {
  adj: number[][];
  hubs: number[];
  communities: number[];
  groups: number[][];
  clear: boolean;
  q: number;
  baseline: number;
  margin: number;
  correctHubs: boolean;
  requestedCenterId: number | null;
  centerId: number | null;
  singleLeafCount?: number;
  equivalenceGroupCount?: number;
  equivalenceGroups?: number[][];
  bounds: {
    cx: number;
    cy: number;
    radius: number;
    minX: number;
    maxX: number;
    minY: number;
    maxY: number;
  };
}

export interface ComputedGraphResult {
  nodes: GraphNode[];
  edges: GraphEdge[];
  analysis: GraphAnalysis;
  clusters: GraphCluster[];
}

export interface EgoLayoutNode {
  node: GraphNode;
  hop: 0 | 1 | 2;
  x: number;
  y: number;
  r: number;
}

export interface EgoLayoutEdge {
  edge: GraphEdge;
  aIdx: number;
  bIdx: number;
  curve: number;
}

export interface EgoSubgraph {
  rootId: number;
  hops: 1 | 2;
  nodes: EgoLayoutNode[];
  edges: EgoLayoutEdge[];
  nodeIndexById: Map<number, number>;
  ring1Radius: number;
  ring2Radius: number;
  truncated: boolean;
  totalReachable: number;
}

export type GraphPalette = Record<GraphNodeType, string>;

export const DARK_GRAPH_PALETTE: GraphPalette = {
  Concept: "#7fd6ba",
  Event: "#e9bb72",
  Person: "#b6a9ef",
  Location: "#e5a4ba",
  Other: "#9fb6c4",
};

export const LIGHT_GRAPH_PALETTE: GraphPalette = {
  Concept: "#3f8f73",
  Event: "#a97c3c",
  Person: "#7967b3",
  Location: "#b26d89",
  Other: "#6c7f8b",
};

export const DARK_CLUSTER_TINTS = [
  "#7aa899",
  "#7b8fa8",
  "#9c8ec4",
  "#c49c6e",
  "#bf8596",
  "#8eab70",
];

export const LIGHT_CLUSTER_TINTS = [
  "#3f8f73",
  "#496ea3",
  "#7363b8",
  "#a47a43",
  "#a8667b",
  "#648f48",
];

export const TYPE_DISPLAY_NAMES: Record<GraphNodeType, string> = {
  Concept: "概念",
  Event: "事件",
  Person: "人物",
  Location: "地点",
  Other: "其他",
};

export const EVENT_SUBTYPE_NAMES: Record<EventSubType, string> = {
  milestone: "人生里程碑",
  travel: "出行游历",
  study: "学业事项",
  interaction: "社交互动",
};

export const EVENT_SUBTYPE_SHORT_NAMES: Record<EventSubType, string> = {
  milestone: "里程碑",
  travel: "出行",
  study: "学业",
  interaction: "互动",
};

export const REL_TYPE_LABELS: Record<string, string> = {
  PARTICIPATED_IN: "参与",
  MENTIONS: "提及",
  OCCURRED_AT: "地点",
  HAS_ATTITUDE: "态度",
};
