import { getNodeProperty, unwrapFieldValue } from "./graphIo";
import type {
  ComputedGraphResult,
  EgoLayoutEdge,
  EgoLayoutNode,
  EgoSubgraph,
  GraphEdge,
  GraphNode,
  GraphNodeType,
} from "./types";

const TYPE_ORDER: Record<GraphNodeType, number> = {
  Person: 0,
  Location: 1,
  Event: 2,
  Concept: 3,
  Other: 4,
};

function nodeTimestamp(node: GraphNode): number {
  const raw =
    getNodeProperty(node, "create_time") ??
    getNodeProperty(node, "created_time") ??
    getNodeProperty(node, "start_time");
  const str = unwrapFieldValue(raw).trim();
  if (!str) return 0;
  const parsed = Date.parse(str);
  return Number.isFinite(parsed) ? parsed : 0;
}

export function buildEgoSubgraph(
  graph: ComputedGraphResult,
  rootId: number,
  hops: 1 | 2,
  visibleTypes: Record<GraphNodeType, boolean>,
): EgoSubgraph | null {
  const root = graph.nodes[rootId];
  if (!root) return null;

  const cx = 560;
  const cy = 400;

  // Build unique neighbor lists sorted by relevance (Persons/Locations -> latest Events -> high-degree Concepts)
  const rankNeighbor = (a: GraphNode, b: GraphNode) =>
    TYPE_ORDER[a.type] - TYPE_ORDER[b.type] ||
    nodeTimestamp(b) - nodeTimestamp(a) ||
    b.degree - a.degree ||
    a.id - b.id;

  // Include equivalence-group siblings on Ring 1 and pull their shared memories seamlessly
  const eqSiblings =
    root.type === "Concept" && (root.eqGroupSize || 1) >= 2 && root.equivalenceGroup
      ? graph.nodes.filter(
          (n) =>
            n.id !== rootId &&
            n.type === "Concept" &&
            n.equivalenceGroup === root.equivalenceGroup &&
            visibleTypes[n.type],
        )
      : [];
  const rootGroupIds = [rootId, ...eqSiblings.map((s) => s.id)];
  const rootGroupSet = new Set(rootGroupIds);

  const hop1IdsRaw = new Set<number>();
  for (const s of eqSiblings) hop1IdsRaw.add(s.id);
  for (const gid of rootGroupIds) {
    for (const nbId of graph.analysis.adj[gid] || []) {
      if (nbId === rootId) continue;
      const nb = graph.nodes[nbId];
      if (!nb || !visibleTypes[nb.type]) continue;
      hop1IdsRaw.add(nbId);
    }
  }

  const hop1Candidates = [...hop1IdsRaw]
    .map((id) => graph.nodes[id]!)
    .sort((a, b) => {
      const aEq = rootGroupSet.has(a.id) ? 1 : 0;
      const bEq = rootGroupSet.has(b.id) ? 1 : 0;
      return bEq - aEq || rankNeighbor(a, b);
    });

  const maxHop1 = hops === 1 ? 110 : 48;
  const hop1Nodes = hop1Candidates.slice(0, maxHop1);
  const includedIds = new Set<number>([rootId, ...hop1Nodes.map((n) => n.id)]);
  let truncated = hop1Candidates.length > hop1Nodes.length;
  let totalReachable = hop1Candidates.length;

  const hop2Parents = new Map<number, number[]>();
  const hop2Nodes: GraphNode[] = [];

  if (hops === 2) {
    const hop2CandidateIds = new Set<number>();
    const maxTotalNodes = 140;
    for (const h1 of hop1Nodes) {
      // Avoid exploding a huge Person hub in 2nd hop unless root has very few neighbors.
      if (h1.bridge && hop1Nodes.length > 6) continue;
      for (const nbId of graph.analysis.adj[h1.id] || []) {
        if (includedIds.has(nbId)) continue;
        const nb = graph.nodes[nbId];
        if (!nb || !visibleTypes[nb.type]) continue;
        hop2CandidateIds.add(nbId);
        const parents = hop2Parents.get(nbId);
        if (parents) parents.push(h1.id);
        else hop2Parents.set(nbId, [h1.id]);
      }
    }

    totalReachable += hop2CandidateIds.size;
    const sortedHop2 = [...hop2CandidateIds]
      .map((id) => graph.nodes[id]!)
      .sort(
        (a, b) =>
          (hop2Parents.get(b.id)?.length || 0) - (hop2Parents.get(a.id)?.length || 0) ||
          rankNeighbor(a, b),
      );

    const budget2 = Math.max(0, maxTotalNodes - includedIds.size);
    if (sortedHop2.length > budget2) truncated = true;
    for (const n2 of sortedHop2.slice(0, budget2)) {
      includedIds.add(n2.id);
      hop2Nodes.push(n2);
    }
  }

  const ring1Radius = Math.max(135, Math.min(245, 96 + hop1Nodes.length * 4.6));
  const ring2Radius =
    hop2Nodes.length > 0
      ? ring1Radius + Math.max(135, Math.min(230, 96 + hop2Nodes.length * 2.4))
      : ring1Radius;

  const layoutNodes: EgoLayoutNode[] = [
    {
      node: root,
      hop: 0,
      x: cx,
      y: cy,
      r: Math.max(9.2, root.r * 1.15),
    },
  ];

  // Place Hop-1 nodes evenly around inner ring, grouped by type so related items sit in tidy arcs.
  const hop1Angles = new Map<number, number>();
  const hop1Count = Math.max(1, hop1Nodes.length);
  hop1Nodes.forEach((node, idx) => {
    const theta = -Math.PI / 2 + (idx / hop1Count) * Math.PI * 2;
    const jitterR = hop1Nodes.length > 18 && idx % 2 === 1 ? 22 : 0;
    const radius = ring1Radius + jitterR;
    hop1Angles.set(node.id, theta);
    layoutNodes.push({
      node,
      hop: 1,
      x: cx + Math.cos(theta) * radius,
      y: cy + Math.sin(theta) * radius,
      r: Math.max(4.4, Math.min(7.5, node.r * 1.05)),
    });
  });

  // Place Hop-2 nodes fanned out near the angle of their Hop-1 parents.
  if (hop2Nodes.length > 0) {
    const withAngle = hop2Nodes.map((node) => {
      const parents = hop2Parents.get(node.id) || [];
      let sx = 0;
      let sy = 0;
      for (const pid of parents) {
        const ang = hop1Angles.get(pid);
        if (ang !== undefined) {
          sx += Math.cos(ang);
          sy += Math.sin(ang);
        }
      }
      const baseAngle = sx === 0 && sy === 0 ? 0 : Math.atan2(sy, sx);
      return { node, baseAngle };
    });

    withAngle.sort((a, b) => a.baseAngle - b.baseAngle || a.node.id - b.node.id);

    // Enforce minimum angular separation around outer ring while preserving parent order.
    const minAngleStep = (Math.PI * 2) / Math.max(withAngle.length * 1.12, 16);
    const angles = withAngle.map((item) => item.baseAngle);
    for (let pass = 0; pass < 10; pass += 1) {
      for (let i = 1; i < angles.length; i += 1) {
        const diff = angles[i]! - angles[i - 1]!;
        if (diff < minAngleStep) {
          const push = (minAngleStep - diff) * 0.5;
          angles[i - 1] = angles[i - 1]! - push;
          angles[i] = angles[i]! + push;
        }
      }
    }

    withAngle.forEach(({ node }, idx) => {
      const theta = angles[idx]!;
      const tierOffset = withAngle.length > 26 ? (idx % 3) * 26 : (idx % 2) * 18;
      const radius = ring2Radius + tierOffset;
      layoutNodes.push({
        node,
        hop: 2,
        x: cx + Math.cos(theta) * radius,
        y: cy + Math.sin(theta) * radius,
        r: Math.max(3.6, Math.min(6.4, node.r)),
      });
    });
  }

  // Light local repulsion polish on Hop-1 & Hop-2 nodes to ensure zero overlap.
  for (let pass = 0; pass < 16; pass += 1) {
    for (let i = 1; i < layoutNodes.length; i += 1) {
      const a = layoutNodes[i]!;
      for (let j = i + 1; j < layoutNodes.length; j += 1) {
        const b = layoutNodes[j]!;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.hypot(dx, dy) || 0.1;
        const minSep = a.r + b.r + 22;
        if (dist < minSep) {
          const push = (minSep - dist) * 0.35;
          const ux = dx / dist;
          const uy = dy / dist;
          a.x -= ux * push;
          a.y -= uy * push;
          b.x += ux * push;
          b.y += uy * push;
        }
      }
    }
  }

  const nodeIndexById = new Map<number, number>();
  layoutNodes.forEach((item, idx) => {
    nodeIndexById.set(item.node.id, idx);
  });

  // Gather all induced edges among included nodes + synthetic equivalence links to sibling aliases.
  const inducedEdges: GraphEdge[] = [];
  const parallel = new Map<string, EgoLayoutEdge[]>();
  const addEgoEdge = (edge: GraphEdge) => {
    const aIdx = nodeIndexById.get(edge.a);
    const bIdx = nodeIndexById.get(edge.b);
    if (aIdx === undefined || bIdx === undefined) return;
    inducedEdges.push(edge);
    const egoEdge: EgoLayoutEdge = { edge, aIdx, bIdx, curve: 0 };
    const key = aIdx <= bIdx ? `${aIdx}:${bIdx}` : `${bIdx}:${aIdx}`;
    const list = parallel.get(key);
    if (list) list.push(egoEdge);
    else parallel.set(key, [egoEdge]);
  };

  for (const edge of graph.edges) {
    addEgoEdge(edge);
  }

  // Link root to its equivalence-group siblings when they aren't directly connected by a real edge
  eqSiblings.forEach((sib, sIdx) => {
    const aIdx = nodeIndexById.get(rootId);
    const bIdx = nodeIndexById.get(sib.id);
    if (aIdx === undefined || bIdx === undefined) return;
    const key = aIdx <= bIdx ? `${aIdx}:${bIdx}` : `${bIdx}:${aIdx}`;
    if (!parallel.has(key)) {
      addEgoEdge({
        id: -(sIdx + 1),
        a: rootId,
        b: sib.id,
        type: "EQUIVALENCE_SIBLING",
        cross: false,
        hub: false,
        curve: 0,
        loop: 0,
        raw: { source: root.originalId, target: sib.originalId, type: "EQUIVALENCE_SIBLING" },
      });
    }
  });

  const layoutEdges: EgoLayoutEdge[] = [];
  for (const group of parallel.values()) {
    group.forEach((item, idx) => {
      item.curve = (idx - (group.length - 1) / 2) * 22;
      layoutEdges.push(item);
    });
  }

  return {
    rootId,
    hops,
    nodes: layoutNodes,
    edges: layoutEdges,
    nodeIndexById,
    ring1Radius,
    ring2Radius,
    truncated,
    totalReachable,
  };
}
