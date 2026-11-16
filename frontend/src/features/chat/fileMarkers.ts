/**
 * File marker AST splitter kept aligned with `attachments/outgoing_files.py::split_file_markers`
 * and legacy `web/static/file-markers.js`.
 */

export type FileMarkerPart =
  | { kind: "text"; text: string }
  | { kind: "file"; path: string };

export function splitFileMarkers(text: string): FileMarkerPart[] {
  const parts: FileMarkerPart[] = [];
  let offset = 0;
  let last = 0;
  let fence = "";

  const lines = text.match(/[^\n]*\n|[^\n]+$/g) || [];
  for (const line of lines) {
    const stripped = line.replace(/[\r\n]+$/, "");
    const fenceMatch = stripped.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fenceMatch) {
      const marker = fenceMatch[1] || "";
      const rest = fenceMatch[2] || "";
      if (!fence) {
        fence = marker;
      } else if (marker[0] === fence[0] && marker.length >= fence.length && !rest.trim()) {
        fence = "";
      }
    } else if (!fence) {
      const inline = /(?<!`)(`+)(?!`)[^\r\n]*?(?<!`)\1(?!`)|(?<!\\)\[file:([^\]\r\n]+)\]/g;
      for (const item of line.matchAll(inline)) {
        if (item[2] === undefined || !item[2].trim() || item.index === undefined) continue;
        const start = offset + item.index;
        const end = start + item[0].length;
        if (start > last) {
          parts.push({ kind: "text", text: text.slice(last, start) });
        }
        parts.push({ kind: "file", path: item[2].trim() });
        last = end;
      }
    }
    offset += line.length;
  }

  if (last < text.length) {
    parts.push({ kind: "text", text: text.slice(last) });
  }
  return parts;
}
