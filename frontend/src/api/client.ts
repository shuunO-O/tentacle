import createClient from "openapi-fetch";
import type { paths } from "./schema";

export const TOKEN_STORAGE_KEY = "tentacle_token";

export class ApiError extends Error {
  status: number;
  round?: Record<string, unknown>;
  code?: string;

  constructor(message: string, status: number, round?: Record<string, unknown>, code?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.round = round;
    this.code = code;
  }
}

let unauthorizedHandler: (() => void | Promise<void>) | null = null;

export function setUnauthorizedHandler(fn: (() => void | Promise<void>) | null): void {
  unauthorizedHandler = fn;
}

export function getStoredToken(): string {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY) || "";
  } catch {
    return "";
  }
}

export function setStoredToken(token: string): void {
  try {
    if (token) {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
    } else {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
    }
  } catch {
    // Ignore storage quota or SSR/private-mode restrictions.
  }
}

export function clearStoredToken(): void {
  setStoredToken("");
}

function authHeaders(extra?: HeadersInit): Record<string, string> {
  const headers: Record<string, string> = {};
  if (extra) {
    new Headers(extra).forEach((value, key) => {
      headers[key] = value;
    });
  }
  const token = getStoredToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  return headers;
}

async function handleUnauthorized(): Promise<never> {
  clearStoredToken();
  if (unauthorizedHandler) {
    await unauthorizedHandler();
  }
  throw new ApiError("unauthorized", 401);
}

/**
 * Authenticated raw fetch helper (attaches Authorization Bearer header, handles 401
 * when `handleAuthFailure` is true, and returns the raw Response).
 */
export async function apiRawFetch(
  path: string,
  init: RequestInit = {},
  options: { handleAuthFailure?: boolean } = {},
): Promise<Response> {
  const headers = authHeaders(init.headers);
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401 && options.handleAuthFailure !== false) {
    return handleUnauthorized();
  }
  return res;
}

/**
 * Authenticated JSON / text request helper that preserves Tentacle error semantics
 * (`ApiError.status`, `ApiError.round`) and clears tokens on 401.
 */
export async function apiFetch<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = authHeaders(init.headers);
  if (!(init.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(path, { ...init, headers });
  if (res.status === 401) {
    return handleUnauthorized();
  }

  if (!res.ok) {
    const payload = (await res.json().catch(() => ({ detail: res.statusText }))) as {
      detail?: string;
      round?: Record<string, unknown>;
      code?: string;
    };
    throw new ApiError(payload.detail || "请求失败", res.status, payload.round, payload.code);
  }

  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return (await res.json()) as T;
  }
  return (await res.text()) as unknown as T;
}

export interface UploadedFileRecord {
  local_id: string;
  path: string;
  filename: string;
  mime?: string;
  size?: number;
}

export async function apiUploadFile(file: File): Promise<UploadedFileRecord> {
  const form = new FormData();
  form.append("file", file);
  const res = await apiFetch<{ ok: boolean; file: UploadedFileRecord }>("/api/files/upload", {
    method: "POST",
    body: form,
  });
  return res.file;
}

function filenameFromDisposition(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch {
      // Fallback to plain filename below
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain?.[1]?.trim() || fallback;
}

async function triggerBlobDownload(
  res: Response,
  fallbackName: string,
  errorLabel: string,
): Promise<void> {
  if (res.status === 401) {
    return handleUnauthorized();
  }
  if (!res.ok) {
    const payload = (await res.json().catch(() => ({ detail: res.statusText }))) as {
      detail?: string;
    };
    throw new ApiError(payload.detail || errorLabel, res.status);
  }
  const blob = await res.blob();
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filenameFromDisposition(res.headers.get("content-disposition"), fallbackName);
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

/**
 * Authenticated GET blob download helper for exported archives and backup files.
 */
export async function apiDownloadGet(
  path: string,
  fallbackName: string,
  errorLabel = "下载失败",
): Promise<void> {
  const res = await fetch(path, { headers: authHeaders() });
  return triggerBlobDownload(res, fallbackName, errorLabel);
}

/**
 * Authenticated POST JSON blob download helper (used for ephemeral encrypted migration exports).
 */
export async function apiDownloadPost(
  path: string,
  body: unknown,
  fallbackName: string,
  errorLabel = "导出失败",
): Promise<void> {
  const headers = authHeaders({ "Content-Type": "application/json" });
  const res = await fetch(path, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  return triggerBlobDownload(res, fallbackName, errorLabel);
}

export { authHeaders };

/**
 * Downloads a controlled outgoing file via `/api/files/download` with Bearer header
 * so credentials never enter link hrefs or message history.
 */
export async function apiDownloadFile(filePath: string): Promise<void> {
  const url = "/api/files/download?" + new URLSearchParams({ path: filePath });
  const res = await fetch(url, { headers: authHeaders() });
  return triggerBlobDownload(res, filePath.split("/").pop() || "download", "下载失败");
}

export async function apiDownloadMediaById(localId: string, filename?: string): Promise<void> {
  const res = await fetch(mediaContentUrl(localId), { headers: authHeaders() });
  return triggerBlobDownload(res, filename || localId, "下载附件失败");
}

export function mediaContentUrl(localId: string): string {
  return `/api/files/${encodeURIComponent(localId)}/content`;
}

/** Typed openapi-fetch client for callers preferring path/method inference. */
export const openapi = createClient<paths>({
  baseUrl: "",
  fetch: async (input) => {
    const headers = authHeaders(input.headers);
    return fetch(new Request(input, { headers }));
  },
});
