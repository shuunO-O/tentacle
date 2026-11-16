import * as RadixDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "./Button";

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title?: string;
  description?: string;
  children: ReactNode;
  className?: string;
  hideClose?: boolean;
  preventClose?: boolean;
}

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  className,
  hideClose,
  preventClose,
}: DialogProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={preventClose ? undefined : onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-[2px]" />
        <RadixDialog.Content
          onEscapeKeyDown={preventClose ? (e) => e.preventDefault() : undefined}
          onPointerDownOutside={preventClose ? (e) => e.preventDefault() : undefined}
          className={cn(
            "fixed left-1/2 top-1/2 z-50 w-[min(92vw,480px)] -translate-x-1/2 -translate-y-1/2",
            "rounded-2xl border border-border bg-surface p-6 text-fg shadow-2xl focus:outline-none",
            className,
          )}
        >
          {(title || !hideClose) && (
            <div className="mb-3 flex items-start justify-between gap-4">
              <div>
                {title && (
                  <RadixDialog.Title className="text-base font-semibold text-fg">
                    {title}
                  </RadixDialog.Title>
                )}
                {description && (
                  <RadixDialog.Description className="mt-1 text-xs text-fg-dim">
                    {description}
                  </RadixDialog.Description>
                )}
              </div>
              {!hideClose && !preventClose && (
                <RadixDialog.Close
                  className="rounded-lg p-1 text-fg-dim hover:bg-hover hover:text-fg"
                  aria-label="关闭"
                >
                  <X className="h-4 w-4" />
                </RadixDialog.Close>
              )}
            </div>
          )}
          {children}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  );
}
