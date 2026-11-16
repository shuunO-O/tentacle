import { useEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown, Check, ChevronRight, Copy, Search, Wrench } from "lucide-react";
import { mediaContentUrl } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { cn } from "../../components/ui/Button";
import {
  useChatStore,
  type ArchivedChatMessage,
  type AttachmentInfo,
  type LiveAssistantGroup,
  type LiveErrorEntry,
  type LiveSystemEntry,
  type LiveUserEntry,
  type ToolCallEntry,
  type UsageCounters,
} from "./chatStore";
import type { LightboxImage } from "./MediaLightbox";
import { FileDownloadChip, SafeMarkdown } from "./SafeMarkdown";

const SCROLL_SLACK = 64;

const EMPTY_CHIPS = [
  { cmd: "/help", label: "/help 查看命令" },
  { cmd: "/session", label: "/session 开启新会话" },
  { cmd: "/compress", label: "/compress 整理记忆" },
  { cmd: "/model current", label: "/model 当前模型" },
];

function fmtDateKey(ts?: number): string {
  const d = ts ? new Date(ts * 1000) : new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function fmtDateLabel(ts?: number): string {
  if (!ts) return "今天";
  const d = new Date(ts * 1000);
  const now = new Date();
  if (fmtDateKey(ts) === fmtDateKey(now.getTime() / 1000)) return "今天";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (fmtDateKey(ts) === fmtDateKey(yesterday.getTime() / 1000)) return "昨天";
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

function fmtMsgTime(ts?: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function fmtFullTime(ts?: number): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("zh-CN");
}

function stripFilePlaceholders(text: string, images?: AttachmentInfo[]): string {
  let visible = text;
  for (const file of images || []) {
    if (file.path) {
      visible = visible.replaceAll(`[file:${file.path}]`, "");
    }
  }
  return visible.trim();
}

function CopyIconButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false);

  async function onCopy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Ignore clipboard failure in non-HTTPS contexts.
    }
  }

  return (
    <button
      type="button"
      onClick={() => void onCopy()}
      aria-label={label}
      title={label}
      className="inline-flex h-6 w-6 items-center justify-center rounded-md text-fg-faint transition-colors hover:bg-hover hover:text-fg"
    >
      {copied ? <Check className="h-3.5 w-3.5 text-ok" /> : <Copy className="h-3.5 w-3.5" />}
    </button>
  );
}

function AttachmentGallery({
  images,
  onOpenImage,
}: {
  images?: AttachmentInfo[];
  onOpenImage: (img: LightboxImage) => void;
}) {
  if (!images || !images.length) return null;

  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {images.map((item, idx) => {
        const localId = item.local_id || "";
        const mime = item.mime || "";
        if (!localId) {
          return (
            <div
              key={`broken-${idx}`}
              className="rounded-lg border border-border bg-elevated px-3 py-2 text-xs text-fg-faint"
            >
              图片
            </div>
          );
        }
        const url = mediaContentUrl(localId);
        if (mime.startsWith("audio/") || mime.startsWith("video/")) {
          return (
            <div
              key={`${localId}-${idx}`}
              className="flex max-w-full flex-col gap-1 rounded-xl border border-border bg-surface p-2 text-xs"
            >
              <span className="truncate text-fg-dim">
                {item.filename || (mime.startsWith("audio/") ? "音频" : "视频")}
              </span>
              {mime.startsWith("audio/") ? (
                <audio controls preload="none" src={url} className="max-w-xs" />
              ) : (
                <video controls preload="none" src={url} className="max-h-56 max-w-xs rounded-lg" />
              )}
            </div>
          );
        }
        if (mime && !mime.startsWith("image/")) {
          return (
            <FileDownloadChip
              key={`${localId}-${idx}`}
              path={item.path || localId}
              label={item.filename || "文件"}
            />
          );
        }
        return (
          <ImageThumb
            key={`${localId}-${idx}`}
            localId={localId}
            filename={item.filename || undefined}
            onOpen={() => onOpenImage({ localId, filename: item.filename || undefined })}
          />
        );
      })}
    </div>
  );
}

function ImageThumb({
  localId,
  filename,
  onOpen,
}: {
  localId: string;
  filename?: string;
  onOpen: () => void;
}) {
  const [broken, setBroken] = useState(false);

  if (broken) {
    return (
      <div className="rounded-lg border border-border bg-elevated px-3 py-2 text-xs text-fg-faint">
        图片已删除
      </div>
    );
  }

  return (
    <button
      type="button"
      onClick={onOpen}
      className="group overflow-hidden rounded-xl border border-border bg-elevated transition-transform hover:scale-[1.01]"
      title={filename || "查看大图"}
    >
      <img
        src={mediaContentUrl(localId)}
        alt={filename || "附件图片"}
        loading="lazy"
        onError={() => setBroken(true)}
        className="max-h-48 max-w-[260px] object-cover"
      />
    </button>
  );
}

function AssistantBlock({
  time,
  reasoning,
  tools,
  segments,
  usage,
  interrupted,
  pending,
  showReasoning,
  isStreamingGroup,
}: {
  time?: number;
  reasoning?: string;
  tools?: ToolCallEntry[];
  segments: Array<{
    id: string;
    text: string;
    platform?: string;
    streaming?: boolean;
  }>;
  usage?: UsageCounters;
  interrupted?: boolean;
  pending?: boolean;
  showReasoning: boolean;
  isStreamingGroup?: boolean;
}) {
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);

  const visibleTools = useMemo(
    () =>
      (tools || []).filter(
        (t) => !t.name?.startsWith("subagent_") || (t.result && String(t.result).includes('"error"')),
      ),
    [tools],
  );

  // Skip rows that only contain hidden subagent_* tool calls and nothing else.
  if (
    !pending &&
    !reasoning &&
    !segments.length &&
    (tools?.length || 0) > 0 &&
    visibleTools.length === 0
  ) {
    return null;
  }

  const copyPayload = segments.map((s) => s.text).filter(Boolean).join("\n\n");

  return (
    <article data-msg-role="agent" className="group flex flex-col items-start gap-2 py-2.5">
      <div className="w-full max-w-[820px] space-y-2">
        {pending && !reasoning && (
          <div className="inline-flex items-center gap-2 rounded-xl border border-border-soft bg-elevated px-3 py-1.5 text-xs text-fg-dim">
            <span className="h-2 w-2 animate-pulse rounded-full bg-accent" />
            <span>正在思考…</span>
          </div>
        )}

        {showReasoning && reasoning && (
          <details
            open={reasoningOpen}
            onToggle={(e) => setReasoningOpen((e.currentTarget as HTMLDetailsElement).open)}
            className="rounded-xl border border-border-soft bg-elevated/60 px-3 py-2 text-xs"
          >
            <summary className="flex cursor-pointer list-none items-center gap-1.5 font-medium text-fg-dim select-none hover:text-fg">
              <ChevronRight
                className={cn("h-3.5 w-3.5 transition-transform", reasoningOpen && "rotate-90")}
              />
              <span>思考过程</span>
            </summary>
            <div className="mt-2 border-t border-border-soft pt-2 text-fg-dim">
              <SafeMarkdown text={reasoning} />
            </div>
          </details>
        )}

        {visibleTools.length > 0 && (
          <details
            open={toolsOpen}
            onToggle={(e) => setToolsOpen((e.currentTarget as HTMLDetailsElement).open)}
            className="rounded-xl border border-border-soft bg-elevated/60 px-3 py-2 text-xs"
          >
            <summary className="flex cursor-pointer list-none items-center gap-1.5 font-medium text-fg-dim select-none hover:text-fg">
              <ChevronRight
                className={cn("h-3.5 w-3.5 transition-transform", toolsOpen && "rotate-90")}
              />
              <Wrench className="h-3 w-3" />
              <span>工具调用（{visibleTools.length}）</span>
            </summary>
            <div className="mt-2 space-y-2 border-t border-border-soft pt-2">
              {visibleTools.map((tool) => (
                <div
                  key={tool.id}
                  className="rounded-lg border border-border-soft bg-surface p-2.5 text-xs"
                >
                  <div className="mb-1 flex items-center gap-1.5 font-mono font-medium text-fg">
                    {tool.isWebSearch && <Search className="h-3 w-3 text-accent" />}
                    <span>{tool.name}</span>
                  </div>
                  {tool.arguments && (
                    <pre className="max-h-40 overflow-auto rounded bg-code-bg p-2 font-mono text-[11px] text-fg-dim">
                      {tool.arguments}
                    </pre>
                  )}
                  {tool.result !== null && (
                    <pre className="mt-1.5 max-h-48 overflow-auto rounded bg-code-bg p-2 font-mono text-[11px] text-fg">
                      {tool.result}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          </details>
        )}

        {segments.map((seg) => (
          <div
            key={seg.id}
            data-agent-segment
            className="rounded-2xl rounded-tl-sm border border-border-soft bg-surface px-4 py-3 text-fg shadow-xs"
          >
            {seg.platform && seg.platform !== "web" ? (
              <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
                <Badge>{seg.platform}</Badge>
              </div>
            ) : null}
            <SafeMarkdown
              text={seg.text}
              allowFiles
              isStreaming={Boolean(seg.streaming && isStreamingGroup)}
            />
          </div>
        ))}

        {!pending && !segments.length && !reasoning && !tools?.length && (
          <div className="px-2 text-xs text-fg-faint">…</div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-2 px-1 text-[11px] text-fg-faint">
        {time && <span title={fmtFullTime(time)}>{fmtMsgTime(time)}</span>}
        {usage && usage.total_tokens > 0 && (
          <span
            data-testid="msg-token-count"
            title={
              usage.prompt_tokens > 0 || usage.completion_tokens > 0
                ? `输入 ${usage.prompt_tokens.toLocaleString()} · 输出 ${usage.completion_tokens.toLocaleString()} · 总计 ${usage.total_tokens.toLocaleString()} tok`
                : `总计 ${usage.total_tokens.toLocaleString()} tok`
            }
            className="font-mono text-[11px] text-fg-faint"
          >
            {usage.total_tokens.toLocaleString()} tok
          </span>
        )}
        {interrupted && <Badge variant="warn">已中断</Badge>}
        {copyPayload && (
          <span className="opacity-70 transition-opacity group-hover:opacity-100">
            <CopyIconButton text={copyPayload} label="复制这条回复" />
          </span>
        )}
      </div>
    </article>
  );
}

type TimelineRow =
  | { key: string; kind: "date"; label: string }
  | { key: string; kind: "archived_user"; message: ArchivedChatMessage }
  | { key: string; kind: "archived_system"; message: ArchivedChatMessage }
  | { key: string; kind: "archived_assistant"; message: ArchivedChatMessage }
  | { key: string; kind: "live_user"; entry: LiveUserEntry }
  | { key: string; kind: "live_system"; entry: LiveSystemEntry }
  | { key: string; kind: "live_error"; entry: LiveErrorEntry }
  | { key: string; kind: "live_assistant"; entry: LiveAssistantGroup };

export interface MessageTimelineProps {
  onFillPrompt: (cmd: string) => void;
  onOpenImage: (img: LightboxImage) => void;
}

export function MessageTimeline({ onFillPrompt, onOpenImage }: MessageTimelineProps) {
  const archivedMessages = useChatStore((s) => s.archivedMessages);
  const liveEntries = useChatStore((s) => s.liveEntries);
  const usageByRound = useChatStore((s) => s.usageByRound);
  const hasMore = useChatStore((s) => s.hasMore);
  const historyLimit = useChatStore((s) => s.historyLimit);
  const loadingMore = useChatStore((s) => s.loadingMore);
  const loadHistory = useChatStore((s) => s.loadHistory);
  const settings = useChatStore((s) => s.settings);
  const currentGroupId = useChatStore((s) => s.currentGroupId);
  const scrollRequestNonce = useChatStore((s) => s.scrollRequestNonce);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);
  const [isPinned, setIsPinned] = useState(true);
  const [unreadCount, setUnreadCount] = useState(0);
  const prevRowCountRef = useRef(0);
  const preserveScrollRef = useRef<{ prevHeight: number; prevTop: number } | null>(null);

  const rows = useMemo<TimelineRow[]>(() => {
    const out: TimelineRow[] = [];
    let lastDateKey = "";

    const maybePushDate = (ts?: number, salt?: string) => {
      const key = fmtDateKey(ts);
      if (key !== lastDateKey) {
        lastDateKey = key;
        out.push({ key: `date-${key}-${salt || out.length}`, kind: "date", label: fmtDateLabel(ts) });
      }
    };

    for (const msg of archivedMessages) {
      maybePushDate(msg.created_time, String(msg.id));
      if (msg.role === "user") {
        if (msg.platform === "system") {
          out.push({ key: `arch-sys-${msg.id}`, kind: "archived_system", message: msg });
        } else {
          out.push({ key: `arch-usr-${msg.id}`, kind: "archived_user", message: msg });
        }
      } else if (msg.role === "assistant") {
        out.push({ key: `arch-ast-${msg.id}`, kind: "archived_assistant", message: msg });
      }
    }

    for (const entry of liveEntries) {
      if (entry.kind === "system") {
        if (entry.time) maybePushDate(entry.time, entry.id);
        out.push({ key: entry.id, kind: "live_system", entry });
      } else if (entry.kind === "user") {
        maybePushDate(entry.time, entry.id);
        out.push({ key: entry.id, kind: "live_user", entry });
      } else if (entry.kind === "error") {
        maybePushDate(entry.time, entry.id);
        out.push({ key: entry.id, kind: "live_error", entry });
      } else {
        maybePushDate(entry.time, entry.id);
        out.push({ key: entry.id, kind: "live_assistant", entry });
      }
    }

    return out;
  }, [archivedMessages, liveEntries]);

  const useVirtual = rows.length > 60 && (scrollRef.current?.clientHeight ?? 0) > 0;

  const virtualizer = useVirtualizer({
    count: useVirtual ? rows.length : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 96,
    overscan: 10,
    initialRect: { width: 800, height: 640 },
  });

  function renderTimelineRow(row: TimelineRow) {
    switch (row.kind) {
      case "date":
        return (
          <div className="my-3 flex items-center justify-center">
            <span className="rounded-full border border-border-soft bg-elevated px-3 py-0.5 text-[11px] text-fg-dim">
              {row.label}
            </span>
          </div>
        );

      case "archived_system":
      case "live_system":
        return (
          <div className="my-2 flex justify-center">
            <div
              className="max-w-lg rounded-xl border border-border-soft bg-elevated/70 px-3.5 py-1.5 text-center text-xs text-fg-dim"
              title={
                row.kind === "archived_system"
                  ? fmtFullTime(row.message.created_time)
                  : row.entry.time
                    ? fmtFullTime(row.entry.time)
                    : undefined
              }
            >
              {row.kind === "archived_system" ? row.message.content : row.entry.text}
            </div>
          </div>
        );

      case "live_error":
        return (
          <div className="my-2">
            <div
              role="alert"
              className="rounded-xl border border-danger/35 bg-danger-soft px-3.5 py-2.5 text-xs text-danger"
            >
              {row.entry.text}
            </div>
          </div>
        );

      case "archived_user":
      case "live_user": {
        const rawText = row.kind === "archived_user" ? row.message.content : row.entry.text;
        const images = row.kind === "archived_user" ? row.message.images : row.entry.images;
        const platform = row.kind === "archived_user" ? row.message.platform : row.entry.platform;
        const time = row.kind === "archived_user" ? row.message.created_time : row.entry.time;
        const visibleText = stripFilePlaceholders(rawText, images);

        return (
          <article data-msg-role="user" className="group flex flex-col items-end gap-1.5 py-2">
            <div className="max-w-[82%] rounded-2xl rounded-tr-sm border border-border-soft bg-user-bubble px-4 py-2.5 text-sm text-fg shadow-xs">
              {platform && platform !== "web" && (
                <div className="mb-1">
                  <Badge>{platform}</Badge>
                </div>
              )}
              {visibleText && (
                <div className="whitespace-pre-wrap break-words leading-relaxed">
                  {visibleText}
                </div>
              )}
              <AttachmentGallery images={images} onOpenImage={onOpenImage} />
            </div>
            <div className="flex items-center gap-2 px-1 text-[11px] text-fg-faint">
              <span className="opacity-0 transition-opacity group-hover:opacity-100">
                <CopyIconButton text={visibleText || rawText} label="复制这条消息" />
              </span>
              <span title={fmtFullTime(time)}>{fmtMsgTime(time)}</span>
            </div>
          </article>
        );
      }

      case "archived_assistant": {
        const m = row.message;
        const rawSegs =
          m.segments && m.segments.length ? m.segments : m.content ? [m.content] : [];
        return (
          <AssistantBlock
            time={m.created_time}
            usage={m.usage || (m.round_id ? usageByRound[m.round_id] : undefined)}
            interrupted={m.interrupted}
            showReasoning={Boolean(settings.show_reasoning)}
            segments={rawSegs.map((text, idx) => ({
              id: `${m.id}-seg-${idx}`,
              text,
              platform: m.platform,
            }))}
          />
        );
      }

      case "live_assistant":
        return (
          <AssistantBlock
            time={row.entry.time}
            reasoning={row.entry.reasoning}
            tools={row.entry.tools}
            usage={
              row.entry.usage ||
              (row.entry.roundId ? usageByRound[row.entry.roundId] : undefined) ||
              (row.entry.requestId ? usageByRound[row.entry.requestId] : undefined)
            }
            pending={row.entry.pending}
            interrupted={row.entry.interrupted}
            showReasoning={Boolean(settings.show_reasoning)}
            isStreamingGroup={row.entry.id === currentGroupId}
            segments={row.entry.bubbles}
          />
        );
    }
  }

  function scrollToBottom(smooth = false) {
    const el = scrollRef.current;
    if (!el) return;
    if (useVirtual && rows.length > 0) {
      virtualizer.scrollToIndex(rows.length - 1, { align: "end" });
    }
    el.scrollTo({
      top: el.scrollHeight,
      behavior: smooth ? "smooth" : "auto",
    });
    pinnedRef.current = true;
    setIsPinned(true);
    setUnreadCount(0);
  }

  function handleScroll() {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= SCROLL_SLACK;
    pinnedRef.current = atBottom;
    setIsPinned(atBottom);
    if (atBottom) setUnreadCount(0);
  }

  // Handle load-earlier scroll preservation & auto-follow on new/streamed content.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    if (preserveScrollRef.current) {
      const { prevHeight, prevTop } = preserveScrollRef.current;
      preserveScrollRef.current = null;
      const delta = el.scrollHeight - prevHeight;
      el.scrollTop = prevTop + Math.max(delta, 0);
      prevRowCountRef.current = rows.length;
      return;
    }

    if (pinnedRef.current) {
      scrollToBottom(false);
    } else if (rows.length > prevRowCountRef.current) {
      setUnreadCount((c) => c + (rows.length - prevRowCountRef.current));
    }
    prevRowCountRef.current = rows.length;
  });

  useEffect(() => {
    if (scrollRequestNonce > 0) {
      pinnedRef.current = true;
      setIsPinned(true);
      setUnreadCount(0);
      scrollToBottom(false);
    }
  }, [scrollRequestNonce]);

  async function handleLoadEarlier() {
    const el = scrollRef.current;
    if (el) {
      preserveScrollRef.current = { prevHeight: el.scrollHeight, prevTop: el.scrollTop };
    }
    await loadHistory({ limit: Math.min(historyLimit + 50, 200) });
  }

  const isEmpty = rows.length === 0;
  const agentName = settings.agent_name || "Tentacle";

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        data-testid="chat-timeline"
        className="flex-1 overflow-y-auto px-4 pt-4 pb-28 md:px-8 md:pb-32"
      >
        <div data-testid="chat-messages" className="mx-auto flex w-full max-w-[840px] flex-col">
          {hasMore && historyLimit < 200 && (
            <div className="mb-4 flex justify-center">
              <Button
                variant="ghost"
                size="sm"
                disabled={loadingMore}
                onClick={() => void handleLoadEarlier()}
              >
                {loadingMore ? "加载中…" : "加载更早的对话"}
              </Button>
            </div>
          )}

          {isEmpty ? (
            <div className="flex min-h-[55vh] flex-col items-center justify-center px-4 text-center">
              <div className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-border bg-elevated text-accent shadow-xs">
                <svg
                  viewBox="0 0 24 24"
                  width="28"
                  height="28"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  aria-hidden="true"
                >
                  <circle cx="12" cy="8" r="4.5" />
                  <path d="M8 12c0 3-1.5 4-3 5M12 12.5c0 3.5-.5 5-1.5 6.5M16 12c0 3 1.5 4 3 5M10 12.5c-.5 3-2 4.5-4 5.5M14 12.5c.5 3 2 4.5 4 5.5" />
                </svg>
              </div>
              <h2 className="text-xl font-semibold text-fg">{agentName}</h2>
              <p className="mt-1.5 max-w-md text-sm text-fg-dim">
                私人对话，记忆保存在本地。直接开始聊天，或输入 <code className="rounded bg-elevated px-1.5 py-0.5 text-xs">/</code> 查看指令。
              </p>
              <div className="mt-5 flex flex-wrap justify-center gap-2">
                {EMPTY_CHIPS.map((chip) => (
                  <button
                    key={chip.cmd}
                    type="button"
                    onClick={() => onFillPrompt(chip.cmd)}
                    className="rounded-full border border-border bg-elevated px-3.5 py-1.5 text-xs text-fg transition-colors hover:border-accent/60 hover:bg-hover"
                  >
                    {chip.label}
                  </button>
                ))}
              </div>
              <p className="mt-6 text-xs text-fg-faint">
                <kbd className="rounded border border-border bg-elevated px-1.5 py-0.5 font-mono">Enter</kbd> 发送 ·{" "}
                <kbd className="rounded border border-border bg-elevated px-1.5 py-0.5 font-mono">Shift+Enter</kbd> 换行 ·{" "}
                <kbd className="rounded border border-border bg-elevated px-1.5 py-0.5 font-mono">Ctrl/⌘+K</kbd> 命令面板
              </p>
            </div>
          ) : useVirtual ? (
            <div
              style={{
                height: `${virtualizer.getTotalSize()}px`,
                width: "100%",
                position: "relative",
              }}
            >
              {virtualizer.getVirtualItems().map((vRow) => {
                const row = rows[vRow.index];
                if (!row) return null;

                return (
                  <div
                    key={row.key}
                    data-index={vRow.index}
                    ref={virtualizer.measureElement}
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      width: "100%",
                      transform: `translateY(${vRow.start}px)`,
                    }}
                  >
                    {renderTimelineRow(row)}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="flex flex-col">
              {rows.map((row) => (
                <div key={row.key}>{renderTimelineRow(row)}</div>
              ))}
            </div>
          )}
        </div>
      </div>

      {!isPinned && (
        <div className="pointer-events-none absolute bottom-[78px] left-1/2 z-20 -translate-x-1/2 md:bottom-[86px]">
          <Button
            size={unreadCount > 0 ? "sm" : "icon"}
            variant="secondary"
            onClick={() => scrollToBottom(true)}
            aria-label={unreadCount > 0 ? `有 ${unreadCount} 条新消息，回到底部` : "回到底部"}
            title="回到底部"
            className={
              unreadCount > 0
                ? "pointer-events-auto h-9 rounded-full border border-border bg-elevated/90 px-3.5 backdrop-blur-md shadow-[var(--t-shadow-soft)] hover:bg-hover"
                : "pointer-events-auto h-9 w-9 rounded-full border border-border bg-elevated/90 backdrop-blur-md shadow-[var(--t-shadow-soft)] hover:bg-hover"
            }
          >
            <ArrowDown className="h-4 w-4" />
            {unreadCount > 0 && <span>有 {unreadCount} 条新消息</span>}
          </Button>
        </div>
      )}
    </div>
  );
}
