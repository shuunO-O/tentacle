import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
} from "react";
import { ArrowUp, FileText, Plus, Square, X } from "lucide-react";
import { apiUploadFile, mediaContentUrl } from "../../api/client";
import { Button, cn } from "../../components/ui/Button";
import { getChatTarget, useChatStore } from "./chatStore";

interface PendingAttachment {
  clientId: string;
  name: string;
  size: number;
  uploading: boolean;
  local_id?: string;
  path?: string;
  mime?: string;
}

const MAX_FILES_PER_ROUND = 10;
const MAX_FILE_BYTES = 64 * 1024 * 1024;

export interface ComposerProps {
  draftText: string;
  onDraftChange: (value: string) => void;
}

export function Composer({ draftText, onDraftChange }: ComposerProps) {
  const sending = useChatStore((s) => s.sending);
  const activeRound = useChatStore((s) => s.activeRound);
  const pendingRequest = useChatStore((s) => s.pendingRequest);
  const chatSyncing = useChatStore((s) => s.chatSyncing);
  const commands = useChatStore((s) => s.commands);
  const sendMessage = useChatStore((s) => s.sendMessage);
  const stopRound = useChatStore((s) => s.stopRound);
  const appendLocalError = useChatStore((s) => s.appendLocalError);

  const [pendingFiles, setPendingFiles] = useState<PendingAttachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const [paletteIndex, setPaletteIndex] = useState(0);
  const [paletteDismissed, setPaletteDismissed] = useState(false);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const target = getChatTarget({ pendingRequest, activeRound });

  const commandMatches = useMemo(() => {
    if (paletteDismissed) return [];
    const trimmed = draftText.trimStart().toLowerCase();
    if (!trimmed.startsWith("/") || trimmed.includes("\n")) return [];
    return commands.filter((cmd) => cmd.value.toLowerCase().startsWith(trimmed));
  }, [commands, draftText, paletteDismissed]);

  useEffect(() => {
    setPaletteDismissed(false);
  }, [draftText]);

  useEffect(() => {
    setPaletteIndex(0);
  }, [commandMatches.length, draftText]);

  // Auto-resize composer textarea cleanly.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
  }, [draftText]);

  async function uploadFiles(fileList: FileList | File[]) {
    const incoming = Array.from(fileList);
    for (const file of incoming) {
      if (file.size > MAX_FILE_BYTES) {
        appendLocalError("单个文件不能超过 64 MiB");
        continue;
      }

      let duplicateOrOverLimit = false;
      const clientId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;

      setPendingFiles((prev) => {
        if (prev.some((p) => p.name === file.name && p.size === file.size && p.uploading)) {
          duplicateOrOverLimit = true;
          return prev;
        }
        if (prev.length >= MAX_FILES_PER_ROUND) {
          duplicateOrOverLimit = true;
          appendLocalError("单轮最多 10 个附件");
          return prev;
        }
        return [...prev, { clientId, name: file.name, size: file.size, uploading: true }];
      });

      if (duplicateOrOverLimit) continue;

      try {
        const record = await apiUploadFile(file);
        setPendingFiles((prev) =>
          prev.map((item) =>
            item.clientId === clientId
              ? {
                  ...item,
                  uploading: false,
                  local_id: record.local_id,
                  path: record.path,
                  mime: record.mime,
                }
              : item,
          ),
        );
      } catch (err) {
        setPendingFiles((prev) => prev.filter((item) => item.clientId !== clientId));
        if (!(err instanceof Error && err.message === "unauthorized")) {
          appendLocalError(`${file.name}: ${err instanceof Error ? err.message : "上传失败"}`);
        }
      }
    }
  }

  function applyCommandSelection(index = paletteIndex) {
    const chosen = commandMatches[index];
    if (!chosen) return false;
    onDraftChange(chosen.value + " ");
    window.setTimeout(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        const len = el.value.length;
        el.setSelectionRange(len, len);
      }
    }, 0);
    return true;
  }

  async function handleSend() {
    if (target) {
      await stopRound();
      return;
    }
    if (sending || chatSyncing) return;
    if (pendingFiles.some((f) => f.uploading)) {
      appendLocalError("附件还在上传中，请稍候…");
      return;
    }
    const ready = pendingFiles.filter((f) => !f.uploading && f.local_id);
    if (!draftText.trim() && !ready.length) return;

    const textToSend = draftText;
    onDraftChange("");
    setPendingFiles([]);

    await sendMessage(
      textToSend,
      ready.map((r) => ({
        local_id: r.local_id!,
        path: r.path || "",
        filename: r.name,
        mime: r.mime,
        size: r.size,
      })),
    );
    textareaRef.current?.focus();
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (commandMatches.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setPaletteIndex((i) => (i + 1) % commandMatches.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setPaletteIndex((i) => (i - 1 + commandMatches.length) % commandMatches.length);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && draftText.trim() !== commandMatches[paletteIndex]?.value)) {
        e.preventDefault();
        applyCommandSelection();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setPaletteDismissed(true);
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSend();
    }
  }

  function handlePaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    const files: File[] = [];
    for (const item of Array.from(e.clipboardData?.items || [])) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) files.push(f);
      }
    }
    if (files.length) {
      e.preventDefault();
      void uploadFiles(files);
    }
  }

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(false);
    if (e.dataTransfer?.files?.length) {
      void uploadFiles(e.dataTransfer.files);
    }
  }

  const actionLabel = target
    ? "finished" in target && target.finished
      ? "正在更新对话…"
      : target.finishing
        ? "正在保存…"
        : target.stopping
          ? "正在停止…"
          : target.stopError
            ? "重试停止"
            : "停止当前回合"
    : chatSyncing
      ? "正在同步对话…"
      : "发送消息";

  const actionDisabled = target
    ? Boolean(("finished" in target && target.finished) || target.stopping || target.finishing)
    : chatSyncing || (!draftText.trim() && pendingFiles.filter((f) => !f.uploading && f.local_id).length === 0);

  return (
    <div className="pointer-events-none absolute inset-x-0 bottom-0 z-20 bg-gradient-to-t from-bg/85 via-bg/35 to-transparent px-3 pb-3.5 pt-8 md:px-8 md:pb-5">
      <div className="relative mx-auto w-full max-w-[840px]">
        {commandMatches.length > 0 && (
          <div
            role="listbox"
            aria-label="斜杠命令补全"
            data-testid="command-palette"
            className="pointer-events-auto mb-2 max-h-56 overflow-y-auto rounded-2xl border border-border bg-elevated/90 p-1.5 backdrop-blur-xl shadow-[var(--t-shadow-pop)]"
          >
            {commandMatches.map((item, idx) => (
              <button
                key={item.value}
                type="button"
                role="option"
                aria-selected={idx === paletteIndex}
                onClick={() => applyCommandSelection(idx)}
                className={cn(
                  "flex w-full items-center justify-between gap-3 rounded-xl px-3 py-1.5 text-left text-xs transition-colors",
                  idx === paletteIndex ? "bg-accent-soft text-fg" : "text-fg-dim hover:bg-hover",
                )}
              >
                <code className="font-mono font-semibold text-accent">{item.value}</code>
                <span className="truncate">{item.description}</span>
              </button>
            ))}
          </div>
        )}

        {dragging && (
          <div className="mb-2 flex justify-center">
            <span className="rounded-full border border-accent/60 bg-elevated/95 px-3 py-1 text-xs font-medium text-accent backdrop-blur-md shadow-md">
              松开鼠标以上传附件
            </span>
          </div>
        )}

        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
              setDragging(false);
            }
          }}
          onDrop={handleDrop}
          data-testid="chat-composer-pill"
          className={cn(
            "pointer-events-auto rounded-[28px] border bg-elevated/80 px-2.5 py-1.5 backdrop-blur-xl shadow-[var(--t-shadow-pop)] ring-1 ring-white/5 transition-all",
            dragging
              ? "border-accent bg-accent-soft/25"
              : "border-border-strong/75 focus-within:border-accent/80 focus-within:ring-2 focus-within:ring-accent-ring",
          )}
        >
          {pendingFiles.length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-2 px-1.5 pt-1">
              {pendingFiles.map((file) => (
                <div
                  key={file.clientId}
                  className="relative flex items-center gap-2 rounded-xl border border-border bg-surface/90 p-1.5 pr-7 text-xs"
                >
                  {file.uploading ? (
                    <span className="px-2 py-1 text-fg-dim">上传中…</span>
                  ) : file.mime?.startsWith("image/") && file.local_id ? (
                    <img
                      src={mediaContentUrl(file.local_id)}
                      alt={file.name}
                      className="h-10 w-10 rounded-lg object-cover"
                    />
                  ) : (
                    <span className="inline-flex items-center gap-1.5 px-1.5 text-fg">
                      <FileText className="h-3.5 w-3.5 text-accent" />
                      <span className="max-w-[140px] truncate">{file.name}</span>
                    </span>
                  )}
                  {!file.uploading && (
                    <button
                      type="button"
                      onClick={() =>
                        setPendingFiles((prev) => prev.filter((item) => item.clientId !== file.clientId))
                      }
                      aria-label={`移除 ${file.name}`}
                      className="absolute right-1 top-1 rounded-md p-0.5 text-fg-dim hover:bg-hover hover:text-fg"
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}

          <div className="flex items-end gap-1.5">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              data-testid="chat-file-input"
              onChange={(e) => {
                if (e.target.files?.length) {
                  void uploadFiles(e.target.files);
                }
                e.target.value = "";
              }}
            />
            <Button
              type="button"
              size="icon"
              variant="ghost"
              onClick={() => fileInputRef.current?.click()}
              title="上传附件（支持拖拽或 Ctrl+V 粘贴文件/图片）"
              aria-label="上传附件"
              className="h-9 w-9 shrink-0 rounded-full text-fg-dim hover:bg-hover/80 hover:text-fg"
            >
              <Plus className="h-[18px] w-[18px]" />
            </Button>

            <textarea
              ref={textareaRef}
              rows={1}
              data-testid="chat-input"
              value={draftText}
              onChange={(e) => onDraftChange(e.target.value)}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              placeholder="发消息…（Enter 发送，/ 命令）"
              className="max-h-44 min-h-[36px] flex-1 resize-none border-0 bg-transparent px-1 py-1.5 text-sm leading-6 text-fg placeholder:text-fg-faint focus:outline-none focus:ring-0"
            />

            <Button
              type="button"
              size="icon"
              variant={target ? "danger" : "primary"}
              disabled={actionDisabled}
              onClick={() => void handleSend()}
              title={actionLabel}
              aria-label={actionLabel}
              data-testid="chat-action-btn"
              data-mode={target ? "stop" : "send"}
              className="h-9 w-9 shrink-0 rounded-full"
            >
              {target ? <Square className="h-3.5 w-3.5 fill-current" /> : <ArrowUp className="h-4 w-4" />}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
