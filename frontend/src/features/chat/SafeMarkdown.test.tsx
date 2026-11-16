import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SafeMarkdown } from "./SafeMarkdown";

describe("SafeMarkdown", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("blocks raw HTML execution and javascript links", () => {
    const { container } = render(
      <SafeMarkdown text={"text <script>alert(1)</script> [bad](javascript:alert(1)) [ok](https://example.com)"} />,
    );
    expect(container.querySelector("script")).toBeNull();
    const links = Array.from(container.querySelectorAll("a"));
    expect(links).toHaveLength(1);
    expect(links[0]?.getAttribute("href")).toBe("https://example.com");
    expect(screen.getByText("bad")).toBeInTheDocument();
  });

  it("renders file download buttons for [file:...] markers and uses fetch download", async () => {
    const fetchMock = vi.fn(async () => new Response(new Blob(["ok"]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const originalCreate = URL.createObjectURL;
    const originalRevoke = URL.revokeObjectURL;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(() => "blob:mock-file"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      writable: true,
      value: vi.fn(() => {}),
    });

    render(<SafeMarkdown text="请查收 [file:reports/q1.pdf]" allowFiles />);
    const btn = screen.getByRole("button", { name: /q1\.pdf/i });
    fireEvent.click(btn);

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/files/download?path=reports%2Fq1.pdf",
        expect.any(Object),
      );
    });
    expect(await screen.findByText("再次下载")).toBeInTheDocument();

    Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: originalCreate });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: originalRevoke });
  });
});
