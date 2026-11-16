import type { GraphAnalysis, GraphEdge, GraphNode } from "./types";

export function createRng(seed = 20260101): () => number {
  let state = seed >>> 0;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

export function buildAdjacency(nodeCount: number, edges: GraphEdge[]): number[][] {
  const adj: number[][] = Array.from({ length: nodeCount }, () => []);
  for (const e of edges) {
    if (e.a >= 0 && e.a < nodeCount && e.b >= 0 && e.b < nodeCount) {
      adj[e.a]!.push(e.b);
      if (e.a !== e.b) {
        adj[e.b]!.push(e.a);
      }
    }
  }
  return adj;
}

export function computeUniqueDegrees(adj: number[][]): number[] {
  return adj.map((neighbors, id) => {
    const unique = new Set<number>();
    for (const nb of neighbors) {
      if (nb !== id) unique.add(nb);
    }
    return unique.size;
  });
}

export function computeNodeWeights(
  nodes: GraphNode[],
  adj: number[][],
  edges: GraphEdge[],
  correctHubs: boolean,
): { weights: Float64Array; totalWeight: number; hubs: Set<number>; uniqueDegrees: number[] } {
  const n = nodes.length;
  const uniqueDegrees = computeUniqueDegrees(adj);
  let eventCount = 0;
  for (const node of nodes) {
    if (node.type === "Event") eventCount += 1;
  }

  const rawThreshold = Math.max(16, n * 0.07);
  const personThreshold =
    eventCount >= 20 ? Math.max(14, Math.min(rawThreshold, eventCount * 0.22)) : rawThreshold;
  const nonPersonHubThreshold = eventCount >= 20 ? Math.max(24, eventCount * 0.35) : Infinity;

  const hubs = new Set<number>();
  for (let i = 0; i < n; i += 1) {
    if (nodes[i]?.type === "Person" && (uniqueDegrees[i] || 0) >= personThreshold) {
      hubs.add(i);
    }
  }

  const weights = new Float64Array(edges.length);
  let totalWeight = 0;
  for (let i = 0; i < edges.length; i += 1) {
    const e = edges[i]!;
    if (e.a === e.b) continue;
    let w = 1;
    if (correctHubs) {
      if (hubs.has(e.a) || hubs.has(e.b)) {
        w = 0.035;
      } else if (
        (uniqueDegrees[e.a] || 0) >= nonPersonHubThreshold ||
        (uniqueDegrees[e.b] || 0) >= nonPersonHubThreshold
      ) {
        w = 0.2;
      }
    }
    weights[i] = w;
    totalWeight += w;
  }

  return { weights, totalWeight, hubs, uniqueDegrees };
}

export function computeModularity(
  nodeCount: number,
  edges: GraphEdge[],
  weights: Float64Array,
  totalWeight: number,
  communities: number[],
): number {
  if (totalWeight <= 0 || nodeCount === 0) return 0;
  const m2 = totalWeight * 2;
  const degrees = new Float64Array(nodeCount);
  const inner = new Map<number, number>();

  for (let i = 0; i < edges.length; i += 1) {
    const w = weights[i] || 0;
    if (w <= 0) continue;
    const e = edges[i]!;
    degrees[e.a] = (degrees[e.a] || 0) + w;
    degrees[e.b] = (degrees[e.b] || 0) + w;
    const ca = communities[e.a] ?? 0;
    const cb = communities[e.b] ?? 0;
    if (ca === cb) {
      inner.set(ca, (inner.get(ca) || 0) + 2 * w);
    }
  }

  const tot = new Map<number, number>();
  for (let i = 0; i < nodeCount; i += 1) {
    const c = communities[i] ?? 0;
    tot.set(c, (tot.get(c) || 0) + (degrees[i] || 0));
  }

  let q = 0;
  for (const [c, sigTot] of tot.entries()) {
    const sigIn = inner.get(c) || 0;
    q += sigIn / m2 - Math.pow(sigTot / m2, 2);
  }
  return q;
}

/**
 * Multilevel Louvain local modularity optimization followed by fragment splitting.
 */
export function partitionCommunities(
  nodeCount: number,
  adj: number[][],
  edges: GraphEdge[],
  weights: Float64Array,
  totalWeight: number,
  hubs: Set<number> = new Set(),
  virtualPairs: Array<[number, number]> = [],
): number[] {
  if (nodeCount === 0) return [];
  if (totalWeight <= 0 || edges.length === 0) {
    return Array.from({ length: nodeCount }, (_, i) => i);
  }

  let graph: Array<Map<number, number>> = Array.from({ length: nodeCount }, () => new Map());
  let labels = Array.from({ length: nodeCount }, (_, i) => i);

  for (let i = 0; i < edges.length; i += 1) {
    const w = weights[i] || 0;
    if (w <= 0) continue;
    const e = edges[i]!;
    if (e.a === e.b) continue;
    graph[e.a]!.set(e.b, (graph[e.a]!.get(e.b) || 0) + w);
    graph[e.b]!.set(e.a, (graph[e.b]!.get(e.a) || 0) + w);
  }

  // Softly bind Concept equivalence-group siblings into the same Louvain community.
  const virtualAdj: number[][] = Array.from({ length: nodeCount }, () => []);
  for (const [a, b] of virtualPairs) {
    if (a === b || hubs.has(a) || hubs.has(b)) continue;
    graph[a]!.set(b, (graph[a]!.get(b) || 0) + 0.35);
    graph[b]!.set(a, (graph[b]!.get(a) || 0) + 0.35);
    virtualAdj[a]!.push(b);
    virtualAdj[b]!.push(a);
  }

  const random = createRng(177);
  for (let level = 0; level < 12; level += 1) {
    const count = graph.length;
    const community = Array.from({ length: count }, (_, i) => i);
    const degree = new Float64Array(count);
    let m2 = 0;
    for (let i = 0; i < count; i += 1) {
      let deg = 0;
      for (const value of graph[i]!.values()) {
        deg += value;
      }
      degree[i] = deg;
      m2 += deg;
    }
    if (m2 <= 0) break;

    const total = Float64Array.from(degree);
    const order = Array.from({ length: count }, (_, i) => i);

    for (let pass = 0; pass < 40; pass += 1) {
      let moved = 0;
      for (let i = count - 1; i > 0; i -= 1) {
        const j = Math.floor(random() * (i + 1));
        const tmp = order[i]!;
        order[i] = order[j]!;
        order[j] = tmp;
      }

      for (const i of order) {
        const degI = degree[i] || 0;
        if (degI <= 0) continue;
        const old = community[i]!;
        const links = new Map<number, number>();
        for (const [j, value] of graph[i]!) {
          if (j === i) continue;
          const cj = community[j]!;
          links.set(cj, (links.get(cj) || 0) + value);
        }

        total[old] = (total[old] || 0) - degI;
        let best = old;
        let bestGain = (links.get(old) || 0) - (degI * (total[old] || 0)) / m2;

        for (const [c, value] of links) {
          const gain = value - (degI * (total[c] || 0)) / m2;
          if (gain > bestGain + 1e-10) {
            best = c;
            bestGain = gain;
          }
        }

        community[i] = best;
        total[best] = (total[best] || 0) + degI;
        if (best !== old) moved += 1;
      }

      if (moved === 0) break;
    }

    const ids = [...new Set(community)].sort((a, b) => a - b);
    const mapping = new Map<number, number>(ids.map((c, idx) => [c, idx]));
    const compact = community.map((c) => mapping.get(c)!);
    labels = labels.map((c) => compact[c]!);

    if (ids.length === count) break;

    const next: Array<Map<number, number>> = Array.from({ length: ids.length }, () => new Map());
    for (let i = 0; i < count; i += 1) {
      const u = compact[i]!;
      for (const [j, value] of graph[i]!) {
        const v = compact[j]!;
        next[u]!.set(v, (next[u]!.get(v) || 0) + value);
      }
    }
    graph = next;
  }

  // Split disconnected fragments that can remain after local moves (excluding traversal across hubs).
  const split = new Array<number>(nodeCount).fill(-1);
  let nextComm = 0;
  for (let i = 0; i < nodeCount; i += 1) {
    if (split[i] !== -1) continue;
    if (hubs.has(i)) {
      split[i] = nextComm++;
      continue;
    }
    const targetLabel = labels[i]!;
    const stack = [i];
    split[i] = nextComm;
    while (stack.length > 0) {
      const u = stack.pop()!;
      for (const v of adj[u] || []) {
        if (split[v] === -1 && !hubs.has(v) && labels[v] === targetLabel) {
          split[v] = nextComm;
          stack.push(v);
        }
      }
      for (const v of virtualAdj[u] || []) {
        if (split[v] === -1 && !hubs.has(v) && labels[v] === targetLabel) {
          split[v] = nextComm;
          stack.push(v);
        }
      }
    }
    nextComm += 1;
  }

  // Re-index communities densely by descending size.
  const counts = new Map<number, number>();
  for (const c of split) {
    counts.set(c, (counts.get(c) || 0) + 1);
  }
  const ranked = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0] - b[0]);
  const remap = new Map<number, number>(ranked.map(([oldC], idx) => [oldC, idx]));
  return split.map((c) => remap.get(c) ?? 0);
}

export function rewireEdges(edges: GraphEdge[], seed: number): GraphEdge[] {
  const rand = createRng(seed);
  const result = edges.map((e) => ({ ...e }));
  const m = result.length;
  if (m < 2) return result;

  const key = (a: number, b: number) => (a < b ? `${a}:${b}` : `${b}:${a}`);
  const seen = new Set(result.map((e) => key(e.a, e.b)));
  const swaps = Math.min(m * 8, 18_000);

  for (let s = 0; s < swaps; s += 1) {
    const u = Math.floor(rand() * m);
    const v = Math.floor(rand() * m);
    if (u === v) continue;
    const x = result[u]!;
    const y = result[v]!;
    const [a, b] = rand() < 0.5 ? [x.a, x.b] : [x.b, x.a];
    const [c, d] = rand() < 0.5 ? [y.a, y.b] : [y.b, y.a];
    if (a === b || c === d || a === c || a === d || b === c || b === d) continue;
    const k1 = key(a, d);
    const k2 = key(c, b);
    if (seen.has(k1) || seen.has(k2)) continue;
    seen.delete(key(x.a, x.b));
    seen.delete(key(y.a, y.b));
    result[u] = { ...x, a, b: d };
    result[v] = { ...y, a: c, b };
    seen.add(k1);
    seen.add(k2);
  }

  return result;
}

export function analyzeGraph(
  nodes: GraphNode[],
  edges: GraphEdge[],
  correctHubs = true,
): GraphAnalysis {
  const n = nodes.length;
  const adj = buildAdjacency(n, edges);
  const eqGroups = new Map<string, number[]>();
  for (const node of nodes) {
    if (node.type === "Concept" && node.equivalenceGroup) {
      const list = eqGroups.get(node.equivalenceGroup);
      if (list) list.push(node.id);
      else eqGroups.set(node.equivalenceGroup, [node.id]);
    }
  }
  const virtualPairs: Array<[number, number]> = [];
  for (const members of eqGroups.values()) {
    if (members.length < 2) continue;
    for (let k = 1; k < members.length && k <= 12; k += 1) {
      virtualPairs.push([members[0]!, members[k]!]);
    }
  }

  const { weights, totalWeight, hubs, uniqueDegrees } = computeNodeWeights(
    nodes,
    adj,
    edges,
    correctHubs,
  );

  // Build curated multi-member equivalence groups (only >= 2 concepts share a logical entry)
  const equivalenceGroups: number[][] = [];
  const repByNodeId = new Map<number, { repId: number; size: number }>();
  for (const rawMembers of eqGroups.values()) {
    if (rawMembers.length < 2) continue;
    const members = [...rawMembers].sort(
      (a, b) => (uniqueDegrees[b] || 0) - (uniqueDegrees[a] || 0) || a - b,
    );
    const repId = members[0]!;
    equivalenceGroups.push(members);
    for (const mid of members) {
      repByNodeId.set(mid, { repId, size: members.length });
    }
  }

  const communities = partitionCommunities(n, adj, edges, weights, totalWeight, hubs, virtualPairs);

  const groupMap = new Map<number, number[]>();
  for (let i = 0; i < n; i += 1) {
    const c = communities[i] ?? 0;
    const node = nodes[i]!;
    node.c = c;
    node.degree = uniqueDegrees[i] || 0;
    node.isSingleLeaf = false;
    node.leafParentId = null;
    node.foldedLeafCount = 0;
    const eqMeta = repByNodeId.get(i);
    node.eqRepId = eqMeta ? eqMeta.repId : null;
    node.eqGroupSize = eqMeta ? eqMeta.size : 1;
    if (!groupMap.has(c)) groupMap.set(c, []);
    groupMap.get(c)!.push(i);
  }

  let singleLeafCount = 0;
  for (let i = 0; i < n; i += 1) {
    const node = nodes[i]!;
    if (node.degree !== 1 || node.type === "Person" || node.type === "Event") continue;
    if ((node.eqGroupSize || 1) >= 2) continue;
    const parentId = (adj[i] || []).find((nb) => nb !== i);
    if (parentId === undefined) continue;
    const parent = nodes[parentId];
    if (!parent || (uniqueDegrees[parentId] || 0) < 2) continue;
    node.isSingleLeaf = true;
    node.leafParentId = parentId;
    parent.foldedLeafCount = (parent.foldedLeafCount || 0) + 1;
    singleLeafCount += 1;
  }

  const q = computeModularity(n, edges, weights, totalWeight, communities);
  let baselineSum = 0;
  const trials = edges.length >= 4 ? 3 : 0;
  for (let t = 0; t < trials; t += 1) {
    const rw = rewireEdges(edges, 889 + t);
    const rwAdj = buildAdjacency(n, rw);
    const rwStats = computeNodeWeights(nodes, rwAdj, rw, correctHubs);
    const rwComms = partitionCommunities(
      n,
      rwAdj,
      rw,
      rwStats.weights,
      rwStats.totalWeight,
      rwStats.hubs,
    );
    baselineSum += computeModularity(n, rw, rwStats.weights, rwStats.totalWeight, rwComms);
  }

  const baseline = trials > 0 ? baselineSum / trials : 0;
  const margin = q - baseline;
  const clear = n >= 6 && totalWeight > 0 && margin > 0.055;
  const groups = [...groupMap.values()].sort((a, b) => b.length - a.length || (a[0] ?? 0) - (b[0] ?? 0));

  return {
    adj,
    hubs: Array.from(hubs),
    communities,
    groups,
    clear,
    q,
    baseline,
    margin,
    correctHubs,
    requestedCenterId: null,
    centerId: null,
    singleLeafCount,
    equivalenceGroupCount: equivalenceGroups.length,
    equivalenceGroups,
    bounds: {
      cx: 560,
      cy: 400,
      radius: 310,
      minX: 0,
      maxX: 1120,
      minY: 0,
      maxY: 800,
    },
  };
}

/**
 * Extract 2-3 distinctive topic keywords for a community cluster (preferring shared Concepts/Locations).
 */
export function extractClusterKeywords(
  memberIds: number[],
  nodes: GraphNode[],
  adj: number[][],
  maxTerms = 3,
): string[] {
  if (memberIds.length === 0) return [];
  const memberSet = new Set(memberIds);
  const scored: Array<{ text: string; stem: string; score: number }> = [];

  for (const id of memberIds) {
    const node = nodes[id];
    if (!node || node.protagonist || node.bridge) continue;
    if (node.type !== "Concept" && node.type !== "Location" && node.type !== "Other") continue;
    const rawName = node.name.trim();
    if (!rawName || rawName === node.originalId) continue;

    let localLinks = 0;
    for (const nb of adj[id] || []) {
      if (memberSet.has(nb) && nb !== id) {
        localLinks += nodes[nb]?.eventKind === "milestone" ? 2.4 : 1;
      }
    }

    const typeBoost = node.type === "Concept" ? 1.35 : node.type === "Location" ? 1.05 : 0.85;
    const lengthPenalty = rawName.length > 16 ? 0.72 : 1;
    const score =
      (localLinks * 2.2 + Math.sqrt(Math.max(1, node.degree)) * 0.6) * typeBoost * lengthPenalty;

    const stem = rawName.split(/\s*[·:：|-]\s*/)[0]?.trim() || rawName;
    scored.push({
      text: rawName.length > 14 ? `${rawName.slice(0, 13)}…` : rawName,
      stem,
      score,
    });
  }

  scored.sort((a, b) => b.score - a.score);
  const seenStems = new Set<string>();
  const keywords: string[] = [];
  for (const item of scored) {
    const normalizedStem = item.stem.toLowerCase();
    if (seenStems.has(normalizedStem)) continue;
    seenStems.add(normalizedStem);
    keywords.push(item.text);
    if (keywords.length >= maxTerms) break;
  }

  return keywords;
}
