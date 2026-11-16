import type { ConfigField, ConfigFieldGroup, ConfigSectionId } from "./types";

export const GENERAL_SECTIONS: ReadonlyArray<{ id: ConfigSectionId; label: string }> = [
  { id: "models", label: "模型与连接" },
  { id: "memory", label: "记忆系统" },
  { id: "runtime", label: "运行与调度" },
  { id: "channels", label: "通道与媒体" },
];

export function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function isInheritSentinel(value: unknown): boolean {
  return Boolean(value && typeof value === "object" && "$inherit" in (value as Record<string, unknown>));
}

export function fieldSection(path: string): ConfigSectionId {
  if (/^(providers|models|roles)\./.test(path)) return "models";
  if (path.startsWith("memory.") || path.startsWith("concepts.") || path.startsWith("embeddings.")) {
    return "memory";
  }
  if (
    path.startsWith("webui.") ||
    path.startsWith("web_tools.") ||
    path.startsWith("media_inputs.") ||
    path.startsWith("attachments.") ||
    path.startsWith("files.") ||
    path.startsWith("web.")
  ) {
    return "channels";
  }
  return "runtime";
}

export function isFullWidthField(field: ConfigField): boolean {
  const leaf = extractModelKey(field.path);
  return (
    field.kind === "tools" ||
    field.kind === "env" ||
    field.kind === "url" ||
    field.kind === "catalog_url" ||
    field.kind === "proxy" ||
    leaf === "name" ||
    Boolean(field.multiline)
  );
}

const GCP_KEYS = new Set(["project", "project_env", "location", "credentials_env", "gcs_base_url"]);
const EXT_PROVIDER_KEYS = new Set(["models_url", "balance_mode", "balance_url"]);
const CAPABILITY_KEYS = new Set(["vision", "audio", "video", "stream"]);
const AUTH_KEYS = new Set(["auth", "api_key_env"]);
const CONNECTION_BASIC_KEYS = new Set(["type", "label", "provider", "name", "api_style", "base_url", "proxy"]);
const ROLE_BINDING_KEYS = new Set(["label", "model", "max_iterations", "interactions_stateful"]);

function extractModelKey(path: string): string {
  const parts = path.split(".");
  if (parts.length >= 4 && (parts[2] === "defaults" || parts[2] === "overrides")) {
    return parts.slice(3).join(".");
  }
  return parts.slice(2).join(".");
}

function anyTouched(fields: ConfigField[], draft: Record<string, unknown>): boolean {
  return fields.some(
    (f) =>
      Object.prototype.hasOwnProperty.call(draft, f.path) ||
      Boolean(f.pending) ||
      (f.value !== null && f.value !== "" && !sameValue(f.value, f.default)),
  );
}

function groupModelEntityFields(
  fields: ConfigField[],
  effectiveAuth: string | undefined,
  draft: Record<string, unknown>,
  forceExpand: boolean,
): ConfigFieldGroup[] {
  const isProvider = fields.some((f) => f.path.startsWith("providers."));
  const isRole = fields.some((f) => f.path.startsWith("roles."));

  const buckets: Record<string, ConfigField[]> = {
    basic: [],
    binding: [],
    auth: [],
    caps: [],
    gen: [],
    tools: [],
    ext: [],
    gcp: [],
  };

  for (const field of fields) {
    const key = extractModelKey(field.path);
    if (key === "tools.enabled") {
      buckets.tools!.push(field);
    } else if (GCP_KEYS.has(key)) {
      buckets.gcp!.push(field);
    } else if (isProvider && EXT_PROVIDER_KEYS.has(key)) {
      buckets.ext!.push(field);
    } else if (isProvider && AUTH_KEYS.has(key)) {
      buckets.auth!.push(field);
    } else if (!isProvider && AUTH_KEYS.has(key)) {
      buckets.auth!.push(field);
    } else if (isRole && ROLE_BINDING_KEYS.has(key)) {
      buckets.binding!.push(field);
    } else if (CAPABILITY_KEYS.has(key)) {
      buckets.caps!.push(field);
    } else if (CONNECTION_BASIC_KEYS.has(key)) {
      buckets.basic!.push(field);
    } else {
      buckets.gen!.push(field);
    }
  }

  const groups: ConfigFieldGroup[] = [];
  if (buckets.binding!.length > 0) {
    groups.push({
      id: "entity-binding",
      title: "角色绑定与限制",
      subtitle: "选择该角色实际调用的模型及工具执行上限",
      fields: buckets.binding!,
    });
  }
  if (buckets.basic!.length > 0) {
    groups.push({
      id: "entity-basic",
      title: isProvider ? "基础连接" : "基础连接与模型",
      subtitle: isProvider ? "接入类型、服务端点及网络代理" : "所属连接、模型标识与协议",
      fields: buckets.basic!,
    });
  }
  if (buckets.auth!.length > 0) {
    groups.push({
      id: "entity-auth",
      title: "认证与密钥环境变量",
      subtitle: "密钥只保存环境变量名称，实际值可安全写入应用目录 .env",
      fields: buckets.auth!,
    });
  }
  if (buckets.caps!.length > 0) {
    groups.push({
      id: "entity-caps",
      title: isRole ? "能力开关覆盖" : "多模态与输出能力",
      subtitle: isRole ? "留作继承时跟随模型默认开关" : "控制模型是否启用图片/音视频与流式输出",
      fields: buckets.caps!,
    });
  }
  if (buckets.gen!.length > 0) {
    groups.push({
      id: "entity-gen",
      title: isRole ? "生成与推理参数覆盖" : "生成与推理参数",
      subtitle: isRole ? "仅在需要为该角色单独覆盖模型默认值时调整" : "温度、思考强度、上下文窗口与重试策略",
      fields: buckets.gen!,
    });
  }
  if (buckets.tools!.length > 0) {
    groups.push({
      id: "entity-tools",
      title: "可用工具",
      subtitle: "限定该模型/角色在对话回合中可见的工具集合",
      fields: buckets.tools!,
    });
  }
  if (buckets.ext!.length > 0) {
    const hasCustom = anyTouched(buckets.ext!, draft);
    groups.push({
      id: "entity-ext",
      title: "扩展接口（模型目录与余额）",
      subtitle: "自定义模型列表路径及余额查询方式",
      fields: buckets.ext!,
      collapsible: true,
      defaultCollapsed: !forceExpand && !hasCustom,
    });
  }
  if (buckets.gcp!.length > 0) {
    const needsGcp = effectiveAuth === "adc" || anyTouched(buckets.gcp!, draft);
    groups.push({
      id: "entity-gcp",
      title: "Google Cloud / Vertex 专用参数",
      subtitle: "仅使用 ADC / Vertex AI 或独立 GCS 存储桶时需要配置",
      fields: buckets.gcp!,
      collapsible: true,
      defaultCollapsed: !forceExpand && !needsGcp,
    });
  }
  return groups;
}

function fieldSubgroupDescriptor(
  section: Exclude<ConfigSectionId, "models">,
  path: string,
): { id: string; title: string; subtitle: string; order: number } {
  if (section === "memory") {
    if (path.startsWith("memory.concept_alignment.")) {
      return {
        id: "memory-align",
        title: "新记忆自动去重（JEV）",
        subtitle: "写入新记忆时判断概念是否归并已有等价组",
        order: 2,
      };
    }
    if (path.startsWith("memory.concept_dedup.")) {
      return {
        id: "memory-dedup",
        title: "历史概念扫描（JEV）",
        subtitle: "记忆去重页面执行历史扫描时使用的评分模型与阈值",
        order: 3,
      };
    }
    if (path.startsWith("memory.embedding.") || path.startsWith("memory.search.")) {
      return {
        id: "memory-retrieval",
        title: "向量与记忆检索",
        subtitle: "控制向量维度、事件原文返还策略与检索候选条数",
        order: 1,
      };
    }
    return {
      id: "memory-other",
      title: "其他记忆设置",
      subtitle: "通用记忆与概念图谱配置",
      order: 4,
    };
  }

  if (section === "channels") {
    if (path.startsWith("webui.") || path.startsWith("web.")) {
      return {
        id: "channels-webui",
        title: "Web 工作台服务",
        subtitle: "HTTP 监听地址与端口设置（更改后需重启生效）",
        order: 1,
      };
    }
    if (path.startsWith("web_tools.")) {
      return {
        id: "channels-webtools",
        title: "公开网页工具",
        subtitle: "网页搜索与抓取工具的超时、输出长度及密钥环境变量",
        order: 2,
      };
    }
    return {
      id: "channels-media",
      title: "媒体输入与视频下载",
      subtitle: "B 站视频自动获取及本地/远程下载后端配置",
      order: 3,
    };
  }

  // runtime
  if (path.startsWith("app.")) {
    return {
      id: "runtime-app",
      title: "基础与身份",
      subtitle: "程序全局时区、个人称呼与 Agent 名字",
      order: 1,
    };
  }
  if (path.startsWith("heartbeat.session_switch.")) {
    return {
      id: "runtime-session-switch",
      title: "每日 Session 切换",
      subtitle: "指定时段内写完近况交接后自动开启新会话",
      order: 3,
    };
  }
  if (path.startsWith("heartbeat.")) {
    return {
      id: "runtime-heartbeat",
      title: "定时心跳",
      subtitle: "周期性自主唤醒间隔、普通心跳内容、消费层级与超时",
      order: 2,
    };
  }
  if (path.startsWith("scheduler.")) {
    return {
      id: "runtime-scheduler",
      title: "定时任务调度",
      subtitle: "服务短暂下线后单次及间隔任务的错过补跑宽限期",
      order: 4,
    };
  }
  return {
    id: "runtime-ops",
    title: "子 Agent、日志与用量",
    subtitle: "外部编码子 Agent 开关、日志级别与余额查询连接",
    order: 5,
  };
}

export function groupFieldsForDisplay(opts: {
  section: ConfigSectionId;
  fields: ConfigField[];
  draft: Record<string, unknown>;
  forceExpand?: boolean;
  focusedModelPrefix?: string;
}): ConfigFieldGroup[] {
  const { section, fields, draft, forceExpand = false, focusedModelPrefix } = opts;
  if (fields.length === 0) return [];

  if (section === "models") {
    const effectiveValue = (f: ConfigField): unknown =>
      Object.prototype.hasOwnProperty.call(draft, f.path) ? draft[f.path] : f.value;

    const inferAuth = (list: ConfigField[]): string | undefined => {
      const authField = list.find((f) => extractModelKey(f.path) === "auth");
      const val = authField ? effectiveValue(authField) : undefined;
      return typeof val === "string" ? val : undefined;
    };

    if (focusedModelPrefix) {
      return groupModelEntityFields(fields, inferAuth(fields), draft, forceExpand);
    }

    // Multiple models / entities (e.g., global search)
    const byOwner = new Map<string, { label: string; fields: ConfigField[] }>();
    for (const field of fields) {
      const parts = field.path.split(".");
      const ownerId = parts.length >= 2 ? `${parts[0]}.${parts[1]}` : field.path;
      const existing = byOwner.get(ownerId);
      if (existing) {
        existing.fields.push(field);
      } else {
        byOwner.set(ownerId, {
          label: field.group || ownerId,
          fields: [field],
        });
      }
    }

    if (byOwner.size === 1) {
      const only = Array.from(byOwner.values())[0]!;
      return groupModelEntityFields(only.fields, inferAuth(only.fields), draft, forceExpand);
    }

    const result: ConfigFieldGroup[] = [];
    for (const [ownerId, entry] of byOwner.entries()) {
      const subGroups = groupModelEntityFields(entry.fields, inferAuth(entry.fields), draft, forceExpand);
      for (const sg of subGroups) {
        result.push({
          ...sg,
          id: `${ownerId}:${sg.id}`,
          title: `${entry.label} · ${sg.title}`,
        });
      }
    }
    return result;
  }

  const map = new Map<string, { order: number; group: ConfigFieldGroup }>();
  for (const field of fields) {
    const desc = fieldSubgroupDescriptor(section, field.path);
    const existing = map.get(desc.id);
    if (existing) {
      existing.group.fields.push(field);
    } else {
      map.set(desc.id, {
        order: desc.order,
        group: {
          id: desc.id,
          title: desc.title,
          subtitle: desc.subtitle,
          fields: [field],
        },
      });
    }
  }
  return Array.from(map.values())
    .sort((a, b) => a.order - b.order)
    .map((entry) => entry.group);
}

export function listSectionSubgroups(
  section: Exclude<ConfigSectionId, "models">,
  fields: ConfigField[],
  draft: Record<string, unknown> = {},
): Array<{ id: string; title: string; count: number; dirtyCount: number }> {
  const byId = new Map<
    string,
    { id: string; title: string; order: number; count: number; dirtyCount: number }
  >();
  for (const field of fields) {
    const desc = fieldSubgroupDescriptor(section, field.path);
    const isDirty = Object.prototype.hasOwnProperty.call(draft, field.path);
    const cur = byId.get(desc.id);
    if (cur) {
      cur.count += 1;
      if (isDirty) cur.dirtyCount += 1;
    } else {
      byId.set(desc.id, {
        id: desc.id,
        title: desc.title,
        order: desc.order,
        count: 1,
        dirtyCount: isDirty ? 1 : 0,
      });
    }
  }
  return Array.from(byId.values())
    .sort((a, b) => a.order - b.order)
    .map(({ id, title, count, dirtyCount }) => ({ id, title, count, dirtyCount }));
}

export function fieldMatchesSubgroup(
  section: Exclude<ConfigSectionId, "models">,
  path: string,
  subgroupId: string,
): boolean {
  return fieldSubgroupDescriptor(section, path).id === subgroupId;
}
