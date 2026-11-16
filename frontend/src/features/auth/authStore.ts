import { create } from "zustand";
import {
  apiFetch,
  clearStoredToken,
  getStoredToken,
  setStoredToken,
  setUnauthorizedHandler,
} from "../../api/client";
import type { components } from "../../api/schema";
import { wsClient } from "../../api/ws";

export type AuthStateDto = components["schemas"]["AuthStateResponse"];
export type WebSettingsDto = components["schemas"]["WebSettingsDto"];

export type AuthPhase =
  | "booting"
  | "offline"
  | "auth_required"
  | "authenticated";

interface AuthStoreState {
  phase: AuthPhase;
  needPassword: boolean;
  needEnroll: boolean;
  freeze: boolean;
  hasPassword: boolean;
  deviceAuth: boolean;
  boot: () => Promise<WebSettingsDto | null>;
  handleUnauthorized: () => Promise<void>;
  loginWithPassword: (password: string) => Promise<void>;
  enrollDevice: (code: string, name: string) => Promise<void>;
  logout: () => void;
}

async function fetchAuthState(): Promise<AuthStateDto> {
  const res = await fetch("/api/auth/state");
  if (!res.ok) {
    throw new Error("auth state 请求失败");
  }
  return (await res.json()) as AuthStateDto;
}

export const useAuthStore = create<AuthStoreState>((set, get) => ({
  phase: "booting",
  needPassword: false,
  needEnroll: false,
  freeze: false,
  hasPassword: false,
  deviceAuth: false,

  boot: async () => {
    let authState: AuthStateDto;
    try {
      authState = await fetchAuthState();
    } catch {
      wsClient.disconnect();
      set({ phase: "offline" });
      return null;
    }

    const token = getStoredToken();
    const needEnroll = Boolean(authState.device_auth && !authState.device_ok);
    const needPassword = Boolean(authState.has_password && !token);

    if (needEnroll || needPassword) {
      wsClient.disconnect();
      set({
        phase: "auth_required",
        needEnroll,
        needPassword,
        freeze: Boolean(authState.freeze),
        hasPassword: Boolean(authState.has_password),
        deviceAuth: Boolean(authState.device_auth),
      });
      return null;
    }

    try {
      const settings = await apiFetch<WebSettingsDto>("/api/settings");
      set({
        phase: "authenticated",
        needEnroll: false,
        needPassword: false,
        freeze: Boolean(authState.freeze),
        hasPassword: Boolean(settings.has_password ?? authState.has_password),
        deviceAuth: Boolean(settings.device_auth ?? authState.device_auth),
      });
      wsClient.connect();
      return settings;
    } catch (err) {
      if ((err as { status?: number })?.status === 401) {
        return null;
      }
      wsClient.disconnect();
      set({ phase: "offline" });
      return null;
    }
  },

  handleUnauthorized: async () => {
    wsClient.disconnect();
    clearStoredToken();
    try {
      const authState = await fetchAuthState();
      const needEnroll = Boolean(authState.device_auth && !authState.device_ok);
      const needPassword = Boolean(authState.has_password);
      if (!needEnroll && !needPassword) {
        await get().boot();
        return;
      }
      set({
        phase: "auth_required",
        needEnroll,
        needPassword,
        freeze: Boolean(authState.freeze),
        hasPassword: Boolean(authState.has_password),
        deviceAuth: Boolean(authState.device_auth),
      });
    } catch {
      set({ phase: "offline" });
    }
  },

  loginWithPassword: async (password: string) => {
    const res = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (!res.ok) {
      const payload = (await res.json().catch(() => ({}))) as { detail?: string };
      throw new Error(payload.detail || "密码错误");
    }
    const data = (await res.json()) as { token?: string };
    setStoredToken(data.token || "");
    await get().boot();
  },

  enrollDevice: async (code: string, name: string) => {
    const trimmed = code.trim();
    if (!trimmed) {
      throw new Error("请输入主机脚本 gen-code 生成的绑定码");
    }
    const res = await fetch("/api/auth/enroll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: trimmed, name: name.trim() }),
    });
    if (!res.ok) {
      const payload = (await res.json().catch(() => ({}))) as { detail?: string };
      throw new Error(payload.detail || "绑定失败");
    }
    await get().boot();
  },

  logout: () => {
    wsClient.disconnect();
    clearStoredToken();
    void get().boot();
  },
}));

setUnauthorizedHandler(() => useAuthStore.getState().handleUnauthorized());
wsClient.setUnauthorizedCallback(() => {
  void useAuthStore.getState().handleUnauthorized();
});
