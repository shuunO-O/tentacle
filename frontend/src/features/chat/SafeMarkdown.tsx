import { useState, type ComponentPropsWithoutRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Copy, Download, FileText } from "lucide-react";
import { apiDownloadFile } from "../../api/client";
import { splitFileMarkers } from "./fileMarkers";

const SAFE_LINK_RE = /^(https?:\/\/|mailto:|\/(?!\/)|#)/i;

export function safeUrlTransform(url: string): string {
  const trimmed = url.trim();
  if (SAFE_LINK_RE.test(trimmed)) {
    return trimmed;
  }
  return "";
}

export function FileDownloadChip({ path, label }: { path: string; label?: string }) {
  const [status, setStatus] = useState<"idle" | "downloading" | "done" | "error">("idle");
  const [errorText, setErrorText] = useState("");
  const displayName = label || path.split("/").pop() || path;

  async function handleDownload() {
    if (status === "downloading") return;
    setStatus("downloading");
    setErrorText("");
    try {
      await apiDownloadFile(path);
      setStatus("done");
    } catch (err) {
      setStatus("error");
      setErrorText(err instanceof Error ? err.message : "下载失败，请重试");
    }
  }

  const actionLabel =
    status === "downloading"
      ? "正在下载…"
      : status === "done"
        ? "再次下载"
        : status === "error"
          ? errorText
          : "下载文件";

  return (
    <button
      type="button"
      onClick={() => void handleDownload()}
      disabled={status === "downloading"}
      data-file-path={path}
      className="my-1.5 inline-flex max-w-full items-center gap-2.5 rounded-xl border border-border bg-elevated px-3 py-2 text-left text-xs transition-colors hover:border-accent/50 hover:bg-hover disabled:opacity-60"
    >
      <FileText className="h-4 w-4 shrink-0 text-accent" />
      <span className="truncate font-medium text-fg">{displayName}</span>
      <span className="inline-flex shrink-0 items-center gap-1 rounded-md bg-surface px-2 py-0.5 text-[11px] text-fg-dim">
        <Download className="h-3 w-3" />
        {actionLabel}
      </span>
    </button>
  );
}

function CodeBlock({
  className,
  children,
  ...props
}: ComponentPropsWithoutRef<"code"> & { node?: unknown }) {
  const [copied, setCopied] = useState(false);
  const raw = String(children ?? "");
  const isBlock = Boolean(className) || raw.includes("\n");

  if (!isBlock) {
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  }

  const lang = className?.replace(/^language-/, "") || "";
  const cleanCode = raw.replace(/\n$/, "");

  async function copyCode() {
    try {
      await navigator.clipboard.writeText(cleanCode);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard unavailable in restricted contexts.
    }
  }

  return (
    <div className="group relative my-2.5 overflow-hidden rounded-xl border border-border bg-code-bg">
      <div className="flex items-center justify-between border-b border-border-soft bg-elevated/60 px-3 py-1.5 text-[11px] text-fg-dim">
        <span className="font-mono">{lang || "code"}</span>
        <button
          type="button"
          onClick={() => void copyCode()}
          className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 hover:bg-hover hover:text-fg"
          aria-label="复制代码"
        >
          {copied ? <Check className="h-3 w-3 text-ok" /> : <Copy className="h-3 w-3" />}
          <span>{copied ? "已复制" : "复制"}</span>
        </button>
      </div>
      <pre className="!my-0 !border-0 !bg-transparent">
        <code className={className} {...props}>
          {cleanCode}
        </code>
      </pre>
    </div>
  );
}

export interface SafeMarkdownProps {
  text: string;
  allowFiles?: boolean;
  isStreaming?: boolean;
}

export function SafeMarkdown({ text, allowFiles = false, isStreaming = false }: SafeMarkdownProps) {
  const parts = allowFiles ? splitFileMarkers(text) : [{ kind: "text" as const, text }];

  return (
    <div className="md-prose">
      {parts.map((part, idx) => {
        if (part.kind === "file") {
          return <FileDownloadChip key={`file-${idx}-${part.path}`} path={part.path} />;
        }
        return (
          <ReactMarkdown
            key={`md-${idx}`}
            remarkPlugins={[remarkGfm]}
            urlTransform={safeUrlTransform}
            components={{
              a: ({ href, children, ...props }) => {
                if (!href) {
                  return <span className="text-fg-dim">{children}</span>;
                }
                return (
                  <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                    {children}
                  </a>
                );
              },
              pre: ({ children }) => <>{children}</>,
              code: CodeBlock,
            }}
          >
            {part.text}
          </ReactMarkdown>
        );
      })}
      {isStreaming && <span className="stream-caret" aria-hidden="true" />}
    </div>
  );
}
