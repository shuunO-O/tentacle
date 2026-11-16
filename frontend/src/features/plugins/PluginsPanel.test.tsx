import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PluginsPanel } from "./PluginsPanel";

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const REV_1 = "a".repeat(64);
const REV_2 = "b".repeat(64);

describe("PluginsPanel", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("audits pending local plugin, saves .env secret, approves enable, and requests restart", async () => {
    let approved = false;
    let envSet = false;
    let restarted = false;

    const makeSnapshot = () => ({
      revision: approved ? REV_2 : REV_1,
      plugins: [
        {
          id: "weather",
          kind: "local",
          enabled: false,
          trusted: false,
          saved_enabled: approved,
          saved_trusted: approved,
          status: approved ? "pending" : "pending",
          error: null,
          manifest: {
            id: "weather",
            name: "天气扩展",
            version: "1.0.0",
            description: "提供城市天气预报与每日心跳简报",
            permissions: ["tools:register", "hooks:mutate"],
            requires_env: ["WEATHER_API_KEY"],
            config_schema: {
              days: {
                key: "days",
                label: "预报天数",
                type: "integer",
                default: 3,
                minimum: 1,
                maximum: 7,
              },
            },
          },
          permission_details: [
            { permission: "tools:register", level: "low", summary: "注册工具到模型工具表" },
            { permission: "hooks:mutate", level: "high", summary: "在轮次前、工具调用前或心跳前注入上下文/修改调用" },
          ],
          config: { days: 3 },
          saved_config: { days: 3 },
          disabled_tools: [],
          tools: [],
          commands: [],
          hooks: [],
          skills: ["forecast"],
          missing_env: envSet ? [] : ["WEATHER_API_KEY"],
          env_vars: ["WEATHER_API_KEY"],
          env_status: { WEATHER_API_KEY: envSet },
          pending_restart: approved || envSet,
        },
      ],
      mcp_servers: [],
      permission_catalog: {},
      restart_required: approved || envSet,
      restart: { available: true, state: restarted ? "pending" : "idle" },
    });

    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/plugins") {
        return jsonResponse(makeSnapshot());
      }
      if (url === "/api/plugins/env" && init?.method === "POST") {
        envSet = true;
        return jsonResponse({ ok: true, name: "WEATHER_API_KEY", configured: true });
      }
      if (url === "/api/plugins/local/weather/decision" && init?.method === "POST") {
        approved = true;
        return jsonResponse(makeSnapshot());
      }
      if (url === "/api/plugins/restart" && init?.method === "POST") {
        restarted = true;
        return jsonResponse({ pending: true, state: "pending" });
      }
      return jsonResponse({});
    });

    render(<PluginsPanel />);

    expect(await screen.findByText("天气扩展")).toBeInTheDocument();
    expect(screen.getByText("缺失凭据 (1)")).toBeInTheDocument();
    expect(screen.queryByText(/hooks:mutate/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "展开 weather 配置" }));
    expect(screen.getByText(/hooks:mutate/)).toBeInTheDocument();
    expect(screen.getByText("缺失")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "录入" }));
    const pwdInput = await screen.findByPlaceholderText("请输入密钥值（提交后不回显）");
    fireEvent.change(pwdInput, { target: { value: "my-weather-key" } });
    fireEvent.click(screen.getByRole("button", { name: "保存到 .env" }));

    await waitFor(() => {
      expect(screen.getByText("已配置")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "批准并启用" }));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "重启应用使变更生效" })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "重启应用使变更生效" }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/plugins/restart",
        expect.objectContaining({ method: "POST" }),
      );
    });
  });

  it("manages MCP servers including reconnect", async () => {
    let reconnected = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/plugins") {
        return jsonResponse({
          revision: REV_1,
          plugins: [],
          mcp_servers: [
            {
              id: "github",
              kind: "mcp",
              enabled: true,
              trusted: true,
              status: "degraded",
              error: "timeout",
              transport: "stdio",
              tools: [],
              env_vars: [],
              missing_env: [],
            },
          ],
          permission_catalog: {},
          restart_required: false,
          restart: { available: true, state: "idle" },
        });
      }
      if (url === "/api/plugins/mcp/github/reconnect" && init?.method === "POST") {
        reconnected = true;
        return jsonResponse({
          revision: REV_1,
          plugins: [],
          mcp_servers: [
            {
              id: "github",
              kind: "mcp",
              enabled: true,
              trusted: true,
              status: "active",
              error: null,
              transport: "stdio",
              tools: [{ raw_name: "search", name: "mcp_github__search", enabled: true }],
              env_vars: [],
              missing_env: [],
            },
          ],
          permission_catalog: {},
          restart_required: false,
          restart: { available: true, state: "idle" },
        });
      }
      return jsonResponse({});
    });

    render(<PluginsPanel />);
    const mcpTab = await screen.findByText(/MCP 服务 \(1\)/);
    fireEvent.click(mcpTab);
    fireEvent.mouseDown(mcpTab);

    expect(await screen.findByText("github")).toBeInTheDocument();
    expect(screen.getByText("异常降级")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /立即重连/ }));
    await waitFor(() => {
      expect(reconnected).toBe(true);
      expect(screen.getByText("mcp_github__search")).toBeInTheDocument();
    });
  });

  it("renders bundled plugin without delete button and edits string_list config", async () => {
    let postedBody: Record<string, unknown> | null = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/plugins") {
        return jsonResponse({
          revision: REV_1,
          plugins: [
            {
              id: "browser",
              kind: "local",
              bundled: true,
              enabled: true,
              trusted: true,
              saved_enabled: true,
              saved_trusted: true,
              status: "active",
              error: null,
              manifest: {
                id: "browser",
                name: "浏览器自动化",
                version: "1.0.0",
                description: "封装 agent-browser CLI",
                permissions: ["tools:register", "network:outbound"],
                requires_env: [],
                config_schema: {
                  allowed_domains: {
                    key: "allowed_domains",
                    label: "域名白名单",
                    type: "string_list",
                    default: [],
                  },
                },
              },
              permission_details: [],
              config: { allowed_domains: [] },
              saved_config: { allowed_domains: [] },
              disabled_tools: [],
              tools: [{ raw_name: "browser", name: "p_browser__browser", enabled: true }],
              commands: [],
              hooks: [],
              skills: [],
              missing_env: [],
              env_vars: [],
              env_status: {},
              pending_restart: false,
            },
          ],
          mcp_servers: [],
          permission_catalog: {},
          restart_required: false,
          restart: { available: true, state: "idle" },
        });
      }
      if (url === "/api/plugins/local/browser/decision" && init?.method === "POST") {
        postedBody = JSON.parse(String(init.body ?? "{}")) as Record<string, unknown>;
        return jsonResponse({
          revision: REV_2,
          plugins: [
            {
              id: "browser",
              kind: "local",
              bundled: true,
              enabled: true,
              trusted: true,
              saved_enabled: true,
              saved_trusted: true,
              status: "active",
              error: null,
              manifest: {
                id: "browser",
                name: "浏览器自动化",
                version: "1.0.0",
                description: "封装 agent-browser CLI",
                permissions: ["tools:register", "network:outbound"],
                requires_env: [],
                config_schema: {
                  allowed_domains: {
                    key: "allowed_domains",
                    label: "域名白名单",
                    type: "string_list",
                    default: [],
                  },
                },
              },
              permission_details: [],
              config: { allowed_domains: ["example.com", "*.example.org"] },
              saved_config: { allowed_domains: ["example.com", "*.example.org"] },
              disabled_tools: [],
              tools: [{ raw_name: "browser", name: "p_browser__browser", enabled: true }],
              commands: [],
              hooks: [],
              skills: [],
              missing_env: [],
              env_vars: [],
              env_status: {},
              pending_restart: false,
            },
          ],
          mcp_servers: [],
          permission_catalog: {},
          restart_required: true,
          restart: { available: true, state: "idle" },
        });
      }
      return jsonResponse({});
    });

    render(<PluginsPanel />);
    expect(await screen.findByText("浏览器自动化")).toBeInTheDocument();
    expect(screen.getByText("内置插件")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("每行一项")).not.toBeInTheDocument();

    // 内置插件不渲染删除按钮
    const buttons = screen.getAllByRole("button");
    expect(buttons.some((b) => b.querySelector("svg.lucide-trash-2"))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "展开 browser 配置" }));
    expect(screen.getByText("p_browser__browser")).toBeInTheDocument();

    const textarea = screen.getByPlaceholderText("每行一项");
    fireEvent.change(textarea, { target: { value: "example.com\n *.example.org \n" } });
    fireEvent.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => {
      expect(postedBody).not.toBeNull();
      expect((postedBody?.config as Record<string, unknown>)?.allowed_domains).toEqual([
        "example.com",
        "*.example.org",
      ]);
    });

    fireEvent.click(screen.getByRole("button", { name: "收起 browser 配置" }));
    expect(screen.queryByPlaceholderText("每行一项")).not.toBeInTheDocument();
  });
});
