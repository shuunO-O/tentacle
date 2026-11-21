"""Web API 与 WebSocket 的 Pydantic 数据模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FlexibleModel(BaseModel):
    """允许保留扩展字段并向后兼容测试替身返回体。"""

    model_config = ConfigDict(extra="allow")


# ---------- 请求模型 ----------


class DedupStartBody(BaseModel):
    top_k: int = Field(default=5, ge=1, le=20)
    max_pairs: int = Field(default=200, ge=1, le=1000)


class DedupDecisionBody(BaseModel):
    action: str
    keep_id: str | None = None
    policy: str = "reject"


class ConceptDetachBody(BaseModel):
    concept_id: str = Field(min_length=1, max_length=500)


class ChatSend(BaseModel):
    text: str
    platform: str | None = None
    images: list[str] | None = Field(default=None, max_length=10)  # 兼容旧客户端
    files: list[str] | None = Field(default=None, max_length=10)  # 图片/音频/视频 local_id
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_meta: dict[str, Any] | None = None


class CommandBody(BaseModel):
    text: str
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class ChatStop(BaseModel):
    round_id: str | None = Field(default=None, min_length=1, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)


class LoginBody(BaseModel):
    password: str


class SetPasswordBody(BaseModel):
    password: str


class EnrollBody(BaseModel):
    code: str
    name: str = ""


class DeviceAuthBody(BaseModel):
    enabled: bool
    code: str = ""  # 当前设备未绑定时，无论是否设置密码，开启设备验证都需主机绑定码
    name: str = ""


class NewDevicesBody(BaseModel):
    enabled: bool


class SettingsBody(BaseModel):
    show_reasoning: bool | None = None
    split_messages: bool | None = None


class BackupRestoreBody(BaseModel):
    path: str
    timestamp: str | None = None


class GoalCreateBody(BaseModel):
    description: str
    priority: str


class GoalUpdateBody(BaseModel):
    description: str | None = None
    priority: str | None = None
    result: str | None = None
    status: str | None = None


class TaskCreateBody(BaseModel):
    goal_id: int
    content: str
    urgency: str


class TaskUpdateBody(BaseModel):
    content: str | None = None
    urgency: str | None = None
    status: str | None = None
    goal_id: int | None = None


# ---------- 响应模型 ----------


class OkResponse(FlexibleModel):
    ok: bool = True


class AuthStateResponse(FlexibleModel):
    has_password: bool
    device_auth: bool
    device_ok: bool
    freeze: bool


class DeviceEntryDto(FlexibleModel):
    id: str | None = None
    name: str | None = None
    created_at: float | None = None
    last_seen: float | None = None


class PendingCodeInfo(FlexibleModel):
    expires_at: float | None = None
    remaining_seconds: int | None = None


class AuthDevicesResponse(FlexibleModel):
    devices: list[dict[str, Any]]
    device_auth: bool
    device_bound: bool
    allow_new_devices: bool
    has_password: bool
    freeze: bool
    pending_code: dict[str, Any] | None = None


class DeviceAuthResponse(FlexibleModel):
    ok: bool = True
    device_auth: bool


class NewDevicesResponse(FlexibleModel):
    ok: bool = True
    allow_new_devices: bool


class LoginResponse(FlexibleModel):
    token: str
    has_password: bool


class SetPasswordResponse(FlexibleModel):
    ok: bool = True
    has_password: bool


class WebSettingsDto(FlexibleModel):
    agent_name: str | None = None
    show_reasoning: bool | None = None
    split_messages: bool | None = None
    has_password: bool | None = None
    device_auth: bool | None = None


class CommandOutputDto(FlexibleModel):
    text: str
    refresh_history: bool = True


class ChatSendResponse(FlexibleModel):
    ok: bool
    total_tokens: int = 0
    round: dict[str, Any] | None = None
    command: CommandOutputDto | None = None
    text: str | None = None
    detail: str | None = None


class ChatStopResponse(FlexibleModel):
    accepted: bool | None = None
    round_id: str | None = None
    request_id: str | None = None
    status: str | None = None


class ChatCommandDto(FlexibleModel):
    name: str | None = None
    description: str | None = None
    usage: str | None = None


class ChatCommandsResponse(FlexibleModel):
    commands: list[dict[str, Any]]


class ChatMessageDto(FlexibleModel):
    id: int | str | None = None
    round_id: str | None = None
    role: str
    content: str
    platform: str | None = None
    created_time: float | str | None = None
    segments: list[str] | None = None
    images: list[dict[str, Any]] | None = None
    interrupted: bool | None = None


class ChatHistoryResponse(FlexibleModel):
    messages: list[dict[str, Any]]
    has_more: bool


class ChatSyncResponse(FlexibleModel):
    messages: list[dict[str, Any]]
    has_more: bool
    instance_id: str
    seq: int
    rounds: list[dict[str, Any]]
    events: list[dict[str, Any]]
    finished: list[dict[str, Any]]


class StatusTokensDto(FlexibleModel):
    accumulated: int = 0


class HeartbeatStatusDto(FlexibleModel):
    interval_seconds: float | int | None = None
    next_beat_at: float | None = None


class StatusSnapshotResponse(FlexibleModel):
    agent_name: str | None = None
    session: dict[str, Any] | None = None
    tokens: StatusTokensDto
    usage: dict[str, Any]
    heartbeat: HeartbeatStatusDto
    db: dict[str, int]
    connections: int
    time: float


class MemorySummaryResponse(FlexibleModel):
    ok: bool
    data: dict[str, Any]
    error: str | None = None


class MemoryGraphDataDto(FlexibleModel):
    nodes: list[dict[str, Any]]
    relationships: list[dict[str, Any]]


class MemoryGraphResponse(FlexibleModel):
    ok: bool
    data: MemoryGraphDataDto
    error: str | None = None


class ConceptDetachResponse(FlexibleModel):
    concept_id: str | None = None
    group_id: str | None = None
    previous_group_id: str | None = None


class DedupStatusResponse(FlexibleModel):
    run: dict[str, Any] | None = None
    counts: dict[str, int] | None = None
    pairs: list[dict[str, Any]] | None = None
    offset: int | None = None


class DedupAuditResponse(FlexibleModel):
    entries: list[dict[str, Any]] | None = None


class DedupStartResponse(FlexibleModel):
    run_id: str


class DedupDecisionResponse(FlexibleModel):
    state: str
    result: dict[str, Any] | None = None


class UsageStatsResponse(FlexibleModel):
    series: list[dict[str, Any]] | None = None
    totals: dict[str, Any] | None = None
    breakdown: list[dict[str, Any]] | None = None


class BalancesResponse(FlexibleModel):
    balances: list[dict[str, Any]] | None = None


class FileRecordDto(FlexibleModel):
    local_id: str
    filename: str
    mime: str
    size: int | None = None
    kind: str | None = None
    path: str | None = None
    file_id: str | None = None


class FileUploadResponse(FlexibleModel):
    ok: bool = True
    file: dict[str, Any]


class FileListResponse(FlexibleModel):
    files: list[dict[str, Any]]


class TerminalHistoryEntryDto(FlexibleModel):
    ts: float
    content: str


class TerminalHistoryResponse(FlexibleModel):
    ok: bool = True
    entries: list[TerminalHistoryEntryDto]


class AdminTableInfoDto(FlexibleModel):
    name: str
    count: int


class AdminTablesResponse(FlexibleModel):
    tables: list[AdminTableInfoDto]


class AdminTableRowsResponse(FlexibleModel):
    columns: list[str]
    rows: list[list[Any]]
    total: int | None = None


class GoalManagerOpResponse(FlexibleModel):
    status: str | None = None
    results: list[dict[str, Any]] | None = None


class AdminBackupsResponse(FlexibleModel):
    items: list[dict[str, Any]]


class AdminBackupRestoreResponse(FlexibleModel):
    ok: bool = True


# ---------- 子模块路由响应模型 ----------


class ConfigurationSnapshotResponse(FlexibleModel):
    revision: str | None = None
    fields: list[dict[str, Any]] | None = None
    entities: dict[str, Any] | None = None


class ConfigurationActionResponse(FlexibleModel):
    status: dict[str, Any] | None = None
    applied: bool | None = None
    equivalent: bool | None = None
    changes: list[dict[str, Any]] | None = None


class ConfigurationRestartResponse(FlexibleModel):
    pending: bool | None = None
    reembed: bool | None = None
    reembedding: bool | None = None
    state: str | None = None


class ConfigurationDiscoverResponse(FlexibleModel):
    models: list[dict[str, Any]] | None = None
    provider: str | None = None


class PluginDecisionBody(BaseModel):
    revision: str | None = None
    enabled: bool
    trusted: bool | None = None
    config: dict[str, Any] | None = None
    disabled_tools: list[str] | None = None


class McpServerUpsertBody(BaseModel):
    revision: str | None = None
    config: dict[str, Any]


class PluginRestartBody(BaseModel):
    revision: str = Field(min_length=64, max_length=64)


class PluginEnvWriteResponse(FlexibleModel):
    ok: bool
    name: str
    configured: bool


class PluginsSnapshotResponse(FlexibleModel):
    revision: str
    plugins: list[dict[str, Any]]
    mcp_servers: list[dict[str, Any]]
    permission_catalog: dict[str, dict[str, str]]
    restart_required: bool
    restart: dict[str, Any]


class PluginUiItemDto(FlexibleModel):
    plugin_id: str
    name: str
    version: str = ""
    title: str
    description: str = ""
    placement: Literal["sidebar", "plugins_only"] = "sidebar"
    has_routes: bool = False


class PluginUiListResponse(FlexibleModel):
    items: list[PluginUiItemDto] = []


class PluginUiDocumentResponse(FlexibleModel):
    plugin_id: str
    name: str
    version: str = ""
    title: str
    description: str = ""
    placement: Literal["sidebar", "plugins_only"] = "sidebar"
    has_routes: bool = False
    html: str


class SubagentListResponse(FlexibleModel):
    enabled: bool
    tasks: list[dict[str, Any]]
    has_more: bool | None = None
    execution_blocked: bool | None = None
    coding_enabled: bool | None = None


class SubagentDetailResponse(FlexibleModel):
    enabled: bool
    task: dict[str, Any]
    turns: list[dict[str, Any]]
    has_more: bool | None = None
    next_before: int | None = None


class DshSessionsResponse(FlexibleModel):
    sessions: list[dict[str, Any]]


class DshAttachResponse(FlexibleModel):
    task_id: str
    status: str
    dsh_session_id: str | None = None
    workspace: str | None = None


class DshAccessResponse(FlexibleModel):
    url: str


class MaintenanceOperationDto(FlexibleModel):
    id: str | None = None
    action: str | None = None
    version: str | None = None
    state: str | None = None
    error: str | None = None


class MaintenanceStatusResponse(FlexibleModel):
    available: bool | None = None
    version: dict[str, Any] | None = None
    check: dict[str, Any] | None = None
    can_update: bool | None = None
    operation: dict[str, Any] | None = None
    web_allowed: bool | None = None


# ---------- WebSocket 统一事件模型 ----------


class WsSubagentUpdatedData(FlexibleModel):
    event: str | None = None
    task_id: str | None = None
    enabled: bool | None = None
    execution_blocked: bool | None = None
    coding_enabled: bool | None = None
    tasks: list[dict[str, Any]] | None = None
    has_more: bool | None = None


class WsDedupProgressData(FlexibleModel):
    event: str | None = None
    run: dict[str, Any] | None = None
    counts: dict[str, int] | None = None


class WsMaintenanceUpdatedData(FlexibleModel):
    available: bool | None = None
    version: dict[str, Any] | None = None
    check: dict[str, Any] | None = None
    can_update: bool | None = None
    operation: dict[str, Any] | None = None
    web_allowed: bool | None = None


class WsEventEnvelope(FlexibleModel):
    type: Literal[
        "hello",
        "settings",
        "thinking",
        "thinking_delta",
        "message",
        "message_delta",
        "message_seg_end",
        "tool_call",
        "tool_result",
        "web_search",
        "usage",
        "final",
        "round_finished",
        "error",
        "terminal_data",
        "subagent.updated",
        "dedup.progress",
        "maintenance.updated",
        "status.snapshot",
        "plugin.ui_event",
    ]
    data: Any = None
    round: dict[str, Any] | None = None
    chat_seq: int | None = None
    chat_instance: str | None = None
    ts: float | None = None


# ---------- 定期备份与迁移导入/导出模型 ----------


class BackupPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=720)
    retention_count: int | None = Field(default=None, ge=1, le=100)
    include_files: bool | None = None


class BackupNowBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_files: bool | None = None
    scopes: list[str] | None = Field(default=None, max_length=16)
    workspace_paths: list[str] | None = Field(default=None, max_length=500)


class BackupApplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    import_token: str = Field(min_length=32, max_length=32)
    apply_files: bool = True
    apply_env: bool = False
    apply_workspace: bool = True
    create_pre_backup: bool = True


class BackupWorkspaceEntryDto(FlexibleModel):
    path: str
    name: str
    is_dir: bool
    size: int = 0
    mtime: float = 0.0


class BackupWorkspaceTreeResponse(FlexibleModel):
    exists: bool
    current_path: str
    parent_path: str | None = None
    entries: list[BackupWorkspaceEntryDto] = []
    truncated: bool = False


class BackupPolicyDto(FlexibleModel):
    enabled: bool
    interval_hours: int
    retention_count: int
    include_files: bool
    last_auto_backup_at: float | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_backup_id: str | None = None


class BackupItemDto(FlexibleModel):
    backup_id: str
    kind: str
    created_at: str
    created_ts: float
    data_epoch: int
    included_scopes: list[str] = []
    includes_files: bool
    includes_env: bool = False
    includes_workspace: bool = False
    workspace_paths: list[str] = []
    workspace_file_count: int = 0
    workspace_bytes: int = 0
    file_count: int
    payload_bytes: int
    archive_bytes: int
    archive_sha256: str = ""
    required_env_vars: list[str] = []
    configured_env_vars: list[str] = []


class BackupLastRestoreDto(FlexibleModel):
    status: str
    applied_at: float
    backup_id: str = ""
    pre_backup_id: str = ""
    applied_scopes: list[str] = []
    applied_env: bool = False
    applied_files: bool = False
    applied_workspace: bool = False
    missing_env_vars: list[str] = []
    error: str | None = None


class BackupStatusResponse(FlexibleModel):
    policy: BackupPolicyDto
    next_run_at: float | None = None
    running: bool
    running_op: str | None = None
    backups: list[BackupItemDto]
    total_bytes: int
    data_epoch: int
    required_env_vars: list[str] = []
    missing_env_vars: list[str] = []
    last_restore: BackupLastRestoreDto | None = None
    env_export_allowed: bool = False
    restart_available: bool = False
    instance_id: str | None = None
    restarting: bool = False


class BackupPolicyUpdateResponse(FlexibleModel):
    ok: bool = True
    policy: BackupPolicyDto
    next_run_at: float | None = None


class BackupCreateResponse(FlexibleModel):
    ok: bool = True
    backup: BackupItemDto


class BackupImportInspectResponse(FlexibleModel):
    import_token: str
    source_type: str
    backup_id: str = ""
    backup_created_at: str = ""
    data_epoch: int
    encrypted: bool = False
    included_scopes: list[str] = []
    includes_files: bool = False
    includes_env: bool = False
    env_key_count: int = 0
    has_files: bool = False
    files_count: int = 0
    files_bytes: int = 0
    has_workspace: bool = False
    workspace_files_count: int = 0
    workspace_bytes: int = 0
    workspace_sample_paths: list[str] = []
    file_count: int = 0
    payload_bytes: int = 0
    modules: list[str] = []
    required_env_vars: list[str] = []
    missing_if_keep_env: list[str] = []
    missing_if_apply_env: list[str] = []


class BackupImportApplyResponse(FlexibleModel):
    ok: bool = True
    state: str
    instance_id: str | None = None
    pre_backup_id: str = ""
    apply_env: bool = False
    apply_files: bool = True
    apply_workspace: bool = True
