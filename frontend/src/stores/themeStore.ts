import { create } from "zustand";

export type ThemePreference = "dark" | "light" | "system";
export type ResolvedTheme = "dark" | "light";

const STORAGE_KEY = "tentacle_theme";

function readInitialPreference(): ThemePreference {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === "dark" || raw === "light" || raw === "system") {
      return raw;
    }
  } catch {
    // Ignore storage restriction.
  }
  return "dark";
}

function resolveTheme(pref: ThemePreference): ResolvedTheme {
  if (pref !== "system") return pref;
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  return "dark";
}

function applyDomTheme(resolved: ResolvedTheme): void {
  if (typeof document !== "undefined") {
    document.documentElement.dataset.theme = resolved;
  }
}

const initialPref = readInitialPreference();
const initialResolved = resolveTheme(initialPref);
applyDomTheme(initialResolved);

interface ThemeState {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (pref: ThemePreference) => void;
  toggleTheme: () => void;
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  preference: initialPref,
  resolved: initialResolved,
  setPreference: (pref) => {
    const resolved = resolveTheme(pref);
    applyDomTheme(resolved);
    try {
      localStorage.setItem(STORAGE_KEY, pref);
    } catch {
      // Ignore storage error.
    }
    set({ preference: pref, resolved });
  },
  toggleTheme: () => {
    const next: ThemePreference = get().resolved === "dark" ? "light" : "dark";
    get().setPreference(next);
  },
}));

if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
  const mql = window.matchMedia("(prefers-color-scheme: light)");
  const handler = () => {
    const { preference } = useThemeStore.getState();
    if (preference === "system") {
      const resolved = resolveTheme("system");
      applyDomTheme(resolved);
      useThemeStore.setState({ resolved });
    }
  };
  mql.addEventListener?.("change", handler);
}
