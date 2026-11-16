import { useId } from "react";
import { KeyRound } from "lucide-react";
import { Badge } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { Input, Textarea } from "../../components/ui/Input";
import { Switch } from "../../components/ui/Switch";
import { isFullWidthField, isInheritSentinel } from "./configGrouping";
import type { ConfigField, ConfigSnapshot } from "./types";

interface ConfigFieldControlProps {
  field: ConfigField;
  effectiveValue: unknown;
  isDirty: boolean;
  snapshot: ConfigSnapshot | null;
  onChange: (value: unknown) => void;
  onOpenSecretModal: (target: { path: string; envName: string }) => void;
}

export function ConfigFieldControl({
  field,
  effectiveValue,
  isDirty,
  snapshot,
  onChange,
  onOpenSecretModal,
}: ConfigFieldControlProps) {
  const uid = useId();
  const inputId = `cfg-${field.path}-${uid}`;
  const fullWidth = isFullWidthField(field);
  const inheriting = isInheritSentinel(effectiveValue);
  const value = inheriting ? (field.default ?? field.value) : effectiveValue;

  const roleName = field.path.startsWith("roles.") ? field.path.split(".")[1] : undefined;
  const toolNames =
    field.kind === "tools" && roleName
      ? Array.from(
          new Set([
            ...(snapshot?.tools?.[roleName] || []),
            ...(Array.isArray(value) ? (value as string[]) : []),
          ]),
        ).sort()
      : [];

  const helpText = field.help || field.description;

  return (
    <div
      className={cn(
        "min-w-0 rounded-xl p-2.5 text-xs transition-colors",
        fullWidth && "md:col-span-2",
        isDirty
          ? "bg-accent-soft/45 ring-1 ring-accent/45"
          : "bg-elevated/35 ring-1 ring-border-soft/70",
      )}
    >
      <div className="mb-1.5 flex flex-wrap items-start justify-between gap-1.5">
        <div className="min-w-0 flex-1">
          <label
            htmlFor={inputId}
            className="block text-[13px] font-medium leading-snug text-fg"
          >
            {field.label}
          </label>
          <div
            className="truncate font-mono text-[10px] text-fg-faint"
            title={field.path}
          >
            {field.path}
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-1">
          {isDirty && (
            <Badge variant="warn" className="text-[10px]">
              {inheriting ? "待继承默认" : "已修改"}
            </Badge>
          )}
          {field.pending && <Badge variant="warn">待生效</Badge>}
          {field.effect === "restart" && <Badge variant="default">需重启</Badge>}
          {field.effect === "round" && <Badge variant="info">下回合生效</Badge>}
          {field.inheritable && (
            <>
              <Badge
                variant={inheriting || (field.source === "default" && !isDirty) ? "default" : "info"}
              >
                {inheriting || (field.source === "default" && !isDirty) ? "继承模型" : "已覆盖"}
              </Badge>
              <Button
                size="sm"
                variant="ghost"
                className="h-6 px-2 text-[11px]"
                onClick={() =>
                  onChange(field.source === "file" ? { $inherit: true } : field.value)
                }
              >
                继承模型默认值
              </Button>
            </>
          )}
        </div>
      </div>

      {field.kind === "tools" ? (
        <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
          {toolNames.map((t) => {
            const list = Array.isArray(value) ? (value as string[]) : [];
            const checked = list.includes(t);
            return (
              <label
                key={t}
                className="flex cursor-pointer items-center gap-2 rounded-lg border border-border-soft bg-surface/80 px-2.5 py-1.5 hover:border-border"
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => {
                    const next = checked ? list.filter((x) => x !== t) : [...list, t];
                    onChange(next);
                  }}
                  className="accent-accent"
                />
                <span className="truncate font-mono text-[11px] text-fg">{t}</span>
              </label>
            );
          })}
        </div>
      ) : field.choices && field.choices.length > 0 ? (
        <select
          id={inputId}
          aria-label={field.label}
          value={value === null ? "__null__" : String(value ?? "")}
          disabled={Boolean(field.protected)}
          onChange={(e) => onChange(e.target.value === "__null__" ? null : e.target.value)}
          className="h-8 w-full rounded-lg border border-border bg-input px-2.5 text-xs text-fg focus:border-accent focus:outline-none"
        >
          {field.nullable && <option value="__null__">不指定</option>}
          {field.choices.map((c) => (
            <option key={c} value={c}>
              {field.path.endsWith(".type")
                ? snapshot?.provider_types?.[c]?.label || c
                : c}
            </option>
          ))}
        </select>
      ) : field.kind === "boolean" ? (
        <div className="flex h-8 items-center gap-2.5">
          <Switch
            checked={value === true}
            disabled={Boolean(field.protected)}
            onCheckedChange={(v) => onChange(v)}
            aria-label={field.label}
          />
          <span className="text-xs text-fg-dim">{value === true ? "已开启" : "已关闭"}</span>
        </div>
      ) : field.multiline ? (
        <Textarea
          id={inputId}
          rows={3}
          value={String(value ?? "")}
          disabled={Boolean(field.protected) || value === null}
          onChange={(e) => onChange(e.target.value)}
          className="text-xs"
        />
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Input
            id={inputId}
            type={field.kind === "number" || field.kind === "integer" ? "number" : "text"}
            step={field.kind === "integer" ? "1" : "any"}
            min={field.minimum ?? undefined}
            max={field.maximum ?? undefined}
            disabled={Boolean(field.protected) || value === null}
            value={value === null ? "" : String(value ?? "")}
            onChange={(e) => {
              if (field.kind === "number" || field.kind === "integer") {
                onChange(e.target.value === "" ? "" : Number(e.target.value));
              } else {
                onChange(e.target.value);
              }
            }}
            className="h-8 min-w-36 flex-1 text-xs"
          />

          {field.kind === "env" && (
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant={field.configured ? "ok" : "warn"}>
                {field.configured ? "环境变量已配置" : "未设置值"}
              </Badge>
              {typeof value === "string" && value.trim() && (
                <Button
                  size="sm"
                  variant="secondary"
                  className="h-8"
                  onClick={() =>
                    onOpenSecretModal({ path: field.path, envName: value.trim() })
                  }
                >
                  <KeyRound className="h-3.5 w-3.5" />
                  写入 .env 值
                </Button>
              )}
            </div>
          )}
        </div>
      )}

      {field.nullable && field.kind !== "boolean" && !field.choices?.length && (
        <label className="mt-1.5 inline-flex cursor-pointer items-center gap-1.5 text-[11px] text-fg-dim">
          <input
            type="checkbox"
            checked={value === null}
            onChange={(e) =>
              onChange(
                e.target.checked
                  ? null
                  : field.kind === "number" || field.kind === "integer"
                    ? 0
                    : "",
              )
            }
            className="accent-accent"
          />
          <span>{field.kind === "proxy" ? "继承系统环境代理" : "不指定（使用默认行为）"}</span>
        </label>
      )}

      {helpText && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-fg-dim">{helpText}</p>
      )}
    </div>
  );
}
