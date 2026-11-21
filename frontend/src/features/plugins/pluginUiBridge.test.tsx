import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setStoredToken } from "../../api/client";
import { useLayoutStore } from "../workbench/layoutStore";
import { panelFromPathname, pluginPanelId } from "../workbench/panels";
import { PluginUiPanel } from "./PluginUiPanel";
import {
  executePluginExtFetch,
  injectBridgeIntoHtml,
  PLUGIN_UI_CHANNEL,
  sanitizePluginAssetPath,
  sanitizePluginExtPath,
} from "./pluginUiBridge";

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("pluginUiBridge security & routing", () => {
  afterEach(() => {
    cleanup();
    setStoredToken("");
    vi.restoreAllMocks();
  });

  it("constrains plugin API and asset paths strictly within plugin namespace", () => {
    expect(sanitizePluginExtPath("viz", "/metrics?limit=10#top")).toBe(
      "/api/plugins/ext/viz/metrics?limit=10",
    );
    expect(sanitizePluginExtPath("viz", "")).toBe("/api/plugins/ext/viz");
    expect(sanitizePluginAssetPath("viz", "charts/data.json")).toBe(
      "/api/plugins/viz/ui/assets/charts/data.json",
    );

    for (const bad of [
      "../other/secret",
      "%2e%2e/config",
      "a/../../settings",
      "https://evil.example/steal",
      "//evil.example/steal",
      "foo\\bar",
      "bad\0path",
    ]) {
      expect(() => sanitizePluginExtPath("viz", bad)).toThrow();
      expect(() => sanitizePluginAssetPath("viz", bad)).toThrow();
    }
    expect(() => sanitizePluginAssetPath("viz", "")).toThrow();
  });

  it("resolves /plugin/<id> pathname into dynamic PluginPanelId", () => {
    expect(panelFromPathname("/plugin/viz")).toBe(pluginPanelId("viz"));
    expect(panelFromPathname("/plugin/my_tool-2/")).toBe(pluginPanelId("my_tool-2"));
    expect(panelFromPathname("/plugin/INVALID")).toBe("chat");
  });

  it("injects host theme vars and window.tentacle bridge after CSP meta", () => {
    const raw =
      '<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" content="connect-src \'none\';"></head><body><h1>Hi</h1></body></html>';
    const injected = injectBridgeIntoHtml(raw, {
      pluginId: "viz",
      bridgeId: "bridge-1",
      themeSnapshot: { theme: "dark", cssVars: { "--t-bg": "#1d1813" } },
    });
    const cspIdx = injected.indexOf("Content-Security-Policy");
    const bridgeIdx = injected.indexOf("data-tentacle-bridge");
    expect(cspIdx).toBeGreaterThan(-1);
    expect(bridgeIdx).toBeGreaterThan(cspIdx);
    expect(injected).toContain("--t-bg: #1d1813;");
    expect(injected).toContain('"pluginId":"viz"');
  });

  it("executePluginExtFetch strips sensitive request headers and attaches host bearer token", async () => {
    setStoredToken("host-secret-token");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "X-Plugin-Mode": "fast",
          "Set-Cookie": "should-not-leak=1",
        },
      }),
    );

    const res = await executePluginExtFetch("viz", {
      path: "/items?page=2",
      method: "POST",
      headers: {
        Authorization: "Bearer spoofed",
        Cookie: "spoof=1",
        "X-Custom": "hello",
      },
      body: { count: 3 },
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/plugins/ext/viz/items?page=2");
    const sentHeaders = init?.headers as Record<string, string>;
    expect(sentHeaders["Authorization"]).toBe("Bearer host-secret-token");
    expect(sentHeaders["cookie"] ?? sentHeaders["Cookie"]).toBeUndefined();
    expect(sentHeaders["x-custom"]).toBe("hello");
    expect(res.ok).toBe(true);
    expect(res.data).toEqual({ ok: true });
    expect(res.headers["x-plugin-mode"]).toBe("fast");
    expect(res.headers["set-cookie"]).toBeUndefined();
  });

  it("PluginUiPanel renders sandboxed iframe without allow-same-origin and proxies postMessage RPC", async () => {
    setStoredToken("host-token");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/plugins/viz/ui/document") {
        return jsonResponse({
          plugin_id: "viz",
          name: "可视化仪表盘",
          version: "1.2.0",
          title: "实时看板",
          description: "交互式看板",
          placement: "sidebar",
          has_routes: true,
          html: '<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy" content="connect-src \'none\';"></head><body><div id="app"></div></body></html>',
        });
      }
      if (url === "/api/plugins/ext/viz/metrics") {
        return jsonResponse({ points: [10, 20] });
      }
      return jsonResponse({}, 404);
    });

    render(<PluginUiPanel pluginId="viz" />);

    const iframe = (await screen.findByTestId("plugin-ui-iframe")) as HTMLIFrameElement;
    expect(iframe.getAttribute("sandbox")).toBe("allow-scripts allow-forms allow-downloads");
    expect(iframe.getAttribute("sandbox")).not.toContain("allow-same-origin");

    const srcDoc = iframe.getAttribute("srcdoc") || "";
    const match = srcDoc.match(/"bridgeId":"([^"]+)"/);
    expect(match?.[1]).toBeTruthy();
    const bridgeId = match![1]!;

    const postMessageSpy = vi.fn();
    Object.defineProperty(iframe, "contentWindow", {
      value: { postMessage: postMessageSpy },
      configurable: true,
    });

    // 伪造来源消息应被忽略
    fireEvent(
      window,
      new MessageEvent("message", {
        source: window,
        data: {
          channel: PLUGIN_UI_CHANNEL,
          bridgeId,
          kind: "request",
          id: "spoof_1",
          action: "api.fetch",
          payload: { path: "/metrics" },
        },
      }),
    );
    expect(postMessageSpy).not.toHaveBeenCalled();

    // 来自目标 iframe 的合法 RPC 请求由宿主代转发到 /api/plugins/ext/viz/metrics
    fireEvent(
      window,
      new MessageEvent("message", {
        source: iframe.contentWindow as unknown as Window,
        data: {
          channel: PLUGIN_UI_CHANNEL,
          bridgeId,
          kind: "request",
          id: "req_1",
          action: "api.fetch",
          payload: { path: "/metrics" },
        },
      }),
    );

    await waitFor(() => {
      expect(postMessageSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          channel: PLUGIN_UI_CHANNEL,
          bridgeId,
          kind: "response",
          id: "req_1",
          ok: true,
          result: expect.objectContaining({
            status: 200,
            data: { points: [10, 20] },
          }),
        }),
        "*",
      );
    });

    // ui.askChat 通知应预填主对话输入框
    fireEvent(
      window,
      new MessageEvent("message", {
        source: iframe.contentWindow as unknown as Window,
        data: {
          channel: PLUGIN_UI_CHANNEL,
          bridgeId,
          kind: "notify",
          action: "ui.askChat",
          payload: { prompt: "请分析这个图表趋势" },
        },
      }),
    );

    await waitFor(() => {
      expect(useLayoutStore.getState().pendingChatPrompt).toBe("请分析这个图表趋势");
    });

    // ui.openChat 通知应直接切回主会话面板（不发送或预填消息）
    useLayoutStore.getState().openPanel(pluginPanelId("viz"));
    expect(useLayoutStore.getState().primaryPanel).toBe(pluginPanelId("viz"));
    fireEvent(
      window,
      new MessageEvent("message", {
        source: iframe.contentWindow as unknown as Window,
        data: {
          channel: PLUGIN_UI_CHANNEL,
          bridgeId,
          kind: "notify",
          action: "ui.openChat",
          payload: {},
        },
      }),
    );
    await waitFor(() => {
      expect(useLayoutStore.getState().primaryPanel).toBe("chat");
    });
    expect(fetchMock).toHaveBeenCalled();
  });
});
