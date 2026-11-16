import { apiFetch } from "../../api/client";
import { toast } from "../../components/ui/Toast";

const DSH_ACCESS_TIMEOUT_MS = 50_000;

function openPlaceholderTab(): Window | null {
  if (typeof window === "undefined" || typeof window.open !== "function") {
    return null;
  }
  const popup = window.open("", "_blank");
  if (!popup) return null;
  try {
    popup.opener = null;
    popup.document.title = "DSH 编程工作区 · 正在启动…";
    if (popup.document.body && !popup.document.body.textContent) {
      popup.document.body.textContent = "正在启动并连接 DSH 编程工作区，请稍候…";
    }
  } catch {
    // Ignore cross-window access restrictions in tests or hardened browsers.
  }
  return popup;
}

/**
 * Authorize `/coding/` via same-origin POST while pre-opening the target tab synchronously
 * to preserve browser popup permission during cold DSH startup.
 */
export async function openDshWorkspace(): Promise<void> {
  const popup = openPlaceholderTab();
  try {
    const signal =
      typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function"
        ? AbortSignal.timeout(DSH_ACCESS_TIMEOUT_MS)
        : undefined;
    const res = await apiFetch<{ url: string }>("/api/subagents/dsh/access", {
      method: "POST",
      signal,
    });
    if (!res.url) {
      throw new Error("未返回有效的 DSH 入口地址");
    }
    const targetUrl = new URL(res.url, window.location.href).toString();
    if (popup && !popup.closed) {
      popup.location.replace(targetUrl);
    } else if (!popup) {
      window.location.assign(targetUrl);
    }
  } catch (err) {
    try {
      if (popup && !popup.closed) {
        popup.close();
      }
    } catch {
      // Ignore close failures on restricted windows.
    }
    const isTimeout = err instanceof Error && err.name === "TimeoutError";
    const msg = isTimeout
      ? "等待 DSH 编程工作区启动超时，请重试"
      : err instanceof Error
        ? err.message
        : "无法进入 DSH 编程工作区";
    toast(msg, "error");
  }
}
