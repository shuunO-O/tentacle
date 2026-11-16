import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { useLayoutStore } from "../workbench/layoutStore";
import { useChatStore } from "./chatStore";
import { Composer } from "./Composer";
import { MediaLightbox, type LightboxImage } from "./MediaLightbox";
import { MessageTimeline } from "./MessageTimeline";

export function ChatPanel() {
  const settings = useChatStore((s) => s.settings);
  const wsStatus = useChatStore((s) => s.wsStatus);
  const chatSyncing = useChatStore((s) => s.chatSyncing);
  const synchronizeChat = useChatStore((s) => s.synchronizeChat);
  const loadCommands = useChatStore((s) => s.loadCommands);
  const pendingChatPrompt = useLayoutStore((s) => s.pendingChatPrompt);
  const consumeChatPrompt = useLayoutStore((s) => s.consumeChatPrompt);

  const [draftText, setDraftText] = useState("");
  const [lightboxImage, setLightboxImage] = useState<LightboxImage | null>(null);

  useEffect(() => {
    void loadCommands();
  }, [loadCommands]);

  useEffect(() => {
    if (pendingChatPrompt !== null) {
      const seeded = consumeChatPrompt();
      if (seeded !== null) {
        setDraftText(seeded);
      }
    }
  }, [pendingChatPrompt, consumeChatPrompt]);

  const wsLabel =
    wsStatus === "connected"
      ? "实时已连接"
      : wsStatus === "reconnecting"
        ? "重连中…"
        : wsStatus === "connecting"
          ? "连接中…"
          : "未连接";

  const wsDotClass =
    wsStatus === "connected"
      ? "bg-ok ring-4 ring-ok-soft"
      : wsStatus === "reconnecting"
        ? "bg-warn animate-pulse ring-4 ring-warn-soft"
        : wsStatus === "connecting"
          ? "bg-info animate-pulse ring-4 ring-info-soft"
          : "bg-fg-faint/60";

  return (
    <section className="flex h-full min-h-0 flex-col bg-bg">
      <header className="flex shrink-0 items-center justify-between border-b border-border-soft px-4 py-2.5 md:px-6">
        <div className="flex items-center gap-2.5">
          <h1 className="text-sm font-semibold text-fg">{settings.agent_name || "Tentacle"}</h1>
          <span
            role="status"
            aria-label={wsLabel}
            title={wsLabel}
            data-testid="ws-status"
            data-ws-status={wsStatus}
            className={`inline-block h-2.5 w-2.5 shrink-0 rounded-full transition-colors ${wsDotClass}`}
          />
          {chatSyncing && <span className="text-[11px] text-fg-faint">同步对话中…</span>}
        </div>
        <Button
          size="sm"
          variant="ghost"
          disabled={chatSyncing}
          onClick={() => void synchronizeChat()}
          title="重新同步对话快照"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${chatSyncing ? "animate-spin" : ""}`} />
          <span className="hidden sm:inline">同步</span>
        </Button>
      </header>

      <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
        <MessageTimeline
          onFillPrompt={(cmd) => setDraftText(cmd + " ")}
          onOpenImage={setLightboxImage}
        />

        <Composer draftText={draftText} onDraftChange={setDraftText} />
      </div>

      <MediaLightbox image={lightboxImage} onClose={() => setLightboxImage(null)} />
    </section>
  );
}
