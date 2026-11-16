import { forwardRef, type InputHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { cn } from "./Button";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "w-full rounded-lg border border-border bg-input px-3 py-2 text-sm text-fg",
        "placeholder:text-fg-faint transition-colors",
        "focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/25",
        "disabled:opacity-50",
        className,
      )}
      {...props}
    />
  ),
);

Input.displayName = "Input";

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        "w-full rounded-lg border border-border bg-input px-3 py-2 text-sm text-fg",
        "placeholder:text-fg-faint transition-colors",
        "focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/25",
        "disabled:opacity-50",
        className,
      )}
      {...props}
    />
  ),
);

Textarea.displayName = "Textarea";
