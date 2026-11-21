import { useEffect, useState } from "react";
import {
  Activity,
  AppWindow,
  BarChart3,
  Bot,
  Database,
  GitMerge,
  Globe,
  MessageSquare,
  Network,
  Plug,
  Settings,
  SlidersHorizontal,
  Terminal,
  type LucideIcon,
} from "lucide-react";

export type BuiltinPanelId =
  | "chat"
  | "browser"
  | "graph"
  | "config"
  | "plugins"
  | "subagents"
  | "dedup"
  | "admin"
  | "usage"
  | "status"
  | "terminal"
  | "settings";

export type PluginPanelId = `plugin:${string}`;

export type PanelId = BuiltinPanelId | PluginPanelId;

export type PanelGroup = "chat" | "memory" | "system" | "plugin_ui";

export interface PluginUiEntry {
  plugin_id: string;
  name: string;
  version: string;
  title: string;
  description: string;
  placement: "sidebar" | "plugins_only";
  has_routes: boolean;
}

export interface BrowserLiveViewSummary {
  live_view_url: string;
  cdp_configured: boolean;
  mode: "cdp" | "local";
}

const PLUGIN_ID_RE = /^[a-z][a-z0-9_-]{0,47}$/;

export function pluginPanelId(pluginId: string): PluginPanelId {
  return `plugin:${pluginId}`;
}

export function isPluginPanelId(id: PanelId): id is PluginPanelId {
  return typeof id === "string" && id.startsWith("plugin:");
}

export function pluginIdFromPanelId(id: PanelId): string | null {
  if (!isPluginPanelId(id)) return null;
  const pid = id.slice("plugin:".length);
  return PLUGIN_ID_RE.test(pid) ? pid : null;
}

export interface PanelDefinition {
  id: PanelId;
  group: PanelGroup;
  label: string;
  route: string;
  description: string;
  icon: LucideIcon;
  pinned?: boolean;
}

export const PANEL_GROUPS: { id: PanelGroup; label: string }[] = [
  { id: "chat", label: "对话" },
  { id: "memory", label: "记忆" },
  { id: "system", label: "系统" },
];

export const PANELS: PanelDefinition[] = [
  {
    id: "chat",
    group: "chat",
    label: "对话",
    route: "/",
    description: "与主 Agent 对话、执行指令与查看工具过程",
    icon: MessageSquare,
    pinned: true,
  },
  {
    id: "subagents",
    group: "chat",
    label: "子代理",
    route: "/subagents",
    description: "异步后台任务、实时推送与编码工作区",
    icon: Bot,
  },
  {
    id: "browser",
    group: "chat",
    label: "可视化浏览器",
    route: "/browser",
    description: "内嵌或新标签打开 Neko 等可视化浏览器画面进行人工接管",
    icon: Globe,
  },
  {
    id: "graph",
    group: "memory",
    label: "记忆图谱",
    route: "/graph",
    description: "浏览器原生交互式概念与事件关系图谱",
    icon: Network,
  },
  {
    id: "admin",
    group: "memory",
    label: "记忆管理",
    route: "/admin",
    description: "实体、事件、反思、目标、文件与备份管理",
    icon: Database,
  },
  {
    id: "dedup",
    group: "memory",
    label: "实体去重",
    route: "/dedup",
    description: "扫描与裁决重复概念实体并同步记忆库",
    icon: GitMerge,
  },
  {
    id: "usage",
    group: "system",
    label: "用量统计",
    route: "/usage",
    description: "模型 Token 消耗统计与趋势图表",
    icon: BarChart3,
  },
  {
    id: "config",
    group: "system",
    label: "系统配置",
    route: "/config",
    description: "供应商、模型参数、记忆策略与环境变量",
    icon: SlidersHorizontal,
  },
  {
    id: "plugins",
    group: "system",
    label: "插件扩展",
    route: "/plugins",
    description: "本地插件审核、权限门控、环境变量与 MCP 服务管理",
    icon: Plug,
  },
  {
    id: "terminal",
    group: "system",
    label: "终端",
    route: "/terminal",
    description: "主会话标准输入输出终端视图",
    icon: Terminal,
  },
  {
    id: "status",
    group: "system",
    label: "运行状态",
    route: "/status",
    description: "运行状态、心跳计划与在线更新维护",
    icon: Activity,
  },
  {
    id: "settings",
    group: "system",
    label: "界面设置",
    route: "/settings",
    description: "显示偏好、访问密码与设备绑定管理",
    icon: Settings,
  },
];

const PANEL_MAP = new Map<PanelId, PanelDefinition>(PANELS.map((p) => [p.id, p]));

export function buildPluginPanelDef(entry: PluginUiEntry): PanelDefinition {
  return {
    id: pluginPanelId(entry.plugin_id),
    group: "plugin_ui",
    label: entry.title || entry.name || entry.plugin_id,
    route: `/plugin/${entry.plugin_id}`,
    description: entry.description || `${entry.name || entry.plugin_id} 自定义沙箱页面`,
    icon: AppWindow,
  };
}

export function getPanelDef(id: PanelId, pluginUis: PluginUiEntry[] = []): PanelDefinition {
  const builtin = PANEL_MAP.get(id);
  if (builtin) return builtin;
  const pid = pluginIdFromPanelId(id);
  if (pid) {
    const matched = pluginUis.find((item) => item.plugin_id === pid);
    if (matched) return buildPluginPanelDef(matched);
    return {
      id,
      group: "plugin_ui",
      label: pid,
      route: `/plugin/${pid}`,
      description: "插件自定义沙箱页面",
      icon: AppWindow,
    };
  }
  return PANELS[0]!;
}

export function panelFromPathname(pathname: string): PanelId {
  const clean = pathname.replace(/\/+$/, "") || "/";
  if (clean === "/" || clean === "/chat") return "chat";
  const pluginMatch = clean.match(/^\/plugin\/([a-z][a-z0-9_-]{0,47})$/);
  if (pluginMatch && pluginMatch[1]) {
    return pluginPanelId(pluginMatch[1]);
  }
  const matched = PANELS.find((p) => p.route !== "/" && p.route === clean);
  return matched ? matched.id : "chat";
}

export const COMPACT_BREAKPOINT_PX = 860;

export function useIsCompactLayout(): boolean {
  const [compact, setCompact] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.innerWidth <= COMPACT_BREAKPOINT_PX;
  });

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mql = window.matchMedia(`(max-width: ${COMPACT_BREAKPOINT_PX}px)`);
    if (!mql) return;
    const update = () => setCompact(Boolean(mql.matches));
    update();
    mql.addEventListener?.("change", update);
    return () => mql.removeEventListener?.("change", update);
  }, []);

  return compact;
}
