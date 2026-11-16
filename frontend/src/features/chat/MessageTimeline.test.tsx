import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ChatPanel } from "./ChatPanel";
import { useChatStore } from "./chatStore";
import { MessageTimeline } from "./MessageTimeline";

describe("MessageTimeline", () => {
  beforeEach(() => {
    useChatStore.getState().resetSessionState();
  });

  it("renders archived messages, filters internal subagent_* tools, and displays live streaming bubbles", () => {
    useChatStore.setState({
      archivedMessages: [
        {
          id: 1,
          role: "user",
          content: "帮我检查 [file:a/b.txt]",
          created_time: 1700000000,
          images: [{ local_id: "f1", path: "a/b.txt", filename: "b.txt", mime: "text/plain" }],
        },
        {
          id: 2,
          role: "assistant",
          content: "已收到",
          segments: ["第一段回复", "第二段回复"],
          created_time: 1700000010,
          platform: "qq",
        },
      ],
      liveEntries: [
        {
          kind: "assistant_group",
          id: "live-g1",
          time: 1700000020,
          reasoning: "正在推理思路",
          tools: [
            { id: "t1", name: "subagent_spawn", arguments: "{}", result: '{"ok":true}' },
            { id: "t2", name: "read_file", arguments: '{"path":"x"}', result: "contents" },
          ],
          bubbles: [{ id: "b1", text: "流式输出内容", platform: "web", streaming: true }],
          pending: false,
          closed: false,
          roundId: "r2",
          requestId: "req2",
          usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
        },
      ],
      currentGroupId: "live-g1",
    });

    render(<MessageTimeline onFillPrompt={vi.fn()} onOpenImage={vi.fn()} />);

    expect(screen.getByText("帮我检查")).toBeInTheDocument();
    expect(screen.getByText("第一段回复")).toBeInTheDocument();
    expect(screen.getByText("第二段回复")).toBeInTheDocument();
    expect(screen.getAllByText("qq").length).toBeGreaterThan(0);
    expect(screen.getByText("正在推理思路")).toBeInTheDocument();
    expect(screen.getByText("工具调用（1）")).toBeInTheDocument();
    expect(screen.getByText("流式输出内容")).toBeInTheDocument();
    expect(screen.getByTestId("msg-token-count")).toHaveTextContent("120 tok");
  });

  it("renders ChatPanel WS status as a clean dot without text badge and omits token pill in composer", () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ commands: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    useChatStore.setState({
      wsStatus: "connected",
      usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
    });

    render(<ChatPanel />);

    const dot = screen.getByTestId("ws-status");
    expect(dot).toHaveAttribute("data-ws-status", "connected");
    expect(dot).toHaveAttribute("aria-label", "实时已连接");
    expect(dot.textContent).toBe("");
    // Token count must not live inside the floating composer pill anymore.
    const pill = screen.getByTestId("chat-composer-pill");
    expect(pill).not.toHaveTextContent("120 tok");
  });

  it("supports Escape on command palette and removes rolled-back messages from rendered timeline", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/chat/commands") {
        return new Response(
          JSON.stringify({
            commands: [{ value: "/rollback", description: "回退上一轮对话" }],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
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
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (url.startsWith("/api/chat/history")) {
        return new Response(
          JSON.stringify({
            messages: [
              { id: 1, role: "user", content: "保留的问题", created_time: 100, round_id: "r1" },
              { id: 2, role: "assistant", content: "保留的回答", created_time: 101, round_id: "r1" },
            ],
            has_more: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { status: 404 });
    });

    useChatStore.setState({
      wsStatus: "connected",
      liveEntries: [
        { kind: "user", id: "u1", text: "保留的问题", time: 100, roundId: "r1", requestId: "req1" },
        {
          kind: "assistant_group",
          id: "g1",
          time: 101,
          reasoning: "",
          tools: [],
          bubbles: [{ id: "b1", text: "保留的回答", platform: "web", streaming: false }],
          pending: false,
          closed: true,
          roundId: "r1",
          requestId: "req1",
        },
        { kind: "user", id: "u2", text: "要被回退的问题", time: 102, roundId: "r2", requestId: "req2" },
        {
          kind: "assistant_group",
          id: "g2",
          time: 103,
          reasoning: "",
          tools: [],
          bubbles: [{ id: "b2", text: "要被回退的回答", platform: "web", streaming: false }],
          pending: false,
          closed: true,
          roundId: "r2",
          requestId: "req2",
        },
      ],
    });

    render(<ChatPanel />);

    const input = screen.getByTestId("chat-input");
    await user.type(input, "/rollback");
    expect(await screen.findByTestId("command-palette")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("command-palette")).not.toBeInTheDocument();

    await user.keyboard("{Enter}");

    await waitFor(() => {
      expect(
        screen.getByText("[命令] 已回退上一轮对话；工具操作、已发送消息和 Token 用量未撤销"),
      ).toBeInTheDocument();
    });
    expect(screen.getAllByText("保留的问题")).toHaveLength(1);
    expect(screen.getAllByText("保留的回答")).toHaveLength(1);
    expect(screen.queryByText("要被回退的问题")).not.toBeInTheDocument();
    expect(screen.queryByText("要被回退的回答")).not.toBeInTheDocument();
    expect(screen.queryByText("/rollback")).not.toBeInTheDocument();
  });
});
