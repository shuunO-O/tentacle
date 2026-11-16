import { beforeEach, describe, expect, it, vi } from "vitest";
import { wsClient } from "../../api/ws";
import { getChatTarget, useChatStore } from "./chatStore";

describe("chatStore state machine", () => {
  beforeEach(() => {
    useChatStore.getState().resetSessionState();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("keeps round active on `final` until host `round_finished` arrives", () => {
    const store = useChatStore.getState();

    store.receiveWsEvent({
      type: "message_delta",
      data: { text: "你好", platform: "web" },
      round: { round_id: "r1", request_id: "req-1" },
      chat_seq: null,
      chat_instance: null,
      ts: null,
    });

    expect(getChatTarget(useChatStore.getState())?.roundId).toBe("r1");

    store.receiveWsEvent({
      type: "final",
      data: { content: "你好" },
      round: { round_id: "r1", request_id: "req-1" },
      chat_seq: null,
      chat_instance: null,
      ts: null,
    });

    // `final` only marks finishing; activeRound persists until `round_finished`.
    const afterFinal = useChatStore.getState();
    expect(afterFinal.activeRound?.roundId).toBe("r1");
    expect(afterFinal.activeRound?.finishing).toBe(true);

    store.receiveWsEvent({
      type: "round_finished",
      data: { round_id: "r1", request_id: "req-1", status: "cancelled" },
      round: { round_id: "r1", request_id: "req-1" },
      chat_seq: null,
      chat_instance: null,
      ts: null,
    });

    const afterFinish = useChatStore.getState();
    expect(afterFinish.activeRound).toBeNull();
    expect(afterFinish.interruptedRounds).toContain("r1");
    expect(
      afterFinish.liveEntries.some(
        (e) => e.kind === "system" && e.text.includes("本轮已中断"),
      ),
    ).toBe(true);
  });

  it("marks pendingRequest uncertain on transport drop without auto-retrying", async () => {
    vi.spyOn(wsClient, "getStatus").mockReturnValue("connected");
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/chat/send") {
        throw new TypeError("network reset");
      }
      if (url.startsWith("/api/chat/sync")) {
        return new Response(
          JSON.stringify({
            messages: [],
            has_more: false,
            instance_id: "inst-1",
            seq: 10,
            rounds: [],
            events: [],
            finished: [],
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("{}", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await useChatStore.getState().sendMessage("hello world", []);

    // Only one /api/chat/send call must ever happen (no automatic resend).
    const sendCalls = fetchMock.mock.calls.filter((c) => String(c[0]) === "/api/chat/send");
    expect(sendCalls).toHaveLength(1);
    await vi.waitFor(() => {
      const hasUncertainNotice = useChatStore
        .getState()
        .liveEntries.some((e) => e.kind === "error" && e.text.includes("不会自动重发"));
      expect(hasUncertainNotice).toBe(true);
    });
  });

  it("attaches accumulated per-round token usage to the matching assistant response", () => {
    const store = useChatStore.getState();

    store.receiveWsEvent({
      type: "message_delta",
      data: { text: "测试回答", platform: "web" },
      round: { round_id: "r-tok", request_id: "req-tok" },
      chat_seq: null,
      chat_instance: null,
      ts: null,
    });

    store.receiveWsEvent({
      type: "usage",
      data: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
      round: { round_id: "r-tok", request_id: "req-tok" },
      chat_seq: null,
      chat_instance: null,
      ts: null,
    });

    store.receiveWsEvent({
      type: "usage",
      data: { prompt_tokens: 50, completion_tokens: 10, total_tokens: 60 },
      round: { round_id: "r-tok", request_id: "req-tok" },
      chat_seq: null,
      chat_instance: null,
      ts: null,
    });

    const state = useChatStore.getState();
    const grp = state.liveEntries.find((e) => e.kind === "assistant_group");
    expect(grp?.kind).toBe("assistant_group");
    if (grp?.kind === "assistant_group") {
      expect(grp.usage).toEqual({
        prompt_tokens: 150,
        completion_tokens: 30,
        total_tokens: 180,
      });
    }
    expect(state.usageByRound["r-tok"]?.total_tokens).toBe(180);
  });

  it("clears rolled-back live entries, avoids duplicates, and removes /rollback user bubble", async () => {
    vi.spyOn(wsClient, "getStatus").mockReturnValue("connected");
    useChatStore.setState({ wsStatus: "connected" });

    let roundCounter = 0;
    let dbHistory: Array<{
      id: number;
      role: "user" | "assistant";
      content: string;
      created_time: number;
      round_id: string;
    }> = [];

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/chat/send") {
        const body = JSON.parse(String(init?.body || "{}")) as {
          text: string;
          request_id: string;
        };
        if (body.text === "/rollback") {
          // Backend removes the last round from DB history.
          dbHistory = dbHistory.slice(0, -2);
          const command = {
            text: "[命令] 已回退上一轮对话；工具操作、已发送消息和 Token 用量未撤销",
            refresh_history: true,
          };
          const round = {
            round_id: "r-cmd",
            request_id: body.request_id,
            source: "user",
            platform: "web",
            status: "completed",
            stop_reason: "command",
            error: null,
            command,
          };
          return new Response(JSON.stringify({ ok: true, total_tokens: 0, round, command }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }

        roundCounter += 1;
        const roundId = `r-${roundCounter}`;
        const replyText = `回复-${roundCounter}`;
        dbHistory.push(
          { id: roundCounter * 2 - 1, role: "user", content: body.text, created_time: 100, round_id: roundId },
          { id: roundCounter * 2, role: "assistant", content: replyText, created_time: 101, round_id: roundId },
        );
        useChatStore.getState().receiveWsEvent({
          type: "message_delta",
          data: { text: replyText, platform: "web" },
          round: { round_id: roundId, request_id: body.request_id },
          chat_seq: null,
          chat_instance: null,
          ts: null,
        });
        useChatStore.getState().receiveWsEvent({
          type: "final",
          data: { content: replyText },
          round: { round_id: roundId, request_id: body.request_id },
          chat_seq: null,
          chat_instance: null,
          ts: null,
        });
        const round = {
          round_id: roundId,
          request_id: body.request_id,
          source: "user",
          platform: "web",
          status: "completed",
          stop_reason: "complete",
          error: null,
        };
        useChatStore.getState().receiveWsEvent({
          type: "round_finished",
          data: round,
          round,
          chat_seq: null,
          chat_instance: null,
          ts: null,
        });
        return new Response(JSON.stringify({ ok: true, total_tokens: 10, round }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }

      if (url.startsWith("/api/chat/history")) {
        return new Response(JSON.stringify({ messages: dbHistory, has_more: false }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }

      return new Response("{}", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await useChatStore.getState().sendMessage("第一轮问题", []);
    await useChatStore.getState().sendMessage("第二轮要回退的问题", []);

    expect(useChatStore.getState().liveEntries).toHaveLength(4);

    await useChatStore.getState().sendMessage("/rollback", []);

    const finalState = useChatStore.getState();
    expect(finalState.archivedMessages.map((m) => m.content)).toEqual(["第一轮问题", "回复-1"]);
    expect(finalState.liveEntries).toHaveLength(1);
    expect(finalState.liveEntries[0]).toMatchObject({
      kind: "system",
      text: "[命令] 已回退上一轮对话；工具操作、已发送消息和 Token 用量未撤销",
      transient: true,
    });
  });

  it("keeps command feedback visible when /rollback runs without an active WebSocket", async () => {
    vi.spyOn(wsClient, "getStatus").mockReturnValue("idle");
    useChatStore.setState({
      wsStatus: "idle",
      archivedMessages: [
        { id: 1, role: "user", content: "待回退", created_time: 100, round_id: "r1" },
        { id: 2, role: "assistant", content: "待回退回复", created_time: 101, round_id: "r1" },
      ],
    });

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/chat/send") {
        const body = JSON.parse(String(init?.body || "{}")) as { request_id: string };
        const command = {
          text: "[命令] 已回退上一轮对话；工具操作、已发送消息和 Token 用量未撤销",
          refresh_history: true,
        };
        return new Response(
          JSON.stringify({
            ok: true,
            total_tokens: 0,
            command,
            round: {
              round_id: "r-cmd",
              request_id: body.request_id,
              status: "completed",
              stop_reason: "command",
              command,
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.startsWith("/api/chat/history")) {
        return new Response(JSON.stringify({ messages: [], has_more: false }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.startsWith("/api/chat/sync")) {
        throw new Error("should not call /api/chat/sync after command refresh_history");
      }
      return new Response("{}", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    await useChatStore.getState().sendMessage("/rollback", []);

    const state = useChatStore.getState();
    expect(state.archivedMessages).toEqual([]);
    expect(state.liveEntries).toHaveLength(1);
    expect(state.liveEntries[0]).toMatchObject({
      kind: "system",
      text: "[命令] 已回退上一轮对话；工具操作、已发送消息和 Token 用量未撤销",
    });
    expect(fetchMock.mock.calls.some((c) => String(c[0]).startsWith("/api/chat/sync"))).toBe(false);
  });

  it("refreshes history on remote /rollback while preserving an unrelated in-flight round", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/chat/history")) {
        return new Response(
          JSON.stringify({
            messages: [
              { id: 1, role: "user", content: "保留的消息", created_time: 100, round_id: "r-keep" },
            ],
            has_more: false,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("{}", { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);

    useChatStore.setState({
      liveEntries: [
        {
          kind: "user",
          id: "usr-old",
          text: "远端已回退的消息",
          time: 101,
          roundId: "r-rolled",
          requestId: "req-old",
        },
        {
          kind: "user",
          id: "usr-live",
          text: "当前进行中",
          time: 102,
          requestId: "req-live",
        },
        {
          kind: "assistant_group",
          id: "grp-live",
          time: 102,
          reasoning: "",
          tools: [],
          bubbles: [{ id: "b1", text: "流式中", platform: "web", streaming: true }],
          pending: false,
          closed: false,
          roundId: "r-live",
          requestId: "req-live",
        },
      ],
      currentGroupId: "grp-live",
      activeRound: { id: "req-live", roundId: "r-live" },
      pendingRequest: {
        id: "req-live",
        roundId: "r-live",
        finished: false,
        errorShown: false,
        needsHistory: false,
        stopping: false,
        finishing: false,
        stopError: false,
        uncertain: false,
      },
    });

    useChatStore.getState().receiveWsEvent({
      type: "round_finished",
      data: {
        round_id: "r-other-cmd",
        request_id: "req-other-client",
        status: "completed",
        stop_reason: "command",
        command: { text: "[命令] 已回退上一轮对话", refresh_history: true },
      },
      round: { round_id: "r-other-cmd", request_id: "req-other-client" },
      chat_seq: null,
      chat_instance: null,
      ts: null,
    });

    await vi.waitFor(() => {
      expect(useChatStore.getState().archivedMessages).toHaveLength(1);
    });

    const state = useChatStore.getState();
    expect(state.archivedMessages[0]?.content).toBe("保留的消息");
    expect(state.liveEntries.map((e) => e.id)).toEqual(["usr-live", "grp-live"]);
    expect(state.currentGroupId).toBe("grp-live");
  });
});
