import { analyzeGraph } from "./graphEngine";
import { layoutRadialGraph } from "./radialLayout";
import type { ComputedGraphResult, GraphAnalysis, GraphEdge, GraphNode } from "./types";

interface WorkerRequest {
  nodes: GraphNode[];
  edges: GraphEdge[];
  cachedAnalysis: GraphAnalysis | null;
  correctHubs: boolean;
  centerId: number | null;
}

self.onmessage = (ev: MessageEvent<WorkerRequest>) => {
  try {
    const { nodes, edges, cachedAnalysis, correctHubs, centerId } = ev.data;
    const analysis = cachedAnalysis
      ? structuredClone(cachedAnalysis)
      : analyzeGraph(nodes, edges, correctHubs);
    const clusters = layoutRadialGraph(nodes, edges, analysis, { centerId });
    const result: ComputedGraphResult = { nodes, edges, analysis, clusters };
    self.postMessage(result);
  } catch (err) {
    self.postMessage({ error: err instanceof Error ? err.message : "图谱布局计算失败" });
  }
};
