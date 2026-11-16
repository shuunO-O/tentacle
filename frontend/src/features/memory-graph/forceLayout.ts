import { extractClusterKeywords } from "./graphEngine";
import type { GraphAnalysis, GraphCluster, GraphEdge, GraphNode } from "./types";

interface QuadBody {
  id: number;
  x: number;
  y: number;
  mass: number;
}

class QuadTreeNode {
  x0: number;
  y0: number;
  size: number;
  mass = 0;
  cx = 0;
  cy = 0;
  body: QuadBody | null = null;
  extras: QuadBody[] | null = null;
  children: [QuadTreeNode, QuadTreeNode, QuadTreeNode, QuadTreeNode] | null = null;

  constructor(x0: number, y0: number, size: number) {
    this.x0 = x0;
    this.y0 = y0;
    this.size = size;
  }

  insert(b: QuadBody, depth = 0): void {
    const totalMass = this.mass + b.mass;
    this.cx = (this.cx * this.mass + b.x * b.mass) / totalMass;
    this.cy = (this.cy * this.mass + b.y * b.mass) / totalMass;
    this.mass = totalMass;

    if (this.children !== null) {
      this.subInsert(b, depth + 1);
      return;
    }

    if (this.body === null) {
      this.body = b;
      return;
    }

    if (depth >= 12 || (Math.abs(this.body.x - b.x) < 1e-3 && Math.abs(this.body.y - b.y) < 1e-3)) {
      if (!this.extras) this.extras = [];
      this.extras.push(b);
      return;
    }

    const half = this.size * 0.5;
    this.children = [
      new QuadTreeNode(this.x0, this.y0, half),
      new QuadTreeNode(this.x0 + half, this.y0, half),
      new QuadTreeNode(this.x0, this.y0 + half, half),
      new QuadTreeNode(this.x0 + half, this.y0 + half, half),
    ];

    const existing = this.body;
    this.body = null;
    this.subInsert(existing, depth + 1);
    this.subInsert(b, depth + 1);
  }

  private subInsert(b: QuadBody, depth: number): void {
    const half = this.size * 0.5;
    const right = b.x >= this.x0 + half ? 1 : 0;
    const bottom = b.y >= this.y0 + half ? 2 : 0;
    this.children![right + bottom]!.insert(b, depth);
  }

  accumulateRepulsion(
    x: number,
    y: number,
    selfId: number,
    theta: number,
    repulsion: number,
    out: { fx: number; fy: number },
  ): void {
    if (this.mass === 0) return;

    if (this.children === null) {
      if (this.body && this.body.id !== selfId) {
        const dx = x - this.body.x;
        const dy = y - this.body.y;
        const d2 = dx * dx + dy * dy + 28;
        const f = (repulsion * this.body.mass) / d2;
        const invD = 1 / Math.sqrt(d2);
        out.fx += dx * invD * f;
        out.fy += dy * invD * f;
      }
      if (this.extras) {
        for (const extra of this.extras) {
          if (extra.id === selfId) continue;
          const dx = x - extra.x;
          const dy = y - extra.y;
          const d2 = dx * dx + dy * dy + 28;
          const f = (repulsion * extra.mass) / d2;
          const invD = 1 / Math.sqrt(d2);
          out.fx += dx * invD * f;
          out.fy += dy * invD * f;
        }
      }
      return;
    }

    const dx = x - this.cx;
    const dy = y - this.cy;
    const d2 = dx * dx + dy * dy + 28;
    const dist = Math.sqrt(d2);
    if (this.size / dist < theta) {
      const f = (repulsion * this.mass) / d2;
      out.fx += (dx / dist) * f;
      out.fy += (dy / dist) * f;
      return;
    }

    for (const child of this.children) {
      if (child.mass > 0) {
        child.accumulateRepulsion(x, y, selfId, theta, repulsion, out);
      }
    }
  }
}

export interface ForceRefineOptions {
  cx: number;
  cy: number;
  satelliteMinR: number;
  satelliteMaxR: number;
  looseInnerR: number;
  looseOuterR: number;
  hasLargeIslands: boolean;
}

/**
 * Barnes-Hut O(N log N) organic force-directed refinement inside community islands
 * and along cross-island bridges, followed by spatial collision polish and cluster keyword tagging.
 */
export function refineOrganicGalaxyLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  clusters: GraphCluster[],
  analysis: GraphAnalysis,
  opts: ForceRefineOptions,
): void {
  const n = nodes.length;
  if (n <= 2) {
    stampClusterMetadata(nodes, clusters, analysis);
    return;
  }

  const clusterByIndex = new Map<number, GraphCluster>();
  for (const cluster of clusters) {
    clusterByIndex.set(cluster.id, cluster);
  }

  // Pair up equivalent Concepts so synonyms sit comfortably close inside their community island.
  const eqByGroup = new Map<string, number[]>();
  for (const node of nodes) {
    if (node.type === "Concept" && node.equivalenceGroup) {
      const list = eqByGroup.get(node.equivalenceGroup);
      if (list) list.push(node.id);
      else eqByGroup.set(node.equivalenceGroup, [node.id]);
    }
  }
  const eqPairs: Array<[number, number]> = [];
  for (const members of eqByGroup.values()) {
    for (let k = 1; k < members.length && k <= 12; k += 1) {
      eqPairs.push([members[0]!, members[k]!]);
    }
  }

  // Record original packed circle constraints so islands stay organically textured yet non-overlapping.
  const clusterCaps = new Map<number, { x: number; y: number; maxR: number }>();
  for (const cluster of clusters) {
    if (!cluster.loose) {
      clusterCaps.set(cluster.id, {
        x: cluster.x,
        y: cluster.y,
        maxR: Math.max(10, cluster.rx - 4),
      });
    }
  }

  const fx = new Float64Array(n);
  const fy = new Float64Array(n);
  const vx = new Float64Array(n);
  const vy = new Float64Array(n);
  const iterations = n > 1600 ? 54 : n > 500 ? 64 : 72;
  const repulsionConst = n > 1200 ? 115 : 145;
  const theta = 0.85;
  const forceOut = { fx: 0, fy: 0 };

  for (let iter = 0; iter < iterations; iter += 1) {
    const progress = iter / Math.max(1, iterations - 1);
    const cooling = 1 - 0.82 * progress;
    fx.fill(0);
    fy.fill(0);

    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const node of nodes) {
      if (node.x < minX) minX = node.x;
      if (node.x > maxX) maxX = node.x;
      if (node.y < minY) minY = node.y;
      if (node.y > maxY) maxY = node.y;
    }

    const span = Math.max(100, maxX - minX, maxY - minY) + 40;
    const root = new QuadTreeNode(minX - 20, minY - 20, span);
    for (const node of nodes) {
      const mass = node.protagonist
        ? 2.2
        : node.eventKind === "milestone"
          ? 1.55
          : node.isSingleLeaf
            ? 0.42
            : node.type === "Event"
              ? 1.25
              : 0.9;
      root.insert({ id: node.id, x: node.x, y: node.y, mass });
    }

    // 1. Barnes-Hut pairwise repulsion
    for (let i = 0; i < n; i += 1) {
      const node = nodes[i]!;
      if (node.protagonist) continue;
      forceOut.fx = 0;
      forceOut.fy = 0;
      root.accumulateRepulsion(node.x, node.y, node.id, theta, repulsionConst, forceOut);
      // Leaf nodes mainly need repulsion from nearby neighbors; cap huge long-range drift.
      const cap = node.isSingleLeaf ? 4.5 : 8.5;
      fx[i] = Math.max(-cap, Math.min(cap, forceOut.fx));
      fy[i] = Math.max(-cap, Math.min(cap, forceOut.fy));
    }

    // 2. Semantic edge springs (tight leaf bouquets, clear local chains, soft cross bridges)
    for (const e of edges) {
      if (e.a === e.b) continue;
      const a = nodes[e.a];
      const b = nodes[e.b];
      if (!a || !b) continue;

      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 1;

      const isLeafEdge = Boolean(a.isSingleLeaf || b.isSingleLeaf);
      let rest = 28;
      let stiffness = 0.14;

      if (e.hub) {
        // Let satellite hubs drift softly toward the direction of their connected islands.
        rest = 95;
        stiffness = a.bridge || b.bridge ? 0.012 : 0.0025;
      } else if (e.cross) {
        rest = 92;
        stiffness = 0.016;
      } else if (isLeafEdge) {
        rest = 16;
        stiffness = 0.28;
      } else if (a.type === "Event" || b.type === "Event") {
        rest = 26;
        stiffness = 0.16;
      }

      const delta = (d - rest) * stiffness;
      const px = (dx / d) * delta;
      const py = (dy / d) * delta;

      if (!a.protagonist) {
        const weightA = a.bridge ? 0.25 : 1;
        fx[a.id] = (fx[a.id] || 0) + px * weightA;
        fy[a.id] = (fy[a.id] || 0) + py * weightA;
      }
      if (!b.protagonist) {
        const weightB = b.bridge ? 0.25 : 1;
        fx[b.id] = (fx[b.id] || 0) - px * weightB;
        fy[b.id] = (fy[b.id] || 0) - py * weightB;
      }
    }

    // Gentle attraction between equivalence-group Concept siblings
    for (const [aid, bid] of eqPairs) {
      const a = nodes[aid];
      const b = nodes[bid];
      if (!a || !b || a.c !== b.c) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 1;
      const delta = (d - 18) * 0.08;
      const px = (dx / d) * delta;
      const py = (dy / d) * delta;
      fx[aid] = (fx[aid] || 0) + px;
      fy[aid] = (fy[aid] || 0) + py;
      fx[bid] = (fx[bid] || 0) - px;
      fy[bid] = (fy[bid] || 0) - py;
    }

    // 3. Community anchor gravity & boundary containment
    for (let i = 0; i < n; i += 1) {
      const node = nodes[i]!;
      if (node.protagonist) continue;

      if (node.bridge) {
        // Keep satellite Person hubs on a comfortable inner orbital band around protagonist.
        const dx = node.x - opts.cx;
        const dy = node.y - opts.cy;
        const r = Math.hypot(dx, dy) || 1;
        const targetR = Math.max(opts.satelliteMinR, Math.min(opts.satelliteMaxR, r));
        const radialPull = (targetR - r) * 0.25;
        fx[i] = (fx[i] || 0) + (dx / r) * radialPull;
        fy[i] = (fy[i] || 0) + (dy / r) * radialPull;
      } else {
        const cluster = clusterByIndex.get(node.c);
        if (cluster && !cluster.loose) {
          const pull = node.isSingleLeaf ? 0.03 : 0.075;
          fx[i] = (fx[i] || 0) + (cluster.x - node.x) * pull;
          fy[i] = (fy[i] || 0) + (cluster.y - node.y) * pull;
        } else if (cluster && cluster.loose) {
          // Keep loose components softly inside the outer loose band rather than drifting to infinity.
          const dx = node.x - opts.cx;
          const dy = node.y - opts.cy;
          const r = Math.hypot(dx, dy) || 1;
          const midR = (opts.looseInnerR + opts.looseOuterR) * 0.5;
          const pull = (midR - r) * 0.045;
          fx[i] = (fx[i] || 0) + (dx / r) * pull;
          fy[i] = (fy[i] || 0) + (dy / r) * pull;
        }
      }

      // Integrate velocity with damping & cooling
      const damp = 0.55;
      vx[i] = ((vx[i] || 0) + (fx[i] || 0) * cooling) * damp;
      vy[i] = ((vy[i] || 0) + (fy[i] || 0) * cooling) * damp;
      const maxStep = 6.5 * cooling + 1.2;
      const stepLen = Math.hypot(vx[i]!, vy[i]!);
      if (stepLen > maxStep) {
        vx[i] = (vx[i]! / stepLen) * maxStep;
        vy[i] = (vy[i]! / stepLen) * maxStep;
      }

      node.x += vx[i]!;
      node.y += vy[i]!;

      // Enforce hard constraints so islands never invade neighbor islands or protagonist space.
      if (node.bridge) {
        const dx = node.x - opts.cx;
        const dy = node.y - opts.cy;
        const r = Math.hypot(dx, dy) || 1;
        const clamped = Math.max(opts.satelliteMinR, Math.min(opts.satelliteMaxR, r));
        node.x = opts.cx + (dx / r) * clamped;
        node.y = opts.cy + (dy / r) * clamped;
      } else {
        const cap = clusterCaps.get(node.c);
        if (cap) {
          const dx = node.x - cap.x;
          const dy = node.y - cap.y;
          const d = Math.hypot(dx, dy) || 1;
          if (d > cap.maxR) {
            node.x = cap.x + (dx / d) * cap.maxR;
            node.y = cap.y + (dy / d) * cap.maxR;
          }
        } else if (opts.hasLargeIslands) {
          const dx = node.x - opts.cx;
          const dy = node.y - opts.cy;
          const r = Math.hypot(dx, dy) || 1;
          const clamped = Math.max(opts.looseInnerR, Math.min(opts.looseOuterR, r));
          node.x = opts.cx + (dx / r) * clamped;
          node.y = opts.cy + (dy / r) * clamped;
        }
      }
    }
  }

  // Final spatial-grid node collision pass to guarantee crisp separation.
  const cell = 30;
  for (let pass = 0; pass < 8; pass += 1) {
    const grid = new Map<string, number[]>();
    for (const node of nodes) {
      const key = `${Math.floor(node.x / cell)}:${Math.floor(node.y / cell)}`;
      const bucket = grid.get(key);
      if (bucket) bucket.push(node.id);
      else grid.set(key, [node.id]);
    }

    for (const a of nodes) {
      const gx = Math.floor(a.x / cell);
      const gy = Math.floor(a.y / cell);
      for (let ox = -1; ox <= 1; ox += 1) {
        for (let oy = -1; oy <= 1; oy += 1) {
          const bucket = grid.get(`${gx + ox}:${gy + oy}`);
          if (!bucket) continue;
          for (const j of bucket) {
            if (j <= a.id) continue;
            const b = nodes[j]!;
            const dx = b.x - a.x;
            const dy = b.y - a.y;
            const dist = Math.hypot(dx, dy) || 0.01;
            const minSep = a.r + b.r + 3.5;
            if (dist < minSep) {
              const push = (minSep - dist) * 0.45;
              const ux = dx / dist;
              const uy = dy / dist;
              if (!a.protagonist) {
                a.x -= ux * push;
                a.y -= uy * push;
              }
              if (!b.protagonist) {
                b.x += ux * push;
                b.y += uy * push;
              }
            }
          }
        }
      }
    }
  }

  stampClusterMetadata(nodes, clusters, analysis);
}

export function stampClusterMetadata(
  nodes: GraphNode[],
  clusters: GraphCluster[],
  analysis: GraphAnalysis,
): void {
  const membersByCluster = new Map<number, number[]>();
  for (const node of nodes) {
    if (node.protagonist || node.bridge) continue;
    const list = membersByCluster.get(node.c);
    if (list) list.push(node.id);
    else membersByCluster.set(node.c, [node.id]);
  }

  for (const cluster of clusters) {
    const members = membersByCluster.get(cluster.id) || [];
    if (cluster.loose || members.length === 0) {
      cluster.keywords = [];
      cluster.title = "";
      continue;
    }

    const keywords = extractClusterKeywords(members, nodes, analysis.adj, 2);
    if (keywords.length === 0 && members.length > 0) {
      const top = [...members]
        .map((id) => nodes[id]!)
        .sort((a, b) => b.degree - a.degree || a.id - b.id)[0];
      if (top?.name) {
        keywords.push(top.name.length > 14 ? `${top.name.slice(0, 13)}…` : top.name);
      }
    }
    cluster.keywords = keywords;
    cluster.title = keywords.join(" · ");
  }
}
