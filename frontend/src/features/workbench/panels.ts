import { useEffect, useState } from "react";
import {
  Activity,
  BarChart3,
  Bot,
  Database,
  GitMerge,
  MessageSquare,
  Network,
  Plug,
  Settings,
  SlidersHorizontal,
  Terminal,
  type LucideIcon,
} from "lucide-react";

export type PanelId =
  | "chat"
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

export type PanelGroup = "chat" | "memory" | "system";

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

export function getPanelDef(id: PanelId): PanelDefinition {
  return PANEL_MAP.get(id) ?? PANELS[0]!;
}

export function panelFromPathname(pathname: string): PanelId {
  const clean = pathname.replace(/\/+$/, "") || "/";
  if (clean === "/" || clean === "/chat") return "chat";
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
