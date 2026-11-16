import { describe, expect, it } from "vitest";
import { splitFileMarkers } from "./fileMarkers";

describe("splitFileMarkers", () => {
  it("parses file markers while skipping code fences, inline code, and escaped brackets", () => {
    const input = [
      "查看结果：[file:data/files/out.pdf]",
      "行内示例 `[file:ignored.txt]` 和转义 \\[file:escaped.txt]",
      "```markdown",
      "[file:inside_fence.txt]",
      "```",
      "最后一个 [file:reports/summary.md] 完成",
    ].join("\n");

    const parts = splitFileMarkers(input);
    const files = parts.filter((p) => p.kind === "file").map((p) => p.path);
    expect(files).toEqual(["data/files/out.pdf", "reports/summary.md"]);
  });

  it("supports tilde code fences", () => {
    const input = "~~~\n[file:nope.bin]\n~~~\n[file:real.bin]";
    const files = splitFileMarkers(input)
      .filter((p) => p.kind === "file")
      .map((p) => p.path);
    expect(files).toEqual(["real.bin"]);
  });
});
