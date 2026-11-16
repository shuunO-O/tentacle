import { create } from "zustand";
import { apiFetch, type ApiError, type UploadedFileRecord } from "../../api/client";
import type { components } from "../../api/schema";
import { wsClient, type WsConnectionStatus, type WsEventEnvelope } from "../../api/ws";

export type WebSettingsDto = components["schemas"]["WebSettingsDto"];
export type ChatSendResponse = components["schemas"]["ChatSendResponse"];

export interface AttachmentInfo {
  local_id?: string | null;
  filename?: string | null;
  path?: string | null;
  mime?: string | null;
}

export interface ArchivedChatMessage {
  id: string | number;
  role: "user" | "assistant";
  content: string;
  segments?: string[];
  platform?: string;
  created_time: number;
  round_id?: string | null;
  images?: AttachmentInfo[];
  interrupted?: boolean;
  usage?: UsageCounters;
}

export interface ToolCallEntry {
  id: string;
  name: string;
  arguments: string;
  result: string | null;
  isWebSearch?: boolean;
  searchId?: string;
}

export interface LiveBubbleSegment {
  id: string;
  text: string;
  platform?: string;
  streaming: boolean;
}

export interface LiveAssistantGroup {
  kind: "assistant_group";
  id: string;
  time: number;
  reasoning: string;
  tools: ToolCallEntry[];
  bubbles: LiveBubbleSegment[];
  pending: boolean;
  closed: boolean;
  interrupted?: boolean;
  roundId: string | null;
  requestId: string | null;
  usage?: UsageCounters;
}

export interface LiveUserEntry {
  kind: "user";
  id: string;
  text: string;
  time: number;
  platform?: string;
  images?: AttachmentInfo[];
  roundId?: string | null;
  requestId?: string | null;
}

export interface LiveSystemEntry {
  kind: "system";
  id: string;
  text: string;
  time?: number;
  transient?: boolean;
}

export interface LiveErrorEntry {
  kind: "error";
  id: string;
  text: string;
  time: number;
}

export type LiveTimelineEntry =
  | LiveAssistantGroup
  | LiveUserEntry
  | LiveSystemEntry
  | LiveErrorEntry;

export interface PendingRequestState {
  id: string;
  roundId: string | null;
  finished: boolean;
  errorShown: boolean;
  needsHistory: boolean;
  stopping: boolean;
  finishing: boolean;
  stopError: boolean;
  uncertain: boolean;
}

export interface ActiveRoundState {
  id?: string | null;
  roundId: string;
  stopping?: boolean;
  finishing?: boolean;
  stopError?: boolean;
}

export interface UsageCounters {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export interface ChatCommandOption {
  value: string;
  description: string;
}

interface SyncRoundInfo {
  round_id: string;
  request_id?: string | null;
  phase?: string | null;
  stopping?: boolean;
  created_time?: number;
  input?: {
    content?: string;
    platform?: string;
    images?: AttachmentInfo[];
  } | null;
}

interface SyncOutcome {
  round_id: string;
  request_id?: string | null;
  status?: string;
  error?: string;
  command?: { text: string; refresh_history?: boolean } | null;
}

interface ChatSyncPayload {
  messages?: ArchivedChatMessage[];
  has_more?: boolean;
  instance_id?: string | null;
  seq?: number;
  rounds?: SyncRoundInfo[];
  events?: WsEventEnvelope[];
  finished?: SyncOutcome[];
}

interface ChatHistoryPayload {
  messages?: ArchivedChatMessage[];
  has_more?: boolean;
}

let uidCounter = 0;
function nextUid(prefix: string): string {
  uidCounter += 1;
  return `${prefix}-${Date.now()}-${uidCounter}`;
}

export function getChatTarget(state: {
  pendingRequest: PendingRequestState | null;
  activeRound: ActiveRoundState | null;
}): PendingRequestState | ActiveRoundState | null {
  return state.pendingRequest || state.activeRound;
}

interface ChatStoreState {
  settings: WebSettingsDto;
  wsStatus: WsConnectionStatus;
  chatSyncing: boolean;
  chatSyncGeneration: number;
  chatInstance: string | null;
  chatSeq: number;
  chatBuffer: WsEventEnvelope[];

  historyLimit: number;
  hasMore: boolean;
  loadingMore: boolean;
  archivedMessages: ArchivedChatMessage[];
  liveEntries: LiveTimelineEntry[];
  currentGroupId: string | null;

  sending: boolean;
  pendingRequest: PendingRequestState | null;
  activeRound: ActiveRoundState | null;
  eventRound: { round_id?: string | null; request_id?: string | null } | null;
  interruptedRounds: string[];
  lastRoundError: string | null;

  usage: UsageCounters | null;
  usageByRound: Record<string, UsageCounters>;
  commands: ChatCommandOption[];
  scrollRequestNonce: number;

  applySettings: (s: WebSettingsDto) => void;
  setWsStatus: (status: WsConnectionStatus) => void;
  resetSessionState: () => void;
  loadCommands: () => Promise<void>;
  loadHistory: (opts?: { limit?: number }) => Promise<void>;
  synchronizeChat: () => Promise<void>;
  receiveWsEvent: (msg: WsEventEnvelope) => void;
  sendMessage: (text: string, uploadedFiles: UploadedFileRecord[]) => Promise<void>;
  stopRound: () => Promise<void>;
  appendLocalError: (text: string) => void;
  requestScrollToBottom: () => void;
}

const DEFAULT_SETTINGS: WebSettingsDto = {
  agent_name: "Tentacle",
  show_reasoning: true,
  split_messages: true,
  has_password: false,
  device_auth: false,
};

let syncRetryTimer: number | null = null;
let historyRequestSeq = 0;

function clearSyncRetry() {
  if (syncRetryTimer !== null) {
    window.clearTimeout(syncRetryTimer);
    syncRetryTimer = null;
  }
}

function isLiveEntryInFlight(
  entry: LiveTimelineEntry,
  state: Pick<ChatStoreState, "currentGroupId" | "activeRound" | "pendingRequest">,
): boolean {
  if (entry.kind === "assistant_group" && state.currentGroupId && entry.id === state.currentGroupId) {
    return true;
  }
  if (entry.kind !== "assistant_group" && entry.kind !== "user") {
    return false;
  }

  const active = state.activeRound;
  if (
    active &&
    ((entry.roundId && entry.roundId === active.roundId) ||
      (active.id && entry.requestId === active.id))
  ) {
    return true;
  }

  const pending =
    state.pendingRequest && !state.pendingRequest.finished ? state.pendingRequest : null;
  return Boolean(
    pending &&
      ((pending.roundId && entry.roundId === pending.roundId) ||
        entry.requestId === pending.id),
  );
}

function accumulateUsage(
  prev: UsageCounters | undefined,
  next: UsageCounters,
): UsageCounters {
  return {
    prompt_tokens: (prev?.prompt_tokens ?? 0) + next.prompt_tokens,
    completion_tokens: (prev?.completion_tokens ?? 0) + next.completion_tokens,
    total_tokens: (prev?.total_tokens ?? 0) + next.total_tokens,
  };
}

export const useChatStore = create<ChatStoreState>((set, get) => {
  function mutateCurrentGroup(
    updater: (group: LiveAssistantGroup) => LiveAssistantGroup,
    createIfMissing = true,
  ): LiveAssistantGroup | null {
    const state = get();
    const existing = state.currentGroupId
      ? state.liveEntries.find(
          (entry): entry is LiveAssistantGroup =>
            entry.kind === "assistant_group" && entry.id === state.currentGroupId,
        )
      : undefined;

    if (existing) {
      const updated = updater(existing);
      set({
        liveEntries: state.liveEntries.map((entry) =>
          entry.id === existing.id ? updated : entry,
        ),
      });
      return updated;
    }

    if (!createIfMissing) return null;

    const fresh: LiveAssistantGroup = {
      kind: "assistant_group",
      id: nextUid("grp"),
      time: Date.now() / 1000,
      reasoning: "",
      tools: [],
      bubbles: [],
      pending: false,
      closed: false,
      roundId: state.eventRound?.round_id || null,
      requestId: state.eventRound?.request_id ?? state.pendingRequest?.id ?? null,
    };
    const updated = updater(fresh);
    set({
      currentGroupId: updated.id,
      liveEntries: [...state.liveEntries, updated],
    });
    return updated;
  }

  function groupMatchesRound(
    group: LiveAssistantGroup,
    round?: { round_id?: string | null; request_id?: string | null } | null,
  ): boolean {
    if (!round) return true;
    if (group.roundId && round.round_id) return group.roundId === round.round_id;
    if (group.requestId && round.request_id) return group.requestId === round.request_id;
    return true;
  }

  function closeResponseGroup(round?: { round_id?: string | null; request_id?: string | null } | null) {
    const state = get();
    if (!state.currentGroupId) return;
    const current = state.liveEntries.find(
      (entry): entry is LiveAssistantGroup =>
        entry.kind === "assistant_group" && entry.id === state.currentGroupId,
    );
    if (!current || (round && !groupMatchesRound(current, round))) return;

    const hasContent =
      current.bubbles.length > 0 || current.reasoning.length > 0 || current.tools.length > 0;

    if (!hasContent) {
      set({
        currentGroupId: null,
        liveEntries: state.liveEntries.filter((entry) => entry.id !== current.id),
      });
      return;
    }

    const closedGroup: LiveAssistantGroup = {
      ...current,
      pending: false,
      closed: true,
      bubbles: current.bubbles.map((b) => ({ ...b, streaming: false })),
    };
    set({
      currentGroupId: null,
      liveEntries: state.liveEntries.map((entry) =>
        entry.id === closedGroup.id ? closedGroup : entry,
      ),
    });
  }

  function markRoundInterrupted(outcome: { round_id: string; request_id?: string | null; status?: string }) {
    const state = get();
    if (outcome.status !== "cancelled" || state.interruptedRounds.includes(outcome.round_id)) {
      return;
    }

    let targetId: string | null = null;
    for (let i = state.liveEntries.length - 1; i >= 0; i -= 1) {
      const entry = state.liveEntries[i];
      if (!entry || entry.kind !== "assistant_group") continue;
      if (
        (entry.roundId && entry.roundId === outcome.round_id) ||
        (!entry.roundId && entry.requestId && outcome.request_id && entry.requestId === outcome.request_id)
      ) {
        targetId = entry.id;
        break;
      }
    }

    const updatedLive = state.liveEntries.map((entry) =>
      entry.id === targetId && entry.kind === "assistant_group"
        ? { ...entry, interrupted: true }
        : entry,
    );
    const systemEntry: LiveSystemEntry = {
      kind: "system",
      id: nextUid("sys"),
      text: "本轮已中断。已执行的操作不撤销，不自动重试。",
      transient: true,
    };

    set({
      interruptedRounds: [...state.interruptedRounds, outcome.round_id],
      liveEntries: [...updatedLive, systemEntry],
    });
  }

  function releaseRequest(request: PendingRequestState) {
    if (get().pendingRequest?.id !== request.id) return;
    set({ pendingRequest: null, sending: false });
  }

  async function finishRequest(request: PendingRequestState, outcome: SyncOutcome) {
    if (request.finished || outcome.request_id !== request.id) return;
    request.finished = true;

    const currentActive = get().activeRound;
    if (currentActive?.roundId === outcome.round_id) {
      set({ activeRound: null });
    }

    try {
      markRoundInterrupted(outcome);
      closeResponseGroup(outcome);

      if (outcome.command) {
        if (outcome.command.refresh_history) {
          await get().loadHistory();
        } else {
          set((s) => ({
            liveEntries: s.liveEntries.filter(
              (entry) => !(entry.kind === "user" && entry.requestId === request.id),
            ),
          }));
        }
        set((s) => ({
          liveEntries: [
            ...s.liveEntries,
            { kind: "system", id: nextUid("sys"), text: outcome.command!.text, transient: true },
          ],
        }));
      } else if (
        outcome.status &&
        !["completed", "cancelled"].includes(outcome.status) &&
        !request.errorShown
      ) {
        request.errorShown = true;
        set((s) => ({
          lastRoundError: outcome.round_id,
          liveEntries: [
            ...s.liveEntries,
            {
              kind: "error",
              id: nextUid("err"),
              text: outcome.error || "本轮未完成",
              time: Date.now() / 1000,
            },
          ],
        }));
      }
    } catch (err) {
      if (err instanceof Error && err.message !== "unauthorized") {
        get().appendLocalError(err.message);
      }
    } finally {
      releaseRequest(request);
    }
  }

  function applyChatEvent(msg: WsEventEnvelope) {
    const type = msg.type;
    const data = (msg.data || {}) as Record<string, unknown>;
    const round = msg.round as { round_id?: string | null; request_id?: string | null } | null;

    const currentPending = get().pendingRequest;
    if (round?.request_id && currentPending?.id === round.request_id && round.round_id) {
      set({ pendingRequest: { ...currentPending, roundId: round.round_id } });
    }

    const roundBoundEvents = new Set([
      "thinking",
      "thinking_delta",
      "text",
      "text_delta",
      "message_delta",
      "message",
      "tool_call",
      "tool_result",
      "message_seg_end",
    ]);

    if (roundBoundEvents.has(type)) {
      if (round?.round_id) {
        const curActive = get().activeRound;
        if (curActive?.roundId !== round.round_id) {
          set({ activeRound: { id: round.request_id, roundId: round.round_id } });
        }
      }
      const curState = get();
      const curGroup = curState.currentGroupId
        ? curState.liveEntries.find(
            (e): e is LiveAssistantGroup =>
              e.kind === "assistant_group" && e.id === curState.currentGroupId,
          )
        : undefined;
      if (round && curGroup && !groupMatchesRound(curGroup, round)) {
        closeResponseGroup();
      }
      set({ eventRound: round || null });
      if (round) {
        mutateCurrentGroup(
          (g) => ({ ...g, roundId: round.round_id || g.roundId, requestId: round.request_id || g.requestId }),
          false,
        );
      }
    }

    switch (type as string) {
      case "hello":
      case "settings":
        get().applySettings(data as WebSettingsDto);
        break;

      case "thinking":
      case "thinking_delta": {
        const delta = String(data.content || "");
        mutateCurrentGroup((g) => ({
          ...g,
          pending: false,
          reasoning: g.reasoning + delta,
        }));
        break;
      }

      case "text":
      case "text_delta": {
        const text = String(data.content || "");
        mutateCurrentGroup((g) => ({
          ...g,
          pending: false,
          time: Date.now() / 1000,
          bubbles: [...g.bubbles, { id: nextUid("bub"), text, platform: "web", streaming: false }],
        }));
        break;
      }

      case "message_delta": {
        const delta = String(data.text || "");
        const platform = typeof data.platform === "string" ? data.platform : "web";
        mutateCurrentGroup((g) => {
          const last = g.bubbles[g.bubbles.length - 1];
          const nextBubbles =
            last && last.streaming
              ? [
                  ...g.bubbles.slice(0, -1),
                  { ...last, text: last.text + delta },
                ]
              : [
                  ...g.bubbles,
                  { id: nextUid("bub"), text: delta, platform, streaming: true },
                ];
          return { ...g, pending: false, time: Date.now() / 1000, bubbles: nextBubbles };
        });
        break;
      }

      case "message_seg_end": {
        mutateCurrentGroup(
          (g) => {
            const last = g.bubbles[g.bubbles.length - 1];
            if (!last || !last.streaming) return g;
            return {
              ...g,
              bubbles: [...g.bubbles.slice(0, -1), { ...last, streaming: false }],
            };
          },
          false,
        );
        break;
      }

      case "tool_call": {
        const name = String(data.name || "");
        const args = String(data.arguments || "");
        mutateCurrentGroup((g) => ({
          ...g,
          pending: false,
          bubbles: g.bubbles.map((b) => ({ ...b, streaming: false })),
          tools: [...g.tools, { id: nextUid("tool"), name, arguments: args, result: null }],
        }));
        break;
      }

      case "tool_result": {
        const name = String(data.name || "");
        const result = String(data.result ?? "");
        mutateCurrentGroup((g) => {
          const tools = [...g.tools];
          let matchedIdx = -1;
          for (let i = tools.length - 1; i >= 0; i -= 1) {
            const candidate = tools[i];
            if (candidate && candidate.name === name && candidate.result === null) {
              matchedIdx = i;
              break;
            }
          }
          const matchedTool = matchedIdx >= 0 ? tools[matchedIdx] : undefined;
          if (matchedTool) {
            tools[matchedIdx] = { ...matchedTool, result };
          } else {
            tools.push({ id: nextUid("tool"), name, arguments: "", result });
          }
          return { ...g, pending: false, tools };
        });
        break;
      }

      case "web_search": {
        const q = String(data.query || "");
        const status = String(data.status || "");
        const searchId = String(data.id || "");
        mutateCurrentGroup((g) => {
          const tools = [...g.tools];
          let idx = -1;
          if (searchId) {
            idx = tools.findIndex((t) => t.isWebSearch && t.searchId === searchId);
          }
          if (idx === -1 && q) {
            idx = tools.findIndex((t) => t.isWebSearch && t.arguments === q && t.result === null);
          }
          const existingTool = idx >= 0 ? tools[idx] : undefined;
          let entry: ToolCallEntry = existingTool
            ? { ...existingTool }
            : {
                id: nextUid("ws"),
                name: "联网搜索",
                arguments: q,
                result: null,
                isWebSearch: true,
                searchId,
              };
          if (q) entry.arguments = q;
          if (status === "in_progress") entry.result = "准备搜索…";
          else if (status === "searching") entry.result = "搜索中…";
          else if (status === "completed") {
            entry.result =
              entry.result === "搜索中…" || entry.result === "准备搜索…"
                ? "搜索完成"
                : entry.result || "搜索完成";
          } else if (status === "failed") {
            entry.result = "失败（页面无法访问）";
          }
          if (idx >= 0) tools[idx] = entry;
          else tools.push(entry);
          return { ...g, pending: false, tools };
        });
        break;
      }

      case "message": {
        const text = String(data.text || "");
        const platform = typeof data.platform === "string" ? data.platform : "web";
        mutateCurrentGroup((g) => ({
          ...g,
          pending: false,
          time: Date.now() / 1000,
          bubbles: [...g.bubbles, { id: nextUid("bub"), text, platform, streaming: false }],
        }));
        break;
      }

      case "final": {
        // Model finished outputting; round remains active until `round_finished`.
        const state = get();
        if (round?.request_id && state.pendingRequest?.id === round.request_id) {
          set({ pendingRequest: { ...state.pendingRequest, finishing: true } });
        }
        if (state.activeRound && round?.round_id && state.activeRound.roundId === round.round_id) {
          set({ activeRound: { ...state.activeRound, finishing: true } });
        }
        closeResponseGroup(round);
        break;
      }

      case "round_finished": {
        const outcome = data as unknown as SyncOutcome;
        const state = get();
        if (state.activeRound?.roundId === outcome.round_id) {
          set({ activeRound: null });
        }
        markRoundInterrupted(outcome);
        closeResponseGroup(outcome);
        if (state.pendingRequest && outcome.request_id === state.pendingRequest.id) {
          void finishRequest(state.pendingRequest, outcome);
        } else if (outcome.command?.refresh_history) {
          void get().loadHistory();
        } else if (
          outcome.status === "failed" &&
          state.lastRoundError !== outcome.round_id
        ) {
          set((s) => ({
            lastRoundError: outcome.round_id,
            liveEntries: [
              ...s.liveEntries,
              {
                kind: "error",
                id: nextUid("err"),
                text: outcome.error || "回合处理失败",
                time: Date.now() / 1000,
              },
            ],
          }));
        }
        break;
      }

      case "usage": {
        const nextUsage: UsageCounters = {
          prompt_tokens: Number(data.prompt_tokens ?? 0),
          completion_tokens: Number(data.completion_tokens ?? 0),
          total_tokens: Number(data.total_tokens ?? 0),
        };
        const state = get();
        const roundId = round?.round_id || state.activeRound?.roundId || null;
        const requestId = round?.request_id || state.pendingRequest?.id || null;
        const existingRoundUsage =
          (roundId && state.usageByRound[roundId]) ||
          (requestId && state.usageByRound[requestId]) ||
          undefined;
        const mergedRoundUsage = accumulateUsage(existingRoundUsage, nextUsage);
        const nextByRound = { ...state.usageByRound };
        if (roundId) nextByRound[roundId] = mergedRoundUsage;
        if (requestId) nextByRound[requestId] = mergedRoundUsage;

        let targetId: string | null = null;
        if (state.currentGroupId) {
          const cur = state.liveEntries.find(
            (e): e is LiveAssistantGroup =>
              e.kind === "assistant_group" && e.id === state.currentGroupId,
          );
          if (cur && groupMatchesRound(cur, round)) {
            targetId = cur.id;
          }
        }
        if (!targetId) {
          for (let i = state.liveEntries.length - 1; i >= 0; i -= 1) {
            const entry = state.liveEntries[i];
            if (entry?.kind === "assistant_group" && groupMatchesRound(entry, round)) {
              targetId = entry.id;
              break;
            }
          }
        }

        set({
          usage: nextUsage,
          usageByRound: nextByRound,
          liveEntries: targetId
            ? state.liveEntries.map((entry) =>
                entry.id === targetId && entry.kind === "assistant_group"
                  ? { ...entry, usage: accumulateUsage(entry.usage, nextUsage) }
                  : entry,
              )
            : state.liveEntries,
        });
        break;
      }

      case "error": {
        closeResponseGroup(round);
        const state = get();
        if (!round?.round_id || state.lastRoundError !== round.round_id) {
          get().appendLocalError(String(data.error || "未知错误"));
          set({ lastRoundError: round?.round_id || null });
        }
        if (round?.request_id && state.pendingRequest?.id === round.request_id) {
          state.pendingRequest.errorShown = true;
        }
        break;
      }

      default:
        break;
    }
  }

  return {
    settings: DEFAULT_SETTINGS,
    wsStatus: wsClient.getStatus(),
    chatSyncing: false,
    chatSyncGeneration: 0,
    chatInstance: null,
    chatSeq: 0,
    chatBuffer: [],

    historyLimit: 50,
    hasMore: false,
    loadingMore: false,
    archivedMessages: [],
    liveEntries: [],
    currentGroupId: null,

    sending: false,
    pendingRequest: null,
    activeRound: null,
    eventRound: null,
    interruptedRounds: [],
    lastRoundError: null,

    usage: null,
    usageByRound: {},
    commands: [],
    scrollRequestNonce: 0,

    applySettings: (s) => {
      set({ settings: s });
      if (typeof document !== "undefined" && s.agent_name) {
        document.title = `${s.agent_name} · Tentacle`;
      }
    },

    setWsStatus: (status) => {
      const prev = get().wsStatus;
      set({ wsStatus: status });
      if (status === "connected") {
        set({ chatSyncing: true });
      } else if (prev === "connected" && (status === "reconnecting" || status === "idle")) {
        const nextGen = get().chatSyncGeneration + 1;
        const pending = get().pendingRequest;
        if (pending) pending.needsHistory = true;
        set({ chatSyncGeneration: nextGen, chatSyncing: status === "reconnecting" });
        if (status === "reconnecting") {
          void get().synchronizeChat();
        }
      }
    },

    resetSessionState: () => {
      clearSyncRetry();
      historyRequestSeq += 1;
      set({
        chatSyncing: false,
        loadingMore: false,
        chatSyncGeneration: get().chatSyncGeneration + 1,
        chatBuffer: [],
        archivedMessages: [],
        liveEntries: [],
        currentGroupId: null,
        sending: false,
        pendingRequest: null,
        activeRound: null,
        eventRound: null,
        interruptedRounds: [],
        lastRoundError: null,
        usage: null,
        usageByRound: {},
      });
    },

    loadCommands: async () => {
      try {
        const res = await apiFetch<{ commands?: ChatCommandOption[] }>("/api/chat/commands");
        set({ commands: res.commands || [] });
      } catch {
        // Non-fatal; commands autocomplete can fall back cleanly.
      }
    },

    loadHistory: async (opts = {}) => {
      const limit = Math.min(Math.max(opts.limit || get().historyLimit, 1), 200);
      const generation = get().chatSyncGeneration;
      const historyToken = ++historyRequestSeq;
      set({ loadingMore: true });
      try {
        const data = await apiFetch<ChatHistoryPayload>(`/api/chat/history?limit=${limit}`);
        if (historyToken !== historyRequestSeq || generation !== get().chatSyncGeneration) return;
        set((s) => {
          const remainingLive = s.liveEntries.filter((entry) => isLiveEntryInFlight(entry, s));
          const keepCurrentGroup =
            s.currentGroupId && remainingLive.some((entry) => entry.id === s.currentGroupId)
              ? s.currentGroupId
              : null;
          return {
            archivedMessages: data.messages || [],
            hasMore: Boolean(data.has_more),
            historyLimit: limit,
            liveEntries: remainingLive,
            currentGroupId: keepCurrentGroup,
          };
        });
      } finally {
        if (historyToken === historyRequestSeq) {
          set({ loadingMore: false });
        }
      }
    },

    synchronizeChat: async () => {
      clearSyncRetry();
      const generation = get().chatSyncGeneration + 1;
      set({ chatSyncGeneration: generation, chatSyncing: true, chatBuffer: [] });
      try {
        const data = await apiFetch<ChatSyncPayload>(`/api/chat/sync?limit=${get().historyLimit}`);
        if (generation !== get().chatSyncGeneration) return;

        const messages = data.messages || [];
        const archivedIds = new Set(messages.map((m) => m.round_id).filter(Boolean));
        const interruptedSet = new Set(get().interruptedRounds);

        for (const m of messages) {
          if (m.interrupted && m.round_id) interruptedSet.add(m.round_id);
        }
        for (const outcome of data.finished || []) {
          if (outcome.status === "cancelled" && archivedIds.has(outcome.round_id)) {
            interruptedSet.add(outcome.round_id);
          }
        }

        set({
          archivedMessages: messages,
          hasMore: Boolean(data.has_more),
          liveEntries: [],
          currentGroupId: null,
          chatInstance: data.instance_id || null,
          chatSeq: data.seq ?? 0,
          lastRoundError: null,
          activeRound: null,
          eventRound: null,
          interruptedRounds: Array.from(interruptedSet),
        });

        for (const round of data.rounds || []) {
          if (round.input && !archivedIds.has(round.round_id)) {
            const m = round.input;
            if (m.platform === "system") {
              set((s) => ({
                liveEntries: [
                  ...s.liveEntries,
                  {
                    kind: "system",
                    id: nextUid("sys"),
                    text: m.content || "",
                    time: round.created_time,
                  },
                ],
              }));
            } else {
              set((s) => ({
                liveEntries: [
                  ...s.liveEntries,
                  {
                    kind: "user",
                    id: nextUid("usr"),
                    text: m.content || "[附件]",
                    time: round.created_time || Date.now() / 1000,
                    platform: m.platform,
                    images: m.images,
                    roundId: round.round_id,
                    requestId: round.request_id,
                  },
                ],
              }));
            }
          }
          for (const event of data.events || []) {
            if (event.round?.round_id === round.round_id && !archivedIds.has(round.round_id)) {
              applyChatEvent(event);
            }
          }
        }

        const active =
          (data.rounds || []).find((r) => r.phase !== "queued") || data.rounds?.[0] || null;
        if (active) {
          set({
            activeRound: {
              id: active.request_id,
              roundId: active.round_id,
              stopping: active.stopping,
              finishing: active.phase === "finishing",
            },
          });
          if (
            !get().currentGroupId &&
            !archivedIds.has(active.round_id) &&
            active.phase !== "finishing"
          ) {
            set({ eventRound: { round_id: active.round_id, request_id: active.request_id } });
            mutateCurrentGroup((g) => ({ ...g, pending: true }));
          }
        }

        const request = get().pendingRequest;
        if (request) {
          const matches = (data.rounds || []).filter((r) =>
            request.roundId ? r.round_id === request.roundId : r.request_id === request.id,
          );
          const firstMatch = matches[0];
          if (matches.length === 1 && firstMatch) {
            set({
              pendingRequest: {
                ...request,
                roundId: firstMatch.round_id,
                stopping: Boolean(firstMatch.stopping),
                finishing: firstMatch.phase === "finishing",
              },
            });
          } else {
            const terminal = (data.finished || []).find((r) =>
              request.roundId ? r.round_id === request.roundId : r.request_id === request.id,
            );
            if (terminal) {
              await finishRequest(request, terminal);
            } else if (request.uncertain) {
              get().appendLocalError("上一请求的结果无法确认，请核对历史；不会自动重发。");
              releaseRequest(request);
            }
          }
        }

        if (generation !== get().chatSyncGeneration) return;
        const buffered = get().chatBuffer;
        set({ chatSyncing: false, chatBuffer: [] });
        for (const bufferedEvent of buffered) {
          get().receiveWsEvent(bufferedEvent);
        }
      } catch (err) {
        if (generation !== get().chatSyncGeneration) return;
        set({ chatSyncing: false });
        if (!(err instanceof Error && err.message === "unauthorized")) {
          syncRetryTimer = window.setTimeout(() => {
            if (generation === get().chatSyncGeneration) {
              void get().synchronizeChat();
            }
          }, 3000);
        }
      }
    },

    receiveWsEvent: (msg) => {
      if (msg.type === "hello") {
        applyChatEvent(msg);
        void get().synchronizeChat();
        return;
      }
      if (msg.chat_seq != null) {
        const state = get();
        if (state.chatSyncing) {
          set({ chatBuffer: [...state.chatBuffer, msg] });
          return;
        }
        if (msg.chat_instance !== state.chatInstance) {
          set({ chatBuffer: [...state.chatBuffer, msg] });
          void get().synchronizeChat();
          return;
        }
        if (msg.chat_seq <= state.chatSeq) {
          return;
        }
        set({ chatSeq: msg.chat_seq });
      }
      applyChatEvent(msg);
    },

    sendMessage: async (rawText, uploadedFiles) => {
      const state = get();
      const text = rawText.trim();
      if ((!text && !uploadedFiles.length) || state.sending || state.activeRound || state.chatSyncing) {
        return;
      }

      const requestId =
        typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

      const request: PendingRequestState = {
        id: requestId,
        roundId: null,
        finished: false,
        errorShown: false,
        needsHistory: state.wsStatus !== "connected",
        stopping: false,
        finishing: false,
        stopError: false,
        uncertain: false,
      };

      const userEntry: LiveUserEntry = {
        kind: "user",
        id: nextUid("usr"),
        text: text || "[附件]",
        time: Date.now() / 1000,
        images: uploadedFiles.map((f) => ({
          local_id: f.local_id,
          filename: f.filename,
          path: f.path,
          mime: f.mime,
        })),
        requestId,
      };

      closeResponseGroup();
      set((s) => ({
        sending: true,
        pendingRequest: request,
        eventRound: null,
        liveEntries: [...s.liveEntries, userEntry],
        scrollRequestNonce: s.scrollRequestNonce + 1,
      }));
      mutateCurrentGroup((g) => ({ ...g, pending: true }));

      try {
        const body: Record<string, unknown> = { text, request_id: requestId };
        if (uploadedFiles.length) {
          body.files = uploadedFiles.map((f) => f.local_id);
        }
        const res = await apiFetch<ChatSendResponse>("/api/chat/send", {
          method: "POST",
          body: JSON.stringify(body),
        });
        const roundOutcome = (res.round || {}) as unknown as SyncOutcome;
        if (!res.round || roundOutcome.request_id !== request.id) {
          throw new Error("服务端未返回对应回合的处理结果");
        }
        if (res.command && !roundOutcome.command) {
          roundOutcome.command = res.command;
        }
        await finishRequest(request, roundOutcome);
        const totalTokens = Number(res.total_tokens ?? 0);
        if (totalTokens > 0) {
          const state = get();
          const hasRecorded =
            Boolean(roundOutcome.round_id && state.usageByRound[roundOutcome.round_id]) ||
            Boolean(state.usageByRound[request.id]);
          if (!hasRecorded) {
            const fallbackUsage: UsageCounters = {
              prompt_tokens: 0,
              completion_tokens: 0,
              total_tokens: totalTokens,
            };
            const nextByRound = { ...state.usageByRound, [request.id]: fallbackUsage };
            if (roundOutcome.round_id) nextByRound[roundOutcome.round_id] = fallbackUsage;
            set({
              usageByRound: nextByRound,
              liveEntries: state.liveEntries.map((entry) =>
                entry.kind === "assistant_group" &&
                !entry.usage &&
                ((roundOutcome.round_id && entry.roundId === roundOutcome.round_id) ||
                  entry.requestId === request.id)
                  ? { ...entry, usage: fallbackUsage }
                  : entry,
              ),
            });
          }
        }
        if (request.needsHistory && !roundOutcome.command) {
          void get().synchronizeChat();
        }
        if (
          !get().sending &&
          !res.command &&
          /https?:\/\/(?:www\.|m\.)?(?:bilibili\.com|b23\.tv)\//i.test(text)
        ) {
          await get().loadHistory();
        }
      } catch (err) {
        const apiErr = err as ApiError & { round?: SyncOutcome };
        if (apiErr.round?.request_id === request.id) {
          await finishRequest(request, apiErr.round);
          if (request.needsHistory && !apiErr.round.command) {
            void get().synchronizeChat();
          }
        } else if (!request.finished && get().pendingRequest?.id === request.id) {
          closeResponseGroup({ request_id: request.id });
          if (apiErr.message !== "unauthorized" && !request.errorShown) {
            get().appendLocalError(apiErr.message || "发送失败");
          }
          if (apiErr.status) {
            releaseRequest(request);
          } else {
            request.uncertain = true;
            set({ pendingRequest: { ...request } });
            void get().synchronizeChat();
          }
        }
      } finally {
        if (!request.finished && !request.uncertain) {
          releaseRequest(request);
        }
      }
    },

    stopRound: async () => {
      const target = getChatTarget(get());
      if (!target || ("finished" in target && target.finished) || target.stopping || target.finishing) {
        return;
      }

      if (get().pendingRequest) {
        set((s) =>
          s.pendingRequest ? { pendingRequest: { ...s.pendingRequest, stopping: true, stopError: false } } : s,
        );
      } else if (get().activeRound) {
        set((s) =>
          s.activeRound ? { activeRound: { ...s.activeRound, stopping: true, stopError: false } } : s,
        );
      }

      try {
        const payload: Record<string, string> = {};
        if (target.id) payload.request_id = target.id;
        if (target.roundId) payload.round_id = target.roundId;

        const res = await apiFetch<{ accepted: boolean; state?: string }>("/api/chat/stop", {
          method: "POST",
          body: JSON.stringify(payload),
        });

        const current = getChatTarget(get());
        if (!current) return;
        const stillMatches =
          current.id === target.id || (target.roundId && current.roundId === target.roundId);
        if (!stillMatches) return;

        if (res.accepted) {
          if (get().pendingRequest) {
            set((s) => (s.pendingRequest ? { pendingRequest: { ...s.pendingRequest, stopping: true } } : s));
          } else if (get().activeRound) {
            set((s) => (s.activeRound ? { activeRound: { ...s.activeRound, stopping: true } } : s));
          }
        } else {
          const isFinishing = res.state === "finishing";
          if (get().pendingRequest) {
            set((s) =>
              s.pendingRequest
                ? {
                    pendingRequest: {
                      ...s.pendingRequest,
                      stopping: false,
                      finishing: isFinishing,
                      stopError: !isFinishing,
                    },
                  }
                : s,
            );
          } else if (get().activeRound) {
            set((s) =>
              s.activeRound
                ? {
                    activeRound: {
                      ...s.activeRound,
                      stopping: false,
                      finishing: isFinishing,
                      stopError: !isFinishing,
                    },
                  }
                : s,
            );
          }
          if (res.state === "not_found") {
            void get().synchronizeChat();
          }
        }
      } catch (err) {
        if (get().pendingRequest) {
          set((s) =>
            s.pendingRequest
              ? { pendingRequest: { ...s.pendingRequest, stopping: false, stopError: true } }
              : s,
          );
        } else if (get().activeRound) {
          set((s) =>
            s.activeRound
              ? { activeRound: { ...s.activeRound, stopping: false, stopError: true } }
              : s,
          );
        }
        if (err instanceof Error && err.message !== "unauthorized") {
          get().appendLocalError(err.message);
        }
      }
    },

    appendLocalError: (text) => {
      set((s) => ({
        liveEntries: [
          ...s.liveEntries,
          { kind: "error", id: nextUid("err"), text, time: Date.now() / 1000 },
        ],
      }));
    },

    requestScrollToBottom: () => {
      set((s) => ({ scrollRequestNonce: s.scrollRequestNonce + 1 }));
    },
  };
});

// Wire WS connection status + incoming events once on load.
wsClient.onStatus((status) => {
  useChatStore.getState().setWsStatus(status);
});
wsClient.onAny((event) => {
  useChatStore.getState().receiveWsEvent(event);
});
