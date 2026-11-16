import type { HTMLAttributes } from "react";
import { cn } from "./Button";

export type BadgeVariant = "default" | "accent" | "ok" | "warn" | "danger" | "info";

const BADGE_CLASSES: Record<BadgeVariant, string> = {
  default: "bg-elevated text-fg-dim border-border",
  accent: "bg-accent-soft text-accent border-accent/30",
  ok: "bg-ok-soft text-ok border-ok/30",
  warn: "bg-warn-soft text-warn border-warn/30",
  danger: "bg-danger-soft text-danger border-danger/30",
  info: "bg-info-soft text-info border-info/30",
};

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
}

export function Badge({ className, variant = "default", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-tight",
        BADGE_CLASSES[variant],
        className,
      )}
      {...props}
    />
  );
}
