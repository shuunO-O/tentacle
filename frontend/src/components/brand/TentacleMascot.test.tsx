import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "../../features/chat/chatStore";
import { MessageTimeline } from "../../features/chat/MessageTimeline";
import { useLayoutStore } from "../../features/workbench/layoutStore";
import { WorkbenchLayout } from "../../features/workbench/WorkbenchLayout";
import { TentacleMascot, type TentaclePose } from "./TentacleMascot";

describe("TentacleMascot", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify({ commands: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    useChatStore.getState().resetSessionState();
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

  it("renders all 6 poses and switches knock optical size at <= 18px", () => {
    const poses: TentaclePose[] = ["knock", "wave", "think", "reach", "sleep", "cheer"];
    for (const pose of poses) {
      const { container, unmount } = render(
        <TentacleMascot pose={pose} size={32} animate="loop" />,
      );
      const svg = container.querySelector("svg");
      expect(svg).toHaveAttribute("data-mascot-pose", pose);
      expect(svg).toHaveAttribute("data-mascot-optical", "master");
      expect(svg).toHaveAttribute("data-mascot-animate", "loop");
      unmount();
    }

    const { container } = render(<TentacleMascot pose="knock" size={16} animate="loop" />);
    const microSvg = container.querySelector("svg");
    expect(microSvg).toHaveAttribute("data-mascot-optical", "micro");
    expect(microSvg).toHaveAttribute("data-mascot-animate", "none");
  });

  it("renders the knock logo in sidebar and triggers one-shot knock on click", async () => {
    render(<WorkbenchLayout />);
    const btn = await screen.findByTestId("brand-mascot-btn");
    const svg = () => btn.querySelector("svg");

    expect(svg()).toHaveAttribute("data-mascot-pose", "knock");
    expect(svg()).toHaveAttribute("data-mascot-animate", "none");

    fireEvent.click(btn);
    expect(svg()).toHaveAttribute("data-mascot-animate", "once");
  });

  it("renders the static knock logo in empty chat timeline", () => {
    const { container } = render(<MessageTimeline onFillPrompt={vi.fn()} onOpenImage={vi.fn()} />);
    const svg = container.querySelector("svg[data-mascot-pose]");
    expect(svg).toHaveAttribute("data-mascot-pose", "knock");
    expect(svg).toHaveAttribute("data-mascot-animate", "none");
  });
});
