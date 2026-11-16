import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { wsClient } from "./api/ws";

describe("App bootstrap", () => {
  beforeEach(() => {
    vi.spyOn(wsClient, "connect").mockImplementation(() => {});
    vi.spyOn(wsClient, "disconnect").mockImplementation(() => {});
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/auth/state") {
          return new Response(
            JSON.stringify({
              has_password: false,
              device_auth: false,
              device_ok: true,
              freeze: false,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        if (url === "/api/settings") {
          return new Response(
            JSON.stringify({
              agent_name: "Tentacle",
              show_reasoning: true,
              split_messages: true,
              has_password: false,
              device_auth: false,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ commands: [] }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders workbench root and authenticates cleanly", async () => {
    render(<App />);
    expect(screen.getByTestId("workbench-root")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText("Tentacle").length).toBeGreaterThan(0);
    });
  });
});
