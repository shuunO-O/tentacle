import * as RadixSwitch from "@radix-ui/react-switch";
import { cn } from "./Button";

export interface SwitchProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  className?: string;
}

export function Switch({
  checked,
  onCheckedChange,
  disabled,
  id,
  "aria-label": ariaLabel,
  className,
}: SwitchProps) {
  // Wrapper establishes a `relative` containing block so Radix's form BubbleInput
  // (`position: absolute`) stays scoped to the control instead of overflowing `documentElement`.
  return (
    <span className="relative inline-flex shrink-0 items-center">
      <RadixSwitch.Root
        id={id}
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        aria-label={ariaLabel}
        className={cn(
          "relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full border border-border",
          "bg-input transition-colors data-[state=checked]:bg-accent data-[state=checked]:border-accent",
          "disabled:cursor-not-allowed disabled:opacity-50",
          className,
        )}
      >
        <RadixSwitch.Thumb
          className={cn(
            "block h-4 w-4 translate-x-1 rounded-full bg-fg shadow-xs transition-transform",
            "data-[state=checked]:translate-x-5.5 data-[state=checked]:bg-accent-fg",
          )}
        />
      </RadixSwitch.Root>
    </span>
  );
}
