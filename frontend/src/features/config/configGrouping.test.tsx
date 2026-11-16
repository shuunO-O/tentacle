import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ConfigurationPanel } from "./ConfigurationPanel";
import {
  fieldSection,
  groupFieldsForDisplay,
  listSectionSubgroups,
} from "./configGrouping";
import type { ConfigField } from "./types";

function makeField(partial: Partial<ConfigField> & Pick<ConfigField, "path" | "label">): ConfigField {
  return {
    kind: "string",
    default: "",
    value: "",
    ...partial,
  };
}

describe("configGrouping", () => {
  it("routes all real Tentacle path prefixes into the expected tabs", () => {
    expect(fieldSection("providers.ds.base_url")).toBe("models");
    expect(fieldSection("models.chat.name")).toBe("models");
    expect(fieldSection("roles.chat.model")).toBe("models");

    expect(fieldSection("memory.embedding.dimensions")).toBe("memory");
    expect(fieldSection("memory.concept_alignment.enabled")).toBe("memory");
    expect(fieldSection("memory.concept_dedup.model_role")).toBe("memory");

    expect(fieldSection("webui.port")).toBe("channels");
    expect(fieldSection("web_tools.timeout_seconds")).toBe("channels");
    expect(fieldSection("media_inputs.download.backend")).toBe("channels");

    expect(fieldSection("app.timezone")).toBe("runtime");
    expect(fieldSection("heartbeat.session_switch.window_start")).toBe("runtime");
    expect(fieldSection("scheduler.catch_up_grace_hours")).toBe("runtime");
    expect(fieldSection("logging.level")).toBe("runtime");
    expect(fieldSection("subagents.enabled")).toBe("runtime");
    expect(fieldSection("usage.balance_provider")).toBe("runtime");
  });

  it("collapses untouched GCP fields for api_key auth and auto-expands for adc or search", () => {
    const fields: ConfigField[] = [
      makeField({ path: "models.chat.name", label: "模型名称", value: "deepseek-chat" }),
      makeField({ path: "models.chat.auth", label: "认证方式", value: "api_key" }),
      makeField({ path: "models.chat.project", label: "Google Cloud 项目", default: null, value: null, nullable: true }),
      makeField({ path: "models.chat.location", label: "Google Cloud 区域", default: "global", value: "global" }),
      makeField({ path: "models.chat.vision", label: "允许图片输入", kind: "boolean", value: false }),
    ];

    const apiKeyGroups = groupFieldsForDisplay({
      section: "models",
      fields,
      draft: {},
      focusedModelPrefix: "models.chat.",
    });
    const gcpCollapsed = apiKeyGroups.find((g) => g.id === "entity-gcp");
    expect(gcpCollapsed?.collapsible).toBe(true);
    expect(gcpCollapsed?.defaultCollapsed).toBe(true);

    const adcGroups = groupFieldsForDisplay({
      section: "models",
      fields,
      draft: { "models.chat.auth": "adc" },
      focusedModelPrefix: "models.chat.",
    });
    expect(adcGroups.find((g) => g.id === "entity-gcp")?.defaultCollapsed).toBe(false);

    const searchedGroups = groupFieldsForDisplay({
      section: "models",
      fields,
      draft: {},
      forceExpand: true,
      focusedModelPrefix: "models.chat.",
    });
    expect(searchedGroups.find((g) => g.id === "entity-gcp")?.defaultCollapsed).toBe(false);
  });

  it("extracts ordered sub-groups with dirty counts for non-model tabs", () => {
    const fields: ConfigField[] = [
      makeField({ path: "scheduler.catch_up_grace_hours", label: "单次任务补跑" }),
      makeField({ path: "heartbeat.enabled", label: "启用心跳", kind: "boolean" }),
      makeField({ path: "heartbeat.session_switch.enabled", label: "启用切换", kind: "boolean" }),
      makeField({ path: "app.timezone", label: "程序时区" }),
      makeField({ path: "logging.level", label: "日志级别" }),
    ];

    const sub = listSectionSubgroups("runtime", fields, { "heartbeat.enabled": false });
    expect(sub.map((s) => s.id)).toEqual([
      "runtime-app",
      "runtime-heartbeat",
      "runtime-session-switch",
      "runtime-scheduler",
      "runtime-ops",
    ]);
    expect(sub.find((s) => s.id === "runtime-heartbeat")?.dirtyCount).toBe(1);
  });
});

describe("ConfigurationPanel organization", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("collapses GCP fields cleanly and categorizes channel/runtime sections", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input) === "/api/configuration") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              revision: "c".repeat(64),
              active_role: "chat",
              active_model: { name: "example-chat" },
              restart: { available: true, state: "idle" },
              entities: {
                providers: [{ id: "example", label: "示例连接", type: "google_developer", references: [] }],
                models: [{ id: "example/chat", label: "对话模型", provider: "example", name: "example-chat", references: [] }],
                roles: [{ id: "chat", label: "主对话", model: "example/chat", references: [] }],
              },
              provider_types: {
                google_developer: {
                  label: "Google Gemini API",
                  auth: "api_key",
                  base_url: "https://generativelanguage.googleapis.com/v1beta",
                  api_style: "interactions",
                  protocols: ["interactions"],
                },
              },
              fields: [
                makeField({ path: "providers.example.auth", label: "认证方式", value: "api_key", choices: ["api_key", "adc"] }),
                makeField({ path: "providers.example.project", label: "Google Cloud 项目", default: null, value: null, nullable: true }),
                makeField({ path: "models.example/chat.name", label: "模型名称", value: "example-chat" }),
                makeField({ path: "roles.chat.model", label: "使用模型", value: "example/chat", choices: ["example/chat"] }),
                makeField({ path: "app.timezone", label: "程序时区", value: "Asia/Shanghai", help: "IANA 时区名称" }),
                makeField({ path: "webui.port", label: "监听端口", kind: "integer", value: 8420 }),
                makeField({ path: "web_tools.timeout_seconds", label: "请求超时（秒）", kind: "number", value: 60 }),
              ],
              connection_status: { example: { auth: "api_key", configured: true } },
              history: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    render(<ConfigurationPanel />);
    await waitFor(() => {
      expect(screen.getByText("roles.chat")).toBeInTheDocument();
    });

    // Click provider to inspect provider fields including collapsed GCP fields
    fireEvent.click(document.querySelector('[data-config-provider="example"]')!);
    expect(screen.getByText(/已按需折叠：包含 Google Cloud 项目/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^展开 \(/ }));
    expect(screen.getByText("Google Cloud 项目")).toBeInTheDocument();

    // Verify channels tab receives webui & web_tools fields grouped cleanly
    fireEvent.click(screen.getByRole("button", { name: /通道与媒体/ }));
    expect(screen.getAllByText("Web 工作台服务").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("公开网页工具").length).toBeGreaterThanOrEqual(1);

    // Verify runtime tab displays field help text
    fireEvent.click(screen.getByRole("button", { name: /运行与调度/ }));
    expect(screen.getByText("IANA 时区名称")).toBeInTheDocument();
  });

  it("auto-expands provider-model-role tree and renders 3-tier hierarchy chain navigation", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input) === "/api/configuration") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              revision: "d".repeat(64),
              active_role: "chat",
              active_model: { name: "example-chat" },
              restart: { available: true, state: "idle" },
              entities: {
                providers: [{ id: "example", label: "示例连接", type: "openai_compatible", references: [] }],
                models: [{ id: "example/chat", label: "对话模型", provider: "example", name: "example-chat", references: [] }],
                roles: [{ id: "chat", label: "主对话", model: "example/chat", references: [] }],
              },
              provider_types: {
                openai_compatible: {
                  label: "OpenAI 兼容",
                  auth: "api_key",
                  base_url: "https://example.test/v1",
                  api_style: "chat",
                  protocols: ["chat"],
                },
              },
              fields: [
                makeField({ path: "providers.example.base_url", label: "API 地址", kind: "url", value: "https://example.test/v1" }),
                makeField({ path: "models.example/chat.name", label: "模型名称", value: "example-chat" }),
                makeField({ path: "roles.chat.model", label: "使用模型", value: "example/chat", choices: ["example/chat"] }),
              ],
              connection_status: { example: { auth: "api_key", configured: true } },
              history: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    render(<ConfigurationPanel />);
    await waitFor(() => {
      expect(screen.getByTestId("config-hierarchy-chain")).toBeInTheDocument();
    });

    // Provider tree branch is auto-expanded, showing child model and bound role immediately
    expect(document.querySelector('[data-config-model="example/chat"]')).toBeInTheDocument();

    // Jump up from Role -> Model -> Provider via the 3-tier relationship chain
    const chain = screen.getByTestId("config-hierarchy-chain");
    const chainButtons = chain.querySelectorAll("button");
    fireEvent.click(chainButtons[1]!);
    await waitFor(() => {
      expect(screen.getByText("models.example/chat")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId("config-hierarchy-chain").querySelectorAll("button")[0]!);
    await waitFor(() => {
      expect(screen.getByText("providers.example")).toBeInTheDocument();
    });
  });
});
