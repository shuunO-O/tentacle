import * as RadixTabs from "@radix-ui/react-tabs";
import type { ReactNode } from "react";
import { cn } from "./Button";

export const Tabs = RadixTabs.Root;
export const TabsContent = RadixTabs.Content;

export function TabsList({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <RadixTabs.List
      className={cn(
        "inline-flex flex-wrap items-center gap-1 rounded-xl border border-border-soft bg-elevated p-1",
        className,
      )}
    >
      {children}
    </RadixTabs.List>
  );
}

export function TabsTrigger({
  value,
  children,
  className,
}: {
  value: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <RadixTabs.Trigger
      value={value}
      className={cn(
        "rounded-lg px-3 py-1.5 text-xs font-medium text-fg-dim transition-colors cursor-pointer",
        "hover:text-fg data-[state=active]:bg-surface data-[state=active]:text-fg data-[state=active]:shadow-xs",
        className,
      )}
    >
      {children}
    </RadixTabs.Trigger>
  );
}
