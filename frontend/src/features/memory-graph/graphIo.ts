import type {
  EdgeSentiment,
  EventSubType,
  GraphEdge,
  GraphNode,
  GraphNodeType,
  RawGraphDocument,
  RawGraphEdge,
  RawGraphNode,
} from "./types";

const KNOWN_EVENT_SUBTYPES = new Set<EventSubType>(["milestone", "travel", "study", "interaction"]);
const KNOWN_SENTIMENTS = new Set<EdgeSentiment>(["喜欢", "讨厌", "中立", "不明确"]);

export const GRAPH_IMPORT_LIMITS = {
  bytes: 20 * 1024 * 1024,
  nodes: 25_000,
  edges: 120_000,
};

const KNOWN_TYPES = new Set<GraphNodeType>(["Concept", "Event", "Person", "Location", "Other"]);

export function unwrapFieldValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (record.value !== undefined) return String(record.value);
    return JSON.stringify(value);
  }
  return String(value);
}

export function getNodeProperty(node: GraphNode, key: string): unknown {
  return node.raw?.[key] ?? node.raw?.properties?.[key];
}

function inferNodeType(raw: RawGraphNode): GraphNodeType {
  const candidates = [
    ...(Array.isArray(raw.labels) ? raw.labels : []),
    raw.label,
    raw.type,
    typeof raw.properties?.label === "string" ? raw.properties.label : undefined,
    typeof raw.id === "string" && raw.id.includes(":") ? raw.id.split(":")[0] : undefined,
  ];
  for (const item of candidates) {
    if (typeof item === "string" && KNOWN_TYPES.has(item as GraphNodeType)) {
      return item as GraphNodeType;
    }
  }
  return "Other";
}

function inferNodeName(raw: RawGraphNode, originalId: string): string {
  const props = raw.properties || {};
  const candidates = [
    raw.name,
    props.name,
    raw.title,
    props.title,
    props.summary,
    raw.summary,
    originalId,
  ];
  for (const candidate of candidates) {
    const text = unwrapFieldValue(candidate).trim();
    if (text) return text;
  }
  return originalId;
}

function inferEventKind(raw: RawGraphNode, nodeType: GraphNodeType): EventSubType | undefined {
  if (nodeType !== "Event") return undefined;
  const candidate = unwrapFieldValue(raw.properties?.type ?? raw.type)
    .trim()
    .toLowerCase();
  if (KNOWN_EVENT_SUBTYPES.has(candidate as EventSubType)) {
    return candidate as EventSubType;
  }
  return undefined;
}

function inferEquivalenceGroup(raw: RawGraphNode, nodeType: GraphNodeType): string | undefined {
  if (nodeType !== "Concept") return undefined;
  const val = unwrapFieldValue(raw.equivalence_group ?? raw.properties?.equivalence_group).trim();
  return val || undefined;
}

function inferEdgeSentiment(rawEdge: RawGraphEdge): EdgeSentiment | undefined {
  const val = unwrapFieldValue(rawEdge.properties?.sentiment ?? rawEdge.sentiment).trim();
  if (KNOWN_SENTIMENTS.has(val as EdgeSentiment)) {
    return val as EdgeSentiment;
  }
  return undefined;
}

export function normalizeGraphDocument(rawDoc: RawGraphDocument): {
  nodes: GraphNode[];
  edges: GraphEdge[];
  document: { nodes: RawGraphNode[]; relationships: RawGraphEdge[] };
} {
  if (!rawDoc || typeof rawDoc !== "object") {
    throw new Error("图谱数据格式无效：需要 JSON 对象。");
  }

  const rawNodes = Array.isArray(rawDoc.nodes) ? rawDoc.nodes : [];
  const rawEdges = Array.isArray(rawDoc.relationships)
    ? rawDoc.relationships
    : Array.isArray(rawDoc.edges)
      ? rawDoc.edges
      : [];

  if (rawNodes.length > GRAPH_IMPORT_LIMITS.nodes) {
    throw new Error(`节点数量超过上限（${GRAPH_IMPORT_LIMITS.nodes.toLocaleString()}）。`);
  }
  if (rawEdges.length > GRAPH_IMPORT_LIMITS.edges) {
    throw new Error(`关系数量超过上限（${GRAPH_IMPORT_LIMITS.edges.toLocaleString()}）。`);
  }

  const idToIndex = new Map<string, number>();
  const nodes: GraphNode[] = [];

  for (const raw of rawNodes) {
    if (!raw || typeof raw !== "object" || raw.id === undefined || raw.id === null) continue;
    const originalId = String(raw.id);
    if (idToIndex.has(originalId)) continue;
    const id = nodes.length;
    idToIndex.set(originalId, id);
    const type = inferNodeType(raw);
    const eventKind = inferEventKind(raw, type);
    const equivalenceGroup = inferEquivalenceGroup(raw, type);
    nodes.push({
      id,
      originalId,
      name: inferNodeName(raw, originalId),
      type,
      ...(eventKind ? { eventKind } : {}),
      ...(equivalenceGroup ? { equivalenceGroup } : {}),
      c: 0,
      x: 560,
      y: 400,
      r: type === "Person" ? 6 : eventKind === "milestone" ? 6.5 : 4,
      degree: 0,
      anchor: eventKind === "milestone",
      protagonist: false,
      bridge: false,
      raw,
    });
  }

  const edges: GraphEdge[] = [];
  for (const rawEdge of rawEdges) {
    if (!rawEdge || typeof rawEdge !== "object") continue;
    const srcRaw = rawEdge.source ?? rawEdge.start ?? rawEdge.from ?? rawEdge.startNode;
    const dstRaw = rawEdge.target ?? rawEdge.end ?? rawEdge.to ?? rawEdge.endNode;
    if (srcRaw === undefined || dstRaw === undefined) continue;
    const a = idToIndex.get(String(srcRaw));
    const b = idToIndex.get(String(dstRaw));
    if (a === undefined || b === undefined) continue;
    const sentiment = inferEdgeSentiment(rawEdge);
    edges.push({
      id: edges.length,
      a,
      b,
      type: String(rawEdge.type || rawEdge.label || "RELATED"),
      ...(sentiment ? { sentiment } : {}),
      cross: false,
      hub: false,
      curve: 0,
      loop: 0,
      raw: rawEdge,
    });
  }

  return {
    nodes,
    edges,
    document: {
      nodes: nodes.map((n) => n.raw),
      relationships: rawEdges,
    },
  };
}

export function parseGraphJsonText(
  text: string,
  filename = "graph.json",
): {
  nodes: GraphNode[];
  edges: GraphEdge[];
  document: { nodes: RawGraphNode[]; relationships: RawGraphEdge[] };
} {
  if (/\.dump$/i.test(filename)) {
    throw new Error(".dump 不能直接导入。请先恢复数据库并导出 JSON。");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error("无法解析 JSON 文件，请确认格式有效。");
  }

  // Handle wrapped arrays/records exported from spreadsheets/Cypher if needed.
  if (Array.isArray(parsed)) {
    const first = parsed[0] as RawGraphDocument | undefined;
    if (first && typeof first === "object" && (Array.isArray(first.nodes) || Array.isArray(first.edges))) {
      return normalizeGraphDocument(first);
    }
    throw new Error("无法识别的 JSON 数组结构：请导出包含 nodes 与 relationships 字段的对象。");
  }

  return normalizeGraphDocument(parsed as RawGraphDocument);
}

export function serializeGraphDocument(doc: {
  nodes: RawGraphNode[];
  relationships: RawGraphEdge[];
}): string {
  return JSON.stringify(doc, null, 2);
}
