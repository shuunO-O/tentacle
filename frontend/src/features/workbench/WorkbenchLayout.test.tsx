import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLayoutStore } from "./layoutStore";
import { WorkbenchLayout } from "./WorkbenchLayout";

describe("WorkbenchLayout", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ commands: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    })));
    useLayoutStore.setState({
      primaryPanel: "chat",
      secondaryPanel: null,
      sidebarCollapsed: false,
      drawerOpen: false,
      mobileNavOpen: false,
      domainStatus: {
        runningSubagents: 0,
        codingEnabled: false,
        dedupRunning: false,
        updateAvailable: false,
        latestEventNote: null,
      },
    });
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders expanded grouped desktop sidebar with text labels and hides top multi-tab bar by default", async () => {
    render(<WorkbenchLayout />);
    const rail = await screen.findByTestId("desktop-rail");
    expect(rail).toHaveAttribute("data-collapsed", "false");
    expect(screen.getByRole("navigation", { name: "工作区面板" })).toBeInTheDocument();
    expect(screen.getByText("记忆图谱")).toBeInTheDocument();
    expect(screen.getByText("系统配置")).toBeInTheDocument();
    expect(screen.queryByTestId("split-bar")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "收起侧边栏" }));
    expect(rail).toHaveAttribute("data-collapsed", "true");
    expect(useLayoutStore.getState().sidebarCollapsed).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "展开侧边栏" }));
    expect(rail).toHaveAttribute("data-collapsed", "false");
  });

  it("supports contextual on-demand split pane on desktop", async () => {
    render(<WorkbenchLayout />);
    const splitBtn = await screen.findByRole("button", { name: "切换分屏" });
    fireEvent.click(splitBtn);
    expect(useLayoutStore.getState().secondaryPanel).toBe("graph");
    expect(await screen.findByTestId("split-bar")).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "记忆图谱" })).toBeInTheDocument();

    const secondarySelect = screen.getByRole("combobox", { name: "选择右侧分屏面板" });
    fireEvent.change(secondarySelect, { target: { value: "subagents" } });
    expect(useLayoutStore.getState().secondaryPanel).toBe("subagents");

    fireEvent.click(screen.getByRole("button", { name: "关闭分屏" }));
    expect(useLayoutStore.getState().secondaryPanel).toBeNull();
    expect(screen.queryByTestId("split-bar")).not.toBeInTheDocument();
  });

  it("switches to compact mobile drawer mode at <= 860px", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: query.includes("860px"),
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    render(<WorkbenchLayout />);
    const navBtn = await screen.findByRole("button", { name: "打开导航菜单" });
    fireEvent.click(navBtn);
    expect(useLayoutStore.getState().mobileNavOpen).toBe(true);
    expect(await screen.findByTestId("mobile-nav-drawer")).toBeInTheDocument();
  });

  it("shows sidebar DSH shortcut when codingEnabled is true and pre-opens tab safely", async () => {
    const replaceMock = vi.fn();
    const openSpy = vi.spyOn(window, "open").mockReturnValue({
      closed: false,
      opener: window,
      document: { title: "", body: { textContent: "" } },
      location: { replace: replaceMock },
      close: vi.fn(),
    } as unknown as Window);
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input) === "/api/subagents/dsh/access") {
          return new Response(JSON.stringify({ url: "/coding/" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify({ commands: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );

    useLayoutStore.setState({
      domainStatus: {
        runningSubagents: 1,
        codingEnabled: true,
        dedupRunning: false,
        updateAvailable: false,
        latestEventNote: null,
      },
    });

    render(<WorkbenchLayout />);
    const dshBtn = await screen.findByTestId("dsh-nav-btn");
    expect(dshBtn).toHaveTextContent("DSH 编码台");
    fireEvent.click(dshBtn);
    expect(openSpy).toHaveBeenCalledWith("", "_blank");
    await vi.waitFor(() => {
      expect(replaceMock).toHaveBeenCalledWith(expect.stringMatching(/\/coding\/$/));
    });
    openSpy.mockRestore();
  });
});
