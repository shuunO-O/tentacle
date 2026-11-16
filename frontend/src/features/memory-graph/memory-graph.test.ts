import { describe, expect, it } from "vitest";
import { buildEgoSubgraph } from "./egoLayout";
import { analyzeGraph, createRng, rewireEdges } from "./graphEngine";
import { parseGraphJsonText, serializeGraphDocument } from "./graphIo";
import { layoutRadialGraph } from "./radialLayout";
import type { RawGraphEdge, RawGraphNode } from "./types";

function buildSyntheticMemoryGraph(total = 2257): {
  nodes: RawGraphNode[];
  relationships: RawGraphEdge[];
} {
  const random = createRng(2419);
  const terms = [
    "摄影", "咖啡", "代码", "森林", "音乐", "海边", "阅读", "设计",
    "散步", "电影", "植物", "旅行", "学习", "画画", "雨声", "朋友",
    "笔记", "计划", "早餐", "日落", "故事", "实验", "车站", "书店",
  ];
  const nodes: RawGraphNode[] = [];
  const relationships: RawGraphEdge[] = [];
  const seen = new Set<string>();

  const add = (label: string, name: string): string => {
    const id = `${label}:${nodes.length}`;
    nodes.push({ id, label, name });
    return id;
  };
  const connect = (source: string, target: string, type: string) => {
    if (source === target) return;
    const key = source < target ? `${source}|${target}` : `${target}|${source}`;
    if (seen.has(key)) return;
    seen.add(key);
    relationships.push({ source, target, type });
  };

  const protagonist = add("Person", "共同人物 A");
  const companion = add("Person", "共同人物 B");
  const eventCount = Math.floor(total * 0.13);
  const personCount = 11;
  const locationCount = 56;

  const friends: string[] = [];
  for (let i = 0; i < personCount; i += 1) {
    friends.push(add("Person", `朋友 ${i + 1}`));
  }
  const places: string[] = [];
  for (let i = 0; i < locationCount; i += 1) {
    places.push(add("Location", `${["书店", "海边", "公园", "家中", "咖啡馆"][i % 5]} ${Math.floor(i / 5) + 1}`));
  }
  const concepts: string[] = [];
  while (nodes.length < total - eventCount) {
    const idx = concepts.length;
    concepts.push(add("Concept", `${terms[idx % terms.length]} · ${Math.floor(idx / terms.length) + 1}`));
  }

  const bucketSize = Math.max(12, Math.floor(concepts.length / 22));
  for (let i = 0; i < eventCount; i += 1) {
    const bucket = Math.floor((i / eventCount) * 22);
    const start = bucket * bucketSize;
    const end = Math.min(concepts.length, start + bucketSize);
    const chosen = new Set<string>();
    const mentions = 5 + Math.floor(random() * 4);
    for (let j = 0; j < mentions; j += 1) {
      const localPool = random() < 0.9;
      const idx = localPool
        ? start + Math.floor(random() * Math.max(1, end - start))
        : Math.floor(random() * concepts.length);
      chosen.add(concepts[idx]!);
    }
    const event = add("Event", `记忆 ${String(i + 1).padStart(3, "0")}`);
    for (const c of chosen) connect(event, c, "MENTIONS");
    connect(protagonist, event, "PARTICIPATED_IN");
    if (random() < 0.72) connect(companion, event, "PARTICIPATED_IN");
    if (random() < 0.3) connect(friends[Math.floor(random() * friends.length)]!, event, "PARTICIPATED_IN");
    if (random() < 0.35) connect(event, places[bucket % places.length]!, "OCCURRED_AT");
  }

  return { nodes, relationships };
}

describe("memory-graph pure engine", () => {
  it("normalizes graph documents and rejects .dump files", () => {
    expect(() => parseGraphJsonText("{}", "memory.dump")).toThrow(/\.dump/);

    const parsed = parseGraphJsonText(
      JSON.stringify({
        nodes: [
          { id: "Person:p1", label: "Person", properties: { name: "Alice" } },
          { id: "Concept:c1", label: "Concept", name: "Rust" },
        ],
        relationships: [{ source: "Person:p1", target: "Concept:c1", type: "LIKES" }],
      }),
      "graph.json",
    );

    expect(parsed.nodes).toHaveLength(2);
    expect(parsed.nodes[0]?.name).toBe("Alice");
    expect(parsed.nodes[1]?.type).toBe("Concept");
    expect(parsed.edges).toHaveLength(1);
    expect(parsed.edges[0]?.type).toBe("LIKES");

    const serialized = serializeGraphDocument(parsed.document);
    expect(serialized).toContain("Person:p1");
  });

  it("partitions communities, folds leaves, and builds ego subgraphs", () => {
    const raw = {
      nodes: [
        { id: "p1", label: "Person", name: "Alice" },
        { id: "c1", label: "Concept", name: "Graph" },
        { id: "c2", label: "Concept", name: "Canvas" },
        { id: "e1", label: "Event", name: "Launch" },
        { id: "l1", label: "Location", name: "Lab" },
      ],
      relationships: [
        { source: "p1", target: "c1", type: "KNOWS" },
        { source: "c1", target: "c2", type: "RELATES" },
        { source: "p1", target: "e1", type: "ATTENDED" },
        { source: "e1", target: "l1", type: "AT" },
      ],
    };

    const doc = parseGraphJsonText(JSON.stringify(raw), "sample.json");
    const analysis = analyzeGraph(doc.nodes, doc.edges, true);
    const clusters = layoutRadialGraph(doc.nodes, doc.edges, analysis, { centerId: 0 });

    expect(analysis.centerId).toBe(0);
    expect(doc.nodes[0]?.protagonist).toBe(true);
    expect(Number.isFinite(analysis.q)).toBe(true);
    expect(analysis.bounds.radius).toBeGreaterThanOrEqual(310);
    expect(clusters.length).toBeGreaterThanOrEqual(1);
    expect(analysis.singleLeafCount).toBe(2); // c2 (under c1) and l1 (under e1)

    const ego1 = buildEgoSubgraph(
      { nodes: doc.nodes, edges: doc.edges, analysis, clusters },
      0,
      1,
      { Person: true, Concept: true, Event: true, Location: true, Other: true },
    );
    expect(ego1).not.toBeNull();
    expect(ego1!.nodes.map((n) => n.hop)).toEqual([0, 1, 1]);

    const ego2 = buildEgoSubgraph(
      { nodes: doc.nodes, edges: doc.edges, analysis, clusters },
      0,
      2,
      { Person: true, Concept: true, Event: true, Location: true, Other: true },
    );
    expect(ego2!.nodes).toHaveLength(5);

    const rewired = rewireEdges(doc.edges, 42);
    expect(rewired).toHaveLength(doc.edges.length);
  });

  it("packs 2257-node memory graph with organic Barnes-Hut forces, leaf metadata, and cluster titles", () => {
    const raw = buildSyntheticMemoryGraph(2257);
    const doc = parseGraphJsonText(JSON.stringify(raw), "large.json");
    const analysis = analyzeGraph(doc.nodes, doc.edges, true);
    const clusters = layoutRadialGraph(doc.nodes, doc.edges, analysis);

    expect(analysis.clear).toBe(true);
    expect(analysis.q).toBeGreaterThan(0.45);
    expect(analysis.hubs.length).toBeGreaterThanOrEqual(2);
    expect(analysis.singleLeafCount).toBeGreaterThan(100);

    const islands = clusters.filter((c) => !c.loose);
    expect(islands.length).toBeGreaterThanOrEqual(8);
    expect(islands.every((c) => (c.title || "").length > 0)).toBe(true);

    // Every island contour remains well separated.
    for (let i = 0; i < islands.length; i += 1) {
      const a = islands[i]!;
      expect(a.rx).toBe(a.ry);
      for (let j = i + 1; j < islands.length; j += 1) {
        const b = islands[j]!;
        const dist = Math.hypot(a.x - b.x, a.y - b.y);
        expect(dist).toBeGreaterThanOrEqual(a.rx + b.rx + 20);
      }
    }

    for (const node of doc.nodes) {
      expect(Number.isFinite(node.x)).toBe(true);
      expect(Number.isFinite(node.y)).toBe(true);
    }
  });

  it("extracts Event subtypes, HAS_ATTITUDE sentiments, and unifies equivalence concept groups", () => {
    const raw = {
      nodes: [
        { id: "Person:主角", label: "Person", name: "主角" },
        { id: "Event:m1", label: "Event", name: "", summary: "加入新团队", type: "milestone" },
        { id: "Event:i1", label: "Event", summary: "晚饭散步", type: "interaction" },
        { id: "Concept:c1", label: "Concept", name: "胶片摄影", equivalence_group: "eq-photo" },
        { id: "Concept:c2", label: "Concept", name: "胶卷拍摄", equivalence_group: "eq-photo" },
        { id: "Concept:c3", label: "Concept", name: "加班", equivalence_group: "c3" },
      ],
      relationships: [
        { source: "Person:主角", target: "Event:m1", type: "PARTICIPATED_IN" },
        { source: "Person:主角", target: "Event:i1", type: "PARTICIPATED_IN" },
        { source: "Event:m1", target: "Concept:c1", type: "MENTIONS" },
        { source: "Event:i1", target: "Concept:c2", type: "MENTIONS" },
        {
          source: "Person:主角",
          target: "Concept:c1",
          type: "HAS_ATTITUDE",
          properties: { sentiment: "喜欢" },
        },
        {
          source: "Person:主角",
          target: "Concept:c3",
          type: "HAS_ATTITUDE",
          properties: { sentiment: "讨厌" },
        },
      ],
    };

    const doc = parseGraphJsonText(JSON.stringify(raw), "schema.json");
    const milestone = doc.nodes.find((n) => n.originalId === "Event:m1")!;
    const interaction = doc.nodes.find((n) => n.originalId === "Event:i1")!;
    const c1 = doc.nodes.find((n) => n.originalId === "Concept:c1")!;
    const c2 = doc.nodes.find((n) => n.originalId === "Concept:c2")!;
    const c3 = doc.nodes.find((n) => n.originalId === "Concept:c3")!;

    expect(milestone.name).toBe("加入新团队");
    expect(milestone.eventKind).toBe("milestone");
    expect(interaction.eventKind).toBe("interaction");
    expect(c1.equivalenceGroup).toBe("eq-photo");
    expect(doc.edges.find((e) => e.type === "HAS_ATTITUDE" && e.b === c1.id)?.sentiment).toBe("喜欢");
    expect(doc.edges.find((e) => e.sentiment === "讨厌")?.sentiment).toBe("讨厌");

    const analysis = analyzeGraph(doc.nodes, doc.edges, true);
    const clusters = layoutRadialGraph(doc.nodes, doc.edges, analysis);
    expect(milestone.anchor).toBe(true);
    expect(milestone.r).toBeGreaterThan(interaction.r);
    expect(c1.c).toBe(c2.c);
    // Multi-member equivalence group metadata & singleton isolation
    expect(analysis.equivalenceGroupCount).toBe(1);
    expect(c1.eqRepId).toBe(c1.id);
    expect(c2.eqRepId).toBe(c1.id);
    expect(c1.eqGroupSize).toBe(2);
    expect(c2.isSingleLeaf).toBe(false);
    expect(c3.eqRepId).toBeNull();
    expect(c3.eqGroupSize).toBe(1);

    // Ego view opened on c1 pulls in both sibling c2 and c2's interaction event
    const ego = buildEgoSubgraph(
      { nodes: doc.nodes, edges: doc.edges, analysis, clusters },
      c1.id,
      1,
      { Person: true, Concept: true, Event: true, Location: true, Other: true },
    );
    expect(ego).not.toBeNull();
    const egoIds = new Set(ego!.nodes.map((item) => item.node.id));
    expect(egoIds.has(c2.id)).toBe(true);
    expect(egoIds.has(interaction.id)).toBe(true);
    expect(ego!.edges.some((e) => e.edge.type === "EQUIVALENCE_SIBLING")).toBe(true);
  });
});
