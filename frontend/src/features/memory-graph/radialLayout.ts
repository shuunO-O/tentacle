import { refineOrganicGalaxyLayout } from "./forceLayout";
import { computeUniqueDegrees } from "./graphEngine";
import type { GraphAnalysis, GraphCluster, GraphEdge, GraphNode } from "./types";

const GOLDEN_ANGLE = 2.399963229728653;
const TAU = Math.PI * 2;

export function layoutRadialGraph(
  nodes: GraphNode[],
  edges: GraphEdge[],
  analysis: GraphAnalysis,
  options: { centerId?: number | null } = {},
): GraphCluster[] {
  const n = nodes.length;
  const cx = 560;
  const cy = 400;
  const outer = 310;

  if (n === 0) {
    analysis.bounds = { cx, cy, radius: outer, minX: 0, maxX: 1120, minY: 0, maxY: 800 };
    analysis.centerId = null;
    analysis.requestedCenterId = options.centerId ?? null;
    return [];
  }

  const adj = analysis.adj;
  const uniqueDeg = computeUniqueDegrees(adj);
  const people = nodes
    .filter((node) => node.type === "Person")
    .sort((a, b) => (uniqueDeg[b.id] || 0) - (uniqueDeg[a.id] || 0) || a.id - b.id);

  let requested: GraphNode | undefined;
  if (options.centerId !== null && options.centerId !== undefined) {
    if (options.centerId >= 0 && options.centerId < n) {
      requested = nodes[options.centerId];
    }
  }

  const fallbackByDegree = [...nodes].sort(
    (a, b) => (uniqueDeg[b.id] || 0) - (uniqueDeg[a.id] || 0) || a.id - b.id,
  )[0];
  const primary = requested ?? people[0] ?? fallbackByDegree ?? null;

  const central = new Set<number>(analysis.correctHubs ? analysis.hubs : []);
  if (primary) central.add(primary.id);

  analysis.requestedCenterId = requested?.id ?? null;
  analysis.centerId = primary?.id ?? null;

  const rawGroups = analysis.clear
    ? analysis.groups
        .map((g) => g.filter((id) => !central.has(id)))
        .filter((g) => g.length > 0)
    : [
        nodes
          .filter((node) => (adj[node.id]?.length || 0) > 0 && !central.has(node.id))
          .map((node) => node.id),
      ].filter((g) => g.length > 0);

  const loose: number[] = [];
  const large: number[][] = [];
  for (const g of rawGroups) {
    if (g.length >= 4) {
      large.push(g);
    } else {
      for (const id of g) loose.push(id);
    }
  }

  const included = new Set<number>([...large.flat(), ...loose, ...central]);
  for (const node of nodes) {
    if (!included.has(node.id)) {
      loose.push(node.id);
    }
  }

  large.sort((a, b) => b.length - a.length || (a[0] ?? 0) - (b[0] ?? 0));

  const groups: Array<{
    ids: number[];
    loose: boolean;
    x: number;
    y: number;
    rx: number;
    ry: number;
  }> = large.map((ids) => ({
    ids,
    loose: false,
    x: cx,
    y: cy,
    rx: outer,
    ry: outer,
  }));

  if (loose.length > 0) {
    groups.push({
      ids: loose,
      loose: true,
      x: cx,
      y: cy,
      rx: outer,
      ry: outer,
    });
  }

  const anchorDegreeThreshold = Math.max(5, Math.ceil(Math.sqrt(n) * 0.28));
  const mergedDegByRep = new Map<number, number>();
  for (const eqMembers of analysis.equivalenceGroups || []) {
    if (eqMembers.length < 2) continue;
    const repId = eqMembers[0]!;
    const uniqueNeighbors = new Set<number>();
    const memberSet = new Set(eqMembers);
    for (const mid of eqMembers) {
      for (const nb of adj[mid] || []) {
        if (!memberSet.has(nb)) uniqueNeighbors.add(nb);
      }
    }
    mergedDegByRep.set(repId, uniqueNeighbors.size);
  }

  for (const node of nodes) {
    const deg = uniqueDeg[node.id] || 0;
    const visualDeg =
      node.eqRepId === node.id && (node.eqGroupSize || 1) >= 2
        ? Math.max(deg, mergedDegByRep.get(node.id) || deg)
        : deg;
    const isMilestone = node.eventKind === "milestone";
    const isNotableEvent = node.eventKind === "travel" || node.eventKind === "study";
    node.degree = deg;
    node.protagonist = node.id === primary?.id;
    node.bridge = central.has(node.id) && !node.protagonist;
    node.anchor =
      central.has(node.id) ||
      node.type === "Person" ||
      isMilestone ||
      visualDeg >= anchorDegreeThreshold;
    node.r = node.protagonist
      ? 8.8
      : isMilestone
        ? Math.max(6.4, Math.min(8.2, 4.2 + Math.sqrt(visualDeg) * 0.55))
        : Math.min(6.2, 2.2 + Math.sqrt(visualDeg) * 0.44) +
          (node.type === "Person" ? 0.5 : isNotableEvent ? 0.75 : 0);
  }

  // Place primary protagonist and satellite Person hubs in the central region.
  const satellites = [...central]
    .filter((id) => id !== primary?.id)
    .sort((a, b) => (uniqueDeg[b] || 0) - (uniqueDeg[a] || 0) || a - b);

  let centralRadius = 0;
  if (primary) {
    primary.x = cx;
    primary.y = cy;
    primary.c = -1;
    primary.anchor = true;
  }

  const satelliteOuter = Math.min(138, 44 + Math.sqrt(Math.max(1, satellites.length)) * 9);
  satellites.forEach((id, i) => {
    const node = nodes[id]!;
    const angle = -Math.PI / 2 + i * GOLDEN_ANGLE;
    const r = Math.sqrt(
      38 * 38 + ((satelliteOuter * satelliteOuter - 38 * 38) * (i + 0.5)) / satellites.length,
    );
    node.x = cx + Math.cos(angle) * r;
    node.y = cy + Math.sin(angle) * r;
    node.c = -1;
    node.anchor = true;
    centralRadius = Math.max(centralRadius, r);
  });

  const inner = primary ? Math.max(62, centralRadius + 26) : 0;
  const looseWidth =
    loose.length > 0 ? Math.min(64, Math.max(24, Math.sqrt(loose.length) * 2.8)) : 0;
  let extent = outer * Math.max(1, Math.sqrt(n / 680), Math.sqrt(large.length / 56));
  let groupOuter = extent - looseWidth - (loose.length > 0 && large.length > 0 ? 18 : 0);

  // Pack separate circular disks for every large community.
  if (analysis.clear && large.length > 0) {
    let packed = false;
    let factor = 9.8;
    const gap = 28;

    for (let attempt = 0; attempt < 64 && !packed; attempt += 1) {
      const radii = large.map((g) => Math.max(18, Math.sqrt(g.length + 4) * factor));
      const minOuterNeeded = inner + (radii[0] || 0) * 2 + 28;
      if (groupOuter < minOuterNeeded) {
        groupOuter = minOuterNeeded;
        extent = groupOuter + looseWidth + (loose.length > 0 ? 18 : 0);
      }

      const placed: Array<{ x: number; y: number; radius: number }> = [];
      const grid = new Map<string, Array<{ x: number; y: number; radius: number }>>();
      const cell = (radii[0] || 20) * 2 + gap;
      packed = true;

      for (let c = 0; c < large.length; c += 1) {
        const radius = radii[c]!;
        const preferred = -Math.PI / 2 + c * GOLDEN_ANGLE;
        const desired = Math.min(groupOuter - radius, inner + radius + 40 + (c % 2) * 28);
        const rMin = inner + radius + 8;
        const rMax = groupOuter - radius;
        const rStep = Math.max(6, (rMax - rMin) / 52);

        let best: { x: number; y: number; radius: number } | null = null;
        let score = Infinity;

        for (let r = rMin; r <= rMax + 0.5; r += rStep) {
          for (let k = 0; k < 120; k += 1) {
            const theta = (k * TAU) / 120 + c * 0.013;
            const x = cx + Math.cos(theta) * r;
            const y = cy + Math.sin(theta) * r;
            const gx = Math.floor(x / cell);
            const gy = Math.floor(y / cell);

            let blocked = false;
            for (let dx = -1; dx <= 1 && !blocked; dx += 1) {
              for (let dy = -1; dy <= 1 && !blocked; dy += 1) {
                const bucket = grid.get(`${gx + dx}:${gy + dy}`);
                if (!bucket) continue;
                for (const other of bucket) {
                  const minDist = radius + other.radius + gap;
                  const distSq = (x - other.x) ** 2 + (y - other.y) ** 2;
                  if (distSq < minDist * minDist) {
                    blocked = true;
                    break;
                  }
                }
              }
            }
            if (blocked) continue;

            const tx = cx + Math.cos(preferred) * desired;
            const ty = cy + Math.sin(preferred) * desired;
            const cost = (x - tx) ** 2 + (y - ty) ** 2;
            if (cost < score) {
              score = cost;
              best = { x, y, radius };
            }
          }
        }

        if (!best) {
          packed = false;
          break;
        }

        placed.push(best);
        const key = `${Math.floor(best.x / cell)}:${Math.floor(best.y / cell)}`;
        const bucket = grid.get(key);
        if (bucket) bucket.push(best);
        else grid.set(key, [best]);
      }

      if (packed) {
        placed.forEach((p, idx) => {
          groups[idx]!.x = p.x;
          groups[idx]!.y = p.y;
          groups[idx]!.rx = p.radius;
          groups[idx]!.ry = p.radius;
        });
      } else if (attempt < 10 && factor > 6.6) {
        factor *= 0.94;
      } else {
        extent *= 1.09;
        groupOuter = extent - looseWidth - (loose.length > 0 ? 18 : 0);
      }
    }
  }

  // Seed initial positions inside each island & loose ring, then refine organically with Barnes-Hut.
  const clusters: GraphCluster[] = [];

  groups.forEach((g, clusterIdx) => {
    const island = !g.loose && analysis.clear;
    if (!island) {
      g.x = cx;
      g.y = cy;
      g.rx = g.loose ? extent : groupOuter;
      g.ry = g.rx;
    }

    const ranked = g.loose
      ? [...g.ids]
      : [...g.ids].sort((a, b) => (uniqueDeg[b] || 0) - (uniqueDeg[a] || 0) || a - b);

    if (island && ranked.length > 0) {
      const topConcept = ranked.find((id) => nodes[id]?.type === "Concept") ?? ranked[0]!;
      nodes[topConcept]!.anchor = true;
    }

    const r0 = island ? 0 : g.loose && large.length > 0 ? groupOuter + 14 : inner;
    const r1 = island ? Math.max(8, g.rx - 7) : g.loose ? extent - 4 : groupOuter;

    ranked.forEach((id, i) => {
      const node = nodes[id]!;
      const fraction = (i * 0.61803398875 + 0.37) % 1;
      const theta = g.loose
        ? (TAU * (i + 0.28 + fraction * 0.4)) / ranked.length
        : i * GOLDEN_ANGLE;
      const radial = g.loose
        ? 0.12 + fraction * 0.78
        : ranked.length <= 1
          ? 0
          : i / Math.max(1, ranked.length - 0.5);
      const radius =
        !primary && n === 1
          ? 0
          : Math.sqrt(r0 * r0 + (r1 * r1 - r0 * r0) * radial) * (island ? 0.88 : 1);

      node.c = clusterIdx;
      node.x = g.x + Math.cos(theta) * radius;
      node.y = g.y + Math.sin(theta) * radius;
    });

    clusters.push({
      id: clusterIdx,
      x: g.x,
      y: g.y,
      rx: g.rx,
      ry: g.ry,
      angle: 0,
      loose: g.loose,
      size: g.ids.length,
    });
  });

  // Compute parallel curve offsets and cross/hub flags before organic refinement.
  const hubSet = new Set(analysis.hubs);
  const parallel = new Map<string, GraphEdge[]>();
  for (const e of edges) {
    const a = nodes[e.a];
    const b = nodes[e.b];
    if (!a || !b) continue;
    e.cross = a.c !== b.c;
    e.hub = hubSet.has(e.a) || hubSet.has(e.b);
    const key = e.a <= e.b ? `${e.a}:${e.b}` : `${e.b}:${e.a}`;
    const list = parallel.get(key);
    if (list) list.push(e);
    else parallel.set(key, [e]);
  }
  for (const group of parallel.values()) {
    group.forEach((e, idx) => {
      e.curve = (idx - (group.length - 1) / 2) * 16;
      e.loop = idx;
    });
  }

  refineOrganicGalaxyLayout(nodes, edges, clusters, analysis, {
    cx,
    cy,
    satelliteMinR: 36,
    satelliteMaxR: Math.max(54, satelliteOuter),
    looseInnerR: large.length > 0 ? groupOuter + 14 : inner,
    looseOuterR: extent - 4,
    hasLargeIslands: analysis.clear && large.length > 0,
  });

  // Compute final bounds across all settled nodes and community islands.
  let minX = cx - outer;
  let maxX = cx + outer;
  let minY = cy - outer;
  let maxY = cy + outer;
  for (const node of nodes) {
    if (node.x - node.r - 12 < minX) minX = node.x - node.r - 12;
    if (node.x + node.r + 12 > maxX) maxX = node.x + node.r + 12;
    if (node.y - node.r - 12 < minY) minY = node.y - node.r - 12;
    if (node.y + node.r + 12 > maxY) maxY = node.y + node.r + 12;
  }
  for (const c of clusters) {
    if (c.loose) continue;
    if (c.x - c.rx - 14 < minX) minX = c.x - c.rx - 14;
    if (c.x + c.rx + 14 > maxX) maxX = c.x + c.rx + 14;
    if (c.y - c.ry - 14 < minY) minY = c.y - c.ry - 14;
    if (c.y + c.ry + 14 > maxY) maxY = c.y + c.ry + 14;
  }

  const maxSpan = Math.max(
    outer,
    Math.hypot(Math.max(Math.abs(maxX - cx), Math.abs(minX - cx)), Math.max(Math.abs(maxY - cy), Math.abs(minY - cy))),
    extent,
  );
  analysis.bounds = {
    cx,
    cy,
    radius: Math.max(outer, maxSpan),
    minX,
    maxX,
    minY,
    maxY,
  };

  return clusters;
}
