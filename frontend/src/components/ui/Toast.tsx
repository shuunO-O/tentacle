import { create } from "zustand";
import { X } from "lucide-react";
import { cn } from "./Button";

export type ToastKind = "info" | "ok" | "error";

export interface ToastEntry {
  id: string;
  kind: ToastKind;
  message: string;
}

interface ToastState {
  items: ToastEntry[];
  push: (message: string, kind?: ToastKind) => void;
  dismiss: (id: string) => void;
}

export const useToastStore = create<ToastState>((set) => ({
  items: [],
  push: (message, kind = "info") => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    set((state) => ({ items: [...state.items.slice(-4), { id, kind, message }] }));
    window.setTimeout(() => {
      set((state) => ({ items: state.items.filter((item) => item.id !== id) }));
    }, 4500);
  },
  dismiss: (id) => set((state) => ({ items: state.items.filter((item) => item.id !== id) })),
}));

export function toast(message: string, kind: ToastKind = "info"): void {
  useToastStore.getState().push(message, kind);
}

export function ToastViewport() {
  const items = useToastStore((s) => s.items);
  const dismiss = useToastStore((s) => s.dismiss);
  if (!items.length) return null;

  return (
    <div
      className="pointer-events-none fixed bottom-4 right-4 z-[60] flex max-w-sm flex-col gap-2"
      aria-live="polite"
    >
      {items.map((item) => (
        <div
          key={item.id}
          role="status"
          className={cn(
            "pointer-events-auto flex items-start justify-between gap-3 rounded-xl border px-3.5 py-2.5 text-xs shadow-xl",
            item.kind === "error" && "border-danger/40 bg-surface text-danger",
            item.kind === "ok" && "border-ok/40 bg-surface text-ok",
            item.kind === "info" && "border-border bg-surface text-fg",
          )}
        >
          <span className="leading-relaxed">{item.message}</span>
          <button
            type="button"
            onClick={() => dismiss(item.id)}
            aria-label="关闭提示"
            className="mt-0.5 rounded p-0.5 text-fg-dim hover:bg-hover hover:text-fg"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        </div>
      ))}
    </div>
  );
}
