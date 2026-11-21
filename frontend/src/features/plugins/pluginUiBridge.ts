/**
 * Tentacle 插件自定义沙箱 WebUI 宿主桥接协议与安全校验。
 *
 * 沙箱 iframe 以 `sandbox="allow-scripts allow-forms allow-downloads"`（无 `allow-same-origin`）
 * 和 `connect-src 'none'` 运行，所有与插件后端的交互均通过本模块经 `postMessage` 代理，
 * 严格限定在 `/api/plugins/ext/<plugin_id>/*` 与 `/api/plugins/<plugin_id>/ui/assets/*`。
 */

import { apiRawFetch } from "../../api/client";
import type { ResolvedTheme } from "../../stores/themeStore";

export const PLUGIN_UI_CHANNEL = "tentacle-plugin-ui";

const ALLOWED_METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE"]);
const FORBIDDEN_REQUEST_HEADERS = new Set([
  "authorization",
  "cookie",
  "host",
  "origin",
  "referer",
  "x-tentacle-device",
  "x-tentacle-device-secret",
]);
const SAFE_RESPONSE_HEADERS = new Set([
  "content-type",
  "content-length",
  "etag",
  "last-modified",
  "cache-control",
]);

const THEME_TOKEN_NAMES = [
  "--t-bg",
  "--t-bg-sidebar",
  "--t-bg-surface",
  "--t-bg-elevated",
  "--t-bg-input",
  "--t-bg-hover",
  "--t-bg-active",
  "--t-border",
  "--t-border-soft",
  "--t-border-strong",
  "--t-fg",
  "--t-fg-dim",
  "--t-fg-faint",
  "--t-accent",
  "--t-accent-hover",
  "--t-accent-fg",
  "--t-accent-soft",
  "--t-accent-ring",
  "--t-user-bubble",
  "--t-code-bg",
  "--t-ok",
  "--t-ok-soft",
  "--t-warn",
  "--t-warn-soft",
  "--t-danger",
  "--t-danger-soft",
  "--t-info",
  "--t-info-soft",
  "--t-graph-bg",
  "--t-graph-grid",
  "--t-graph-edge",
  "--t-graph-label",
  "--t-font-sans",
  "--t-font-mono",
] as const;

export interface HostThemeSnapshot {
  theme: ResolvedTheme;
  cssVars: Record<string, string>;
}

export interface PluginBridgeFetchRequest {
  path: string;
  method?: string;
  headers?: Record<string, string>;
  body?: unknown;
}

export interface PluginBridgeFetchResponse {
  ok: boolean;
  status: number;
  statusText: string;
  headers: Record<string, string>;
  data: unknown;
  text: string;
}

export interface PluginBridgeAssetRequest {
  path: string;
  responseType?: "text" | "json" | "dataUrl";
}

function validateRelativeSegments(rawPath: string, opts: { allowEmpty?: boolean } = {}): string {
  if (typeof rawPath !== "string" || rawPath.includes("\0") || rawPath.includes("\\")) {
    throw new Error("无效的插件请求路径");
  }
  const trimmed = rawPath.trim();
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(trimmed) || trimmed.startsWith("//")) {
    throw new Error("禁止访问外部或跨前缀路径");
  }
  const cleaned = trimmed.replace(/^\/+/, "");
  if (!cleaned) {
    if (opts.allowEmpty) return "";
    throw new Error("资源路径不能为空");
  }
  const segments = cleaned.split("/");
  const encoded: string[] = [];
  for (const seg of segments) {
    if (!seg || seg === "." || seg === "..") {
      throw new Error("禁止路径穿越");
    }
    let decoded: string;
    try {
      decoded = decodeURIComponent(seg);
    } catch {
      throw new Error("路径包含无效 URL 编码");
    }
    if (
      !decoded ||
      decoded === "." ||
      decoded === ".." ||
      decoded.includes("/") ||
      decoded.includes("\\") ||
      decoded.includes("\0")
    ) {
      throw new Error("禁止路径穿越");
    }
    encoded.push(encodeURIComponent(decoded));
  }
  return encoded.join("/");
}

/**
 * 将插件沙箱内传入的相对路径严格约束到 `/api/plugins/ext/<pluginId>/*`。
 */
export function sanitizePluginExtPath(pluginId: string, rawPath: string): string {
  if (typeof rawPath !== "string") {
    throw new Error("请求路径必须为字符串");
  }
  const noHash = rawPath.split("#", 1)[0] ?? "";
  const qIndex = noHash.indexOf("?");
  const pathPart = qIndex >= 0 ? noHash.slice(0, qIndex) : noHash;
  const queryPart = qIndex >= 0 ? noHash.slice(qIndex) : "";
  const encodedRel = validateRelativeSegments(pathPart, { allowEmpty: true });
  const base = `/api/plugins/ext/${encodeURIComponent(pluginId)}`;
  return encodedRel ? `${base}/${encodedRel}${queryPart}` : `${base}${queryPart}`;
}

/**
 * 将插件沙箱内传入的静态资源相对路径严格约束到 `/api/plugins/<pluginId>/ui/assets/*`。
 */
export function sanitizePluginAssetPath(pluginId: string, rawPath: string): string {
  if (typeof rawPath !== "string") {
    throw new Error("资源路径必须为字符串");
  }
  const noHash = rawPath.split("#", 1)[0] ?? "";
  const pathPart = noHash.split("?", 1)[0] ?? "";
  const encodedRel = validateRelativeSegments(pathPart, { allowEmpty: false });
  return `/api/plugins/${encodeURIComponent(pluginId)}/ui/assets/${encodedRel}`;
}

export function collectHostThemeSnapshot(resolvedTheme: ResolvedTheme): HostThemeSnapshot {
  const cssVars: Record<string, string> = {};
  if (typeof window !== "undefined" && typeof document !== "undefined") {
    try {
      const style = window.getComputedStyle(document.documentElement);
      for (const name of THEME_TOKEN_NAMES) {
        const val = style.getPropertyValue(name).trim();
        if (val) {
          cssVars[name] = val;
        }
      }
    } catch {
      // Ignore computed style errors in non-DOM environments.
    }
  }
  return { theme: resolvedTheme, cssVars };
}

function buildThemeStyleCss(snapshot: HostThemeSnapshot): string {
  const varLines = Object.entries(snapshot.cssVars)
    .filter(([k, v]) => k.startsWith("--t-") && !v.includes("</style"))
    .map(([k, v]) => `  ${k}: ${v};`)
    .join("\n");
  return `:root {\n  color-scheme: ${snapshot.theme};\n${varLines}\n}\nhtml, body {\n  background: var(--t-bg, transparent);\n  color: var(--t-fg, inherit);\n  font-family: var(--t-font-sans, system-ui, sans-serif);\n}`;
}

function escapeInlineScriptJson(value: unknown): string {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

/**
 * 将宿主主题变量与 `window.tentacle` 沙箱桥接脚本注入插件 HTML 文档头部。
 */
export function injectBridgeIntoHtml(
  rawHtml: string,
  opts: {
    pluginId: string;
    bridgeId: string;
    themeSnapshot: HostThemeSnapshot;
  },
): string {
  const configJson = escapeInlineScriptJson({
    channel: PLUGIN_UI_CHANNEL,
    pluginId: opts.pluginId,
    bridgeId: opts.bridgeId,
    theme: opts.themeSnapshot.theme,
  });

  const themeStyle = `<style id="__tentacle_host_theme__">\n${buildThemeStyleCss(opts.themeSnapshot)}\n</style>`;
  const bridgeScript = `<script data-tentacle-bridge="1">
(function () {
  var cfg = ${configJson};
  var seq = 0;
  var pending = Object.create(null);
  var themeListeners = [];
  var eventListeners = [];
  var currentTheme = cfg.theme;

  function applyTheme(theme, cssVars) {
    currentTheme = theme === "light" ? "light" : "dark";
    try {
      document.documentElement.dataset.theme = currentTheme;
      document.documentElement.style.colorScheme = currentTheme;
      if (cssVars && typeof cssVars === "object") {
        var keys = Object.keys(cssVars);
        for (var i = 0; i < keys.length; i++) {
          var k = keys[i];
          if (k.indexOf("--t-") === 0 && typeof cssVars[k] === "string") {
            document.documentElement.style.setProperty(k, cssVars[k]);
          }
        }
      }
    } catch (_e) {}
    for (var j = 0; j < themeListeners.length; j++) {
      try { themeListeners[j](currentTheme, cssVars || {}); } catch (_e) {}
    }
  }

  function callHost(action, payload) {
    return new Promise(function (resolve, reject) {
      var id = "req_" + (++seq);
      pending[id] = { resolve: resolve, reject: reject };
      window.parent.postMessage({
        channel: cfg.channel,
        bridgeId: cfg.bridgeId,
        kind: "request",
        id: id,
        action: action,
        payload: payload || {}
      }, "*");
    });
  }

  window.addEventListener("message", function (ev) {
    var msg = ev && ev.data;
    if (!msg || msg.channel !== cfg.channel || msg.bridgeId !== cfg.bridgeId) return;
    if (msg.kind === "response" && msg.id && pending[msg.id]) {
      var slot = pending[msg.id];
      delete pending[msg.id];
      if (msg.ok) {
        slot.resolve(msg.result);
      } else {
        slot.reject(new Error(msg.error || "Tentacle bridge error"));
      }
    } else if (msg.kind === "theme") {
      applyTheme(msg.theme, msg.cssVars);
    } else if (msg.kind === "event") {
      for (var i = 0; i < eventListeners.length; i++) {
        try { eventListeners[i](msg.event, msg.payload); } catch (_e) {}
      }
    }
  });

  document.documentElement.dataset.theme = currentTheme;

  window.tentacle = Object.freeze({
    pluginId: cfg.pluginId,
    get theme() { return currentTheme; },
    fetch: function (path, init) {
      var opts = init && typeof init === "object" ? init : {};
      return callHost("api.fetch", {
        path: String(path || ""),
        method: opts.method ? String(opts.method) : "GET",
        headers: opts.headers && typeof opts.headers === "object" ? opts.headers : undefined,
        body: opts.body
      });
    },
    asset: function (path, opts) {
      var o = opts && typeof opts === "object" ? opts : {};
      return callHost("ui.asset", {
        path: String(path || ""),
        responseType: o.responseType || "text"
      });
    },
    toast: function (message, variant) {
      window.parent.postMessage({
        channel: cfg.channel,
        bridgeId: cfg.bridgeId,
        kind: "notify",
        action: "ui.toast",
        payload: { message: String(message || ""), variant: variant || "info" }
      }, "*");
    },
    askChat: function (prompt) {
      window.parent.postMessage({
        channel: cfg.channel,
        bridgeId: cfg.bridgeId,
        kind: "notify",
        action: "ui.askChat",
        payload: { prompt: String(prompt || "") }
      }, "*");
    },
    openChat: function () {
      window.parent.postMessage({
        channel: cfg.channel,
        bridgeId: cfg.bridgeId,
        kind: "notify",
        action: "ui.openChat",
        payload: {}
      }, "*");
    },
    onThemeChange: function (fn) {
      if (typeof fn !== "function") return function () {};
      themeListeners.push(fn);
      return function () {
        var idx = themeListeners.indexOf(fn);
        if (idx >= 0) themeListeners.splice(idx, 1);
      };
    },
    onEvent: function (fn) {
      if (typeof fn !== "function") return function () {};
      eventListeners.push(fn);
      return function () {
        var idx = eventListeners.indexOf(fn);
        if (idx >= 0) eventListeners.splice(idx, 1);
      };
    }
  });
})();
</script>`;

  const injection = `${themeStyle}\n${bridgeScript}`;
  // 紧跟 CSP meta（或 <head>）之后插入，确保在任何插件脚本执行前就绪。
  const cspMetaMatch = rawHtml.match(/<meta\b[^>]*http-equiv=["']Content-Security-Policy["'][^>]*>/i);
  if (cspMetaMatch && cspMetaMatch.index !== undefined) {
    const pos = cspMetaMatch.index + cspMetaMatch[0].length;
    return `${rawHtml.slice(0, pos)}\n${injection}\n${rawHtml.slice(pos)}`;
  }
  const headMatch = rawHtml.match(/<head\b[^>]*>/i);
  if (headMatch && headMatch.index !== undefined) {
    const pos = headMatch.index + headMatch[0].length;
    return `${rawHtml.slice(0, pos)}\n${injection}\n${rawHtml.slice(pos)}`;
  }
  return `${injection}\n${rawHtml}`;
}

function sanitizeRequestHeaders(raw?: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  if (!raw || typeof raw !== "object") return out;
  for (const [key, val] of Object.entries(raw)) {
    const lower = key.trim().toLowerCase();
    if (!lower || FORBIDDEN_REQUEST_HEADERS.has(lower)) continue;
    if (typeof val === "string") {
      out[key] = val;
    }
  }
  return out;
}

function extractSafeResponseHeaders(headers: Headers): Record<string, string> {
  const out: Record<string, string> = {};
  headers.forEach((value, key) => {
    const lower = key.toLowerCase();
    if (SAFE_RESPONSE_HEADERS.has(lower) || lower.startsWith("x-plugin-")) {
      out[lower] = value;
    }
  });
  return out;
}

export async function executePluginExtFetch(
  pluginId: string,
  req: PluginBridgeFetchRequest,
): Promise<PluginBridgeFetchResponse> {
  const targetUrl = sanitizePluginExtPath(pluginId, req.path);
  const method = String(req.method || "GET").trim().toUpperCase();
  if (!ALLOWED_METHODS.has(method)) {
    throw new Error(`不支持的 HTTP 方法: ${method}`);
  }

  const headers = sanitizeRequestHeaders(req.headers);
  let body: BodyInit | undefined;
  if (req.body !== undefined && req.body !== null && method !== "GET") {
    if (typeof req.body === "string") {
      body = req.body;
    } else {
      body = JSON.stringify(req.body);
      if (!Object.keys(headers).some((k) => k.toLowerCase() === "content-type")) {
        headers["Content-Type"] = "application/json";
      }
    }
  }

  const res = await apiRawFetch(
    targetUrl,
    { method, headers, body },
    { handleAuthFailure: false },
  );

  const text = await res.text();
  const contentType = res.headers.get("content-type") || "";
  let data: unknown = text;
  if (contentType.includes("application/json")) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }

  return {
    ok: res.ok,
    status: res.status,
    statusText: res.statusText,
    headers: extractSafeResponseHeaders(res.headers),
    data,
    text,
  };
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

export async function executePluginAssetFetch(
  pluginId: string,
  req: PluginBridgeAssetRequest,
): Promise<unknown> {
  const targetUrl = sanitizePluginAssetPath(pluginId, req.path);
  const res = await apiRawFetch(targetUrl, { method: "GET" });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`加载资源失败 (${res.status}): ${detail || res.statusText}`);
  }
  const mode = req.responseType || "text";
  if (mode === "json") {
    return await res.json();
  }
  if (mode === "dataUrl") {
    const mime = res.headers.get("content-type") || "application/octet-stream";
    const buf = new Uint8Array(await res.arrayBuffer());
    return `data:${mime};base64,${bytesToBase64(buf)}`;
  }
  return await res.text();
}
