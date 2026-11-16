import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfigurationPanel } from "./config/ConfigurationPanel";
import { DedupPanel } from "./dedup/DedupPanel";
import { SubagentsPanel } from "./subagents/SubagentsPanel";

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("domain panels", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("renders SubagentsPanel and loads task details", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.startsWith("/api/subagents?")) {
        return Promise.resolve(
          jsonResponse({
            enabled: true,
            coding_enabled: true,
            execution_blocked: false,
            tasks: [
              {
                id: "0123456789abcdef0123456789abcdef",
                objective: "审计构建脚本",
                status: "running",
                delivery: "background",
                updated_at: 1_700_000_000,
              },
            ],
          }),
        );
      }
      if (url.includes("/api/subagents/0123456789abcdef0123456789abcdef")) {
        return Promise.resolve(
          jsonResponse({
            enabled: true,
            task: {
              id: "0123456789abcdef0123456789abcdef",
              objective: "审计构建脚本",
              status: "running",
              dsh_session_id: "dsh-session-99",
            },
            turns: [
              {
                id: 1,
                status: "running",
                delivery: "background",
                message: "检查 release 打包列表",
              },
            ],
            has_more: false,
          }),
        );
      }
      return Promise.resolve(jsonResponse({}));
    });

    render(<SubagentsPanel />);
    await waitFor(() => {
      expect(screen.getAllByText("审计构建脚本").length).toBeGreaterThanOrEqual(1);
    });
    expect(screen.getByText("打开编码工作区")).toBeInTheDocument();
    expect(await screen.findByText("检查 release 打包列表")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /打开 DSH/ })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalled();
  });

  it("renders DedupPanel pairs and triggers merge decision", async () => {
    let decided = false;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.includes("/decide") && init?.method === "POST") {
        decided = true;
        return Promise.resolve(jsonResponse({ state: "merged" }));
      }
      if (url.startsWith("/api/memory/dedup?")) {
        return Promise.resolve(
          jsonResponse({
            run: { status: "completed", checked: 10, visited: 5 },
            counts: { pending: decided ? 0 : 1, merged: decided ? 1 : 0 },
            pairs: decided
              ? []
              : [
                  {
                    id: "pair-1",
                    left_id: "Concept:rust",
                    right_id: "Concept:rust-lang",
                    state: "pending",
                    data: {
                      reason: "same",
                      score: { probabilities: { "0": 0.05, "2": 0.92 } },
                      preview: {
                        left: { id: "Concept:rust", name: "Rust" },
                        right: { id: "Concept:rust-lang", name: "Rust Lang" },
                        conflicts: [],
                      },
                    },
                  },
                ],
          }),
        );
      }
      return Promise.resolve(jsonResponse({}));
    });

    render(<DedupPanel />);
    await waitFor(() => {
      expect(screen.getByText("Rust Lang")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /保留 A 并合并/ }));
    await waitFor(() => {
      expect(decided).toBe(true);
    });
  });

  it("renders ConfigurationPanel fields and submits write-only .env modal", async () => {
    let envSavedBody: string | null = null;
    vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url === "/api/configuration/environment" && init?.method === "POST") {
        envSavedBody = String(init.body || "");
        return Promise.resolve(
          jsonResponse({
            status: {
              revision: "b".repeat(64),
              active_role: "chat",
              fields: [],
            },
          }),
        );
      }
      if (url === "/api/configuration") {
        return Promise.resolve(
          jsonResponse({
            revision: "a".repeat(64),
            active_role: "chat",
            active_model: { name: "deepseek-chat" },
            restart: { available: true, state: "idle" },
            entities: {
              providers: [{ id: "ds", label: "DeepSeek", type: "deepseek", references: [] }],
              models: [],
              roles: [{ id: "chat", label: "主对话", model: "ds/deepseek-chat", references: [] }],
            },
            provider_types: {
              deepseek: {
                label: "DeepSeek",
                auth: "api_key",
                base_url: "https://api.deepseek.com",
                api_style: "chat",
                protocols: ["chat"],
              },
            },
            fields: [
              {
                path: "roles.chat.model",
                label: "主对话模型",
                kind: "string",
                default: "ds/deepseek-chat",
                value: "ds/deepseek-chat",
              },
              {
                path: "providers.ds.api_key_env",
                label: "密钥环境变量",
                kind: "env",
                default: "DEEPSEEK_API_KEY",
                value: "DEEPSEEK_API_KEY",
                configured: false,
              },
            ],
            connection_status: { ds: { auth: "api_key", configured: false } },
            history: [],
          }),
        );
      }
      return Promise.resolve(jsonResponse({}));
    });

    render(<ConfigurationPanel />);
    await waitFor(() => {
      expect(screen.getByText("主对话模型")).toBeInTheDocument();
    });
    // Switch to provider node to reveal api_key_env and test .env write-only flow
    fireEvent.click(screen.getByRole("button", { name: "DeepSeek" }));
    await waitFor(() => {
      expect(screen.getByText("写入 .env 值")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "写入 .env 值" }));
    const secretInput = screen.getByPlaceholderText(/粘贴 API Key/);
    fireEvent.change(secretInput, { target: { value: "sk-test-secret" } });
    fireEvent.click(screen.getByRole("button", { name: /安全写入 \.env/ }));
    await waitFor(() => {
      expect(envSavedBody).toContain("sk-test-secret");
    });
  });

  it("renders SubagentsPanel as single-pane drill-down on compact mobile screens", async () => {
    const origWidth = window.innerWidth;
    window.innerWidth = 390;
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: query.includes("860px"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    try {
      vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
        const url = String(input);
        if (url.startsWith("/api/subagents?")) {
          return Promise.resolve(
            jsonResponse({
              enabled: true,
              coding_enabled: true,
              tasks: [
                {
                  id: "task-mobile-1111111111111111",
                  objective: "移动端验证任务",
                  status: "running",
                  delivery: "background",
                },
              ],
            }),
          );
        }
        if (url.includes("/api/subagents/task-mobile-")) {
          return Promise.resolve(
            jsonResponse({
              enabled: true,
              task: { id: "task-mobile-1111111111111111", objective: "移动端验证任务", status: "running" },
              turns: [{ id: 1, status: "running", message: "唯一详情回合正文" }],
            }),
          );
        }
        return Promise.resolve(jsonResponse({}));
      });

      render(<SubagentsPanel />);
      await waitFor(() => {
        expect(screen.getByText("移动端验证任务")).toBeInTheDocument();
      });
      // Detail pane must NOT be rendered in top-bottom split mode on mobile initially
      expect(screen.queryByText("唯一详情回合正文")).not.toBeInTheDocument();

      fireEvent.click(screen.getByText("移动端验证任务"));
      expect(await screen.findByText("唯一详情回合正文")).toBeInTheDocument();
      const backBtn = screen.getByTestId("subagent-mobile-back");
      fireEvent.click(backBtn);
      await waitFor(() => {
        expect(screen.queryByTestId("subagent-mobile-back")).not.toBeInTheDocument();
      });
      expect(screen.getByText("移动端验证任务")).toBeInTheDocument();
    } finally {
      window.innerWidth = origWidth;
      vi.unstubAllGlobals();
    }
  });

  it("renders ConfigurationPanel hierarchy as single-pane drill-down on compact mobile screens", async () => {
    const origWidth = window.innerWidth;
    window.innerWidth = 390;
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: query.includes("860px"),
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    try {
      vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
        if (String(input) === "/api/configuration") {
          return Promise.resolve(
            jsonResponse({
              revision: "e".repeat(64),
              active_role: "chat",
              active_model: { name: "ds-chat" },
              restart: { available: true, state: "idle" },
              entities: {
                providers: [{ id: "ds", label: "DeepSeek", type: "deepseek", references: [] }],
                models: [{ id: "ds/chat", label: "ds-chat", provider: "ds", name: "ds-chat", references: [] }],
                roles: [{ id: "chat", label: "主对话", model: "ds/chat", references: [] }],
              },
              provider_types: { deepseek: { label: "DeepSeek", auth: "api_key", api_style: "chat", protocols: ["chat"] } },
              fields: [
                { path: "providers.ds.api_key_env", label: "密钥环境变量", kind: "env", default: "DS_KEY", value: "DS_KEY" },
                { path: "roles.chat.model", label: "主对话模型", kind: "string", default: "ds/chat", value: "ds/chat" },
              ],
              connection_status: { ds: { auth: "api_key", configured: true } },
              history: [],
            }),
          );
        }
        return Promise.resolve(jsonResponse({}));
      });

      render(<ConfigurationPanel />);
      await waitFor(() => {
        expect(screen.getByRole("button", { name: "DeepSeek" })).toBeInTheDocument();
      });
      // Form editor must NOT be rendered beneath tree in a top-bottom split on mobile initially
      expect(screen.queryByTestId("config-hierarchy-chain")).not.toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "DeepSeek" }));
      expect(await screen.findByText("密钥环境变量")).toBeInTheDocument();
      const backBtn = screen.getByTestId("config-mobile-back");
      fireEvent.click(backBtn);
      await waitFor(() => {
        expect(screen.queryByTestId("config-mobile-back")).not.toBeInTheDocument();
      });
      expect(screen.getByRole("button", { name: "DeepSeek" })).toBeInTheDocument();
    } finally {
      window.innerWidth = origWidth;
      vi.unstubAllGlobals();
    }
  });
});
