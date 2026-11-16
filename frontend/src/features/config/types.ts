export interface ConfigField {
  path: string;
  label: string;
  kind: string;
  default: unknown;
  value: unknown;
  active?: unknown;
  pending?: boolean;
  effect?: "immediate" | "round" | "restart";
  group?: string;
  help?: string;
  description?: string;
  choices?: string[];
  nullable?: boolean;
  multiline?: boolean;
  minimum?: number | null;
  maximum?: number | null;
  inheritable?: boolean;
  source?: string;
  protected?: boolean;
  configured?: boolean;
}

export interface ConfigEntityItem {
  id: string;
  label?: string;
  type?: string;
  provider?: string;
  model?: string;
  name?: string;
  selectable?: boolean;
  references?: unknown;
}

export interface ProviderTypeSpec {
  label: string;
  auth: "api_key" | "adc" | string;
  base_url: string;
  api_style: string;
  protocols: string[];
}

export interface ConfigDiffItem {
  path: string;
  label: string;
  before: unknown;
  after: unknown;
  effect?: string;
}

export interface DiscoveredModel {
  import_id: string;
  name: string;
  context_window?: number;
  configured?: boolean;
}

export interface ConfigSnapshot {
  revision: string;
  active_role: string;
  active_model?: {
    name?: string;
    provider?: string;
    model_id?: string | null;
    provider_id?: string | null;
    api_style?: string;
    auth?: string;
  };
  applying?: boolean;
  error?: string | null;
  restart_required?: boolean;
  restart?: {
    available?: boolean;
    state?: string;
    reembedding?: boolean;
    last_reembed?: { message?: string };
    instance_id?: string;
  };
  fields: ConfigField[];
  entities?: {
    providers?: ConfigEntityItem[];
    models?: ConfigEntityItem[];
    roles?: ConfigEntityItem[];
  };
  provider_types?: Record<string, ProviderTypeSpec>;
  tools?: Record<string, string[]>;
  credentials?: Record<string, { auth: string; configured: boolean }>;
  connection_status?: Record<string, { auth: string; configured: boolean }>;
  history?: Array<{ id: string; created_at: number }>;
}

export type ConfigSectionId = "models" | "memory" | "runtime" | "channels";

export interface ConfigFieldGroup {
  id: string;
  title: string;
  subtitle?: string;
  fields: ConfigField[];
  collapsible?: boolean;
  defaultCollapsed?: boolean;
}
