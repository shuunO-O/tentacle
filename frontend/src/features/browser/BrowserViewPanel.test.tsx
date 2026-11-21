import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserViewPanel } from "./BrowserViewPanel";
import { BrowserNekoControl } from "./BrowserNekoControl";
import { useLayoutStore } from "../workbench/layoutStore";

vi.mock("../../api/client", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "../../api/client";

const mockedApiFetch = vi.mocked(apiFetch);

describe("BrowserViewPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useLayoutStore.setState({
      primaryPanel: "browser",
      browserLiveView: null,
    });
  });

  afterEach(() => {
    cleanup();
  });

  it("renders empty state when live_view_url is not configured", async () => {
    mockedApiFetch.mockResolvedValue({
      configured: false,
      live_view_url: "",
      cdp_configured: false,
      cdp_reachable: false,
      mode: "local",
    });

    render(<BrowserViewPanel />);

    expect(await screen.findByText("尚未配置可视化浏览器地址")).toBeTruthy();
    expect(screen.queryByTestId("browser-live-iframe")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "前往插件配置" }));
    expect(useLayoutStore.getState().primaryPanel).toBe("plugins");
  });

  it("renders live iframe, opens external tab safely, and navigates back to chat", async () => {
    useLayoutStore.setState({
      primaryPanel: "browser",
      browserLiveView: {
        live_view_url: "https://browser.example.test/neko/",
        cdp_configured: true,
        mode: "cdp",
      },
    });
    mockedApiFetch.mockResolvedValue({
      configured: true,
      live_view_url: "https://browser.example.test/neko/",
      cdp_configured: true,
      cdp_reachable: true,
      browser_version: "Chrome/136.0",
      mode: "cdp",
    });

    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);

    render(<BrowserViewPanel />);

    const iframe = (await screen.findByTestId("browser-live-iframe")) as HTMLIFrameElement;
    expect(iframe.getAttribute("src")).toBe("https://browser.example.test/neko/");
    expect(iframe.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(iframe.getAttribute("allow")).toContain("clipboard-write");

    await waitFor(() => {
      expect(screen.getByText("CDP 在线")).toBeTruthy();
      expect(screen.getByText("Chrome/136.0")).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("button", { name: "新标签打开" }));
    expect(openSpy).toHaveBeenCalledWith(
      "https://browser.example.test/neko/",
      "_blank",
      "noopener,noreferrer",
    );

    fireEvent.click(screen.getByRole("button", { name: "回到会话" }));
    expect(useLayoutStore.getState().primaryPanel).toBe("chat");

    openSpy.mockRestore();
  });
});

describe("BrowserNekoControl", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("starts and stops Neko from plugin card and reveals reverse proxy config", async () => {
    mockedApiFetch.mockImplementation(async (path, init) => {
      if (path === "/api/plugins/ext/browser/neko/status") {
        return {
          ok: true,
          available: true,
          running: false,
          container_state: "exited",
          cdp_port: 9222,
          cdp_reachable: false,
          http_listen: "http://127.0.0.1:8080",
          mux_port: 52000,
          screen: "1280x720@24",
          proxy_config: "reverse_proxy 127.0.0.1:8080",
        };
      }
      if (path === "/api/plugins/ext/browser/neko/action") {
        const body = JSON.parse(String(init?.body || "{}"));
        const running = body.action === "start";
        return {
          ok: true,
          available: true,
          running,
          container_state: running ? "running" : "exited",
          cdp_port: 9222,
          cdp_reachable: running,
          http_listen: "http://127.0.0.1:8080",
          mux_port: 52000,
          screen: "1280x720@24",
          proxy_config: "reverse_proxy 127.0.0.1:8080",
        };
      }
      throw new Error(`unexpected path ${path}`);
    });

    render(<BrowserNekoControl hasUnsavedChanges />);

    expect(await screen.findByText("已停止")).toBeTruthy();
    expect(screen.getByText(/未保存修改/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "同步并启动" }));
    expect(await screen.findByText("运行中")).toBeTruthy();
    expect(screen.getByText("CDP 在线")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "反代配置" }));
    expect(screen.getByText("reverse_proxy 127.0.0.1:8080")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "停止" }));
    expect(await screen.findByText("已停止")).toBeTruthy();
  });

  it("shows one-command setup hint when control socket/docker is unavailable", async () => {
    mockedApiFetch.mockResolvedValue({
      ok: true,
      available: false,
      running: false,
      container_state: "unknown",
      cdp_reachable: false,
      setup_hint: "请先在服务器终端执行一次初始化。",
    });

    render(<BrowserNekoControl />);

    expect(await screen.findByText("请先在服务器终端执行一次初始化。")).toBeTruthy();
    expect(screen.getByText("sudo tentacle browser install --install-docker")).toBeTruthy();
    expect((screen.getByRole("button", { name: "同步并启动" }) as HTMLButtonElement).disabled).toBe(true);
  });
});
