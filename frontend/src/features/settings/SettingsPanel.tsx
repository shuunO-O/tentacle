import { useCallback, useEffect, useState, type FormEvent } from "react";
import { KeyRound, MonitorSmartphone, Palette, Settings, ShieldAlert, Trash2 } from "lucide-react";
import { apiFetch, clearStoredToken } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button, cn } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { Switch } from "../../components/ui/Switch";
import { toast } from "../../components/ui/Toast";
import { useThemeStore, type ThemePreference } from "../../stores/themeStore";
import { useAuthStore } from "../auth/authStore";
import { useChatStore } from "../chat/chatStore";
import { BackupSection } from "./BackupSection";

interface DeviceItem {
  id: string;
  name?: string;
  created_at?: number;
  last_used?: number;
  user_agent?: string;
}

interface DevicesPayload {
  devices: DeviceItem[];
  device_auth: boolean;
  device_bound: boolean;
  allow_new_devices: boolean;
  has_password: boolean;
  freeze: boolean;
  pending_code?: { expires_at?: number } | null;
}

export function SettingsPanel() {
  const { preference, setPreference } = useThemeStore();
  const bootAuth = useAuthStore((s) => s.boot);
  const showReasoning = useChatStore((s) => Boolean(s.settings.show_reasoning));
  const splitMessages = useChatStore((s) => Boolean(s.settings.split_messages));
  const applySettings = useChatStore((s) => s.applySettings);

  const [devicesInfo, setDevicesInfo] = useState<DevicesPayload | null>(null);
  const [savingPrefs, setSavingPrefs] = useState(false);

  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [savingPassword, setSavingPassword] = useState(false);

  const [enrollCode, setEnrollCode] = useState("");
  const [enrollName, setEnrollName] = useState("");
  const [togglingDevices, setTogglingDevices] = useState(false);

  const loadDevices = useCallback(async () => {
    try {
      const res = await apiFetch<DevicesPayload>("/api/auth/devices");
      setDevicesInfo(res);
    } catch (err) {
      toast(err instanceof Error ? err.message : "读取设备状态失败", "error");
    }
  }, []);

  useEffect(() => {
    void loadDevices();
  }, [loadDevices]);

  const handleToggleWebSetting = async (patch: {
    show_reasoning?: boolean;
    split_messages?: boolean;
  }) => {
    setSavingPrefs(true);
    try {
      const res = await apiFetch<{ show_reasoning: boolean; split_messages: boolean }>(
        "/api/settings",
        {
          method: "PUT",
          body: JSON.stringify({
            show_reasoning: patch.show_reasoning ?? showReasoning,
            split_messages: patch.split_messages ?? splitMessages,
          }),
        },
      );
      applySettings(res);
      toast("界面偏好已同步保存", "ok");
    } catch (err) {
      toast(err instanceof Error ? err.message : "更新设置失败", "error");
    } finally {
      setSavingPrefs(false);
    }
  };

  const handleSetPassword = async (e: FormEvent) => {
    e.preventDefault();
    if (newPassword && newPassword !== confirmPassword) {
      toast("两次输入的新密码不一致", "error");
      return;
    }
    setSavingPassword(true);
    try {
      const res = await apiFetch<{ ok: boolean; has_password: boolean }>(
        "/api/auth/set_password",
        {
          method: "POST",
          body: JSON.stringify({ password: newPassword }),
        },
      );
      setNewPassword("");
      setConfirmPassword("");
      toast(
        res.has_password ? "访问密码已更新，请重新登录确认" : "访问密码已清除",
        "ok",
      );
      clearStoredToken();
      await bootAuth();
    } catch (err) {
      toast(err instanceof Error ? err.message : "保存密码失败", "error");
    } finally {
      setSavingPassword(false);
    }
  };

  const handleToggleDeviceAuth = async (enabled: boolean) => {
    setTogglingDevices(true);
    try {
      await apiFetch("/api/auth/device_auth", {
        method: "PUT",
        body: JSON.stringify({
          enabled,
          ...(enrollCode.trim() ? { code: enrollCode.trim() } : {}),
          ...(enrollName.trim() ? { name: enrollName.trim() } : {}),
        }),
      });
      setEnrollCode("");
      setEnrollName("");
      toast(enabled ? "设备验证已开启" : "设备验证已关闭", "ok");
      await loadDevices();
      await bootAuth();
    } catch (err) {
      toast(err instanceof Error ? err.message : "切换设备验证失败", "error");
    } finally {
      setTogglingDevices(false);
    }
  };

  const handleToggleNewDevices = async (enabled: boolean) => {
    setTogglingDevices(true);
    try {
      await apiFetch("/api/auth/new_devices", {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      });
      toast(enabled ? "已允许新设备绑定" : "已关闭新设备入口", "ok");
      await loadDevices();
    } catch (err) {
      toast(err instanceof Error ? err.message : "更新新设备入口失败", "error");
    } finally {
      setTogglingDevices(false);
    }
  };

  const handleRevokeDevice = async (deviceId: string) => {
    try {
      await apiFetch(`/api/auth/devices/${encodeURIComponent(deviceId)}`, {
        method: "DELETE",
      });
      toast("已吊销该设备", "ok");
      await loadDevices();
    } catch (err) {
      toast(err instanceof Error ? err.message : "吊销设备失败", "error");
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-bg">
      <div className="flex shrink-0 items-center gap-2 border-b border-border bg-surface px-4 py-2.5">
        <Settings className="h-4 w-4 text-accent" />
        <h2 className="text-sm font-semibold text-fg">系统偏好与安全设置</h2>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <div className="mx-auto max-w-3xl space-y-5">
          {/* Theme */}
          <section className="rounded-2xl border border-border bg-surface p-4">
            <div className="mb-3 flex items-center gap-2">
              <Palette className="h-4 w-4 text-accent" />
              <h3 className="text-sm font-semibold text-fg">外观主题</h3>
            </div>
            <div className="grid grid-cols-3 gap-2">
              {(
                [
                  { id: "dark", label: "深色工作台", desc: "默认低眩光炭黑" },
                  { id: "light", label: "浅色日间", desc: "高可读温白纸张" },
                  { id: "system", label: "跟随系统", desc: "自动响应 OS 外观" },
                ] as Array<{ id: ThemePreference; label: string; desc: string }>
              ).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => setPreference(item.id)}
                  className={cn(
                    "rounded-xl border p-3 text-left transition-colors cursor-pointer",
                    preference === item.id
                      ? "border-accent bg-accent-soft/60 text-fg"
                      : "border-border-soft bg-elevated/50 text-fg-dim hover:text-fg",
                  )}
                >
                  <div className="text-xs font-semibold text-fg">{item.label}</div>
                  <div className="mt-0.5 text-[11px] text-fg-faint">{item.desc}</div>
                </button>
              ))}
            </div>
          </section>

          {/* Chat display settings */}
          <section className="space-y-3 rounded-2xl border border-border bg-surface p-4">
            <h3 className="text-sm font-semibold text-fg">对话显示选项（服务端同步）</h3>
            <div className="flex items-center justify-between gap-4 border-b border-border-soft pb-3">
              <div>
                <div className="text-xs font-medium text-fg">显示模型思考过程</div>
                <div className="text-[11px] text-fg-faint">
                  在回复消息中折叠展示 reasoning_content 推理链
                </div>
              </div>
              <Switch
                checked={showReasoning}
                disabled={savingPrefs}
                onCheckedChange={(v) => void handleToggleWebSetting({ show_reasoning: v })}
                aria-label="显示模型思考过程"
              />
            </div>
            <div className="flex items-center justify-between gap-4">
              <div>
                <div className="text-xs font-medium text-fg">按自然分段拆分消息</div>
                <div className="text-[11px] text-fg-faint">
                  模型多段回复时拆分为更轻量的气泡节拍
                </div>
              </div>
              <Switch
                checked={splitMessages}
                disabled={savingPrefs}
                onCheckedChange={(v) => void handleToggleWebSetting({ split_messages: v })}
                aria-label="按自然分段拆分消息"
              />
            </div>
          </section>

          {/* Password */}
          <section className="rounded-2xl border border-border bg-surface p-4">
            <div className="mb-3 flex items-center gap-2">
              <KeyRound className="h-4 w-4 text-accent" />
              <h3 className="text-sm font-semibold text-fg">Web 访问密码</h3>
              {devicesInfo && (
                <Badge variant={devicesInfo.has_password ? "ok" : "warn"}>
                  {devicesInfo.has_password ? "已设置密码" : "未设置密码"}
                </Badge>
              )}
            </div>

            <form onSubmit={(e) => void handleSetPassword(e)} className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <div>
                  <label className="mb-1 block text-xs text-fg-dim">新密码（留空提交则清除密码）</label>
                  <Input
                    type="password"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="输入新密码…"
                    autoComplete="new-password"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-xs text-fg-dim">确认密码</label>
                  <Input
                    type="password"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="再次输入新密码…"
                    autoComplete="new-password"
                  />
                </div>
              </div>
              <div className="flex justify-end">
                <Button type="submit" size="sm" variant="primary" disabled={savingPassword}>
                  {savingPassword ? "提交中…" : "更新密码"}
                </Button>
              </div>
            </form>
          </section>

          {/* Device Binding */}
          <section className="space-y-3 rounded-2xl border border-border bg-surface p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <MonitorSmartphone className="h-4 w-4 text-accent" />
                <h3 className="text-sm font-semibold text-fg">受信任设备与硬件鉴权</h3>
              </div>
              {devicesInfo?.freeze && (
                <Badge variant="danger">
                  <ShieldAlert className="h-3 w-3" />
                  已冻结
                </Badge>
              )}
            </div>

            {devicesInfo && (
              <>
                <div className="flex items-center justify-between gap-4 border-b border-border-soft pb-3">
                  <div>
                    <div className="text-xs font-medium text-fg">开启设备 Cookie 验证</div>
                    <div className="text-[11px] text-fg-faint">
                      {devicesInfo.device_bound
                        ? "当前浏览器已持有受信任设备凭据"
                        : "若尚未绑定当前设备，请先输入主机生成的一次性绑定码再开启"}
                    </div>
                  </div>
                  <Switch
                    checked={devicesInfo.device_auth}
                    disabled={togglingDevices || devicesInfo.freeze}
                    onCheckedChange={(v) => void handleToggleDeviceAuth(v)}
                    aria-label="开启设备 Cookie 验证"
                  />
                </div>

                {!devicesInfo.device_bound && (
                  <div className="grid gap-2 rounded-xl border border-border-soft bg-elevated/50 p-3 sm:grid-cols-2">
                    <Input
                      value={enrollCode}
                      onChange={(e) => setEnrollCode(e.target.value)}
                      placeholder="一次性绑定码（主机 gen-code）"
                      className="h-8 text-xs font-mono"
                    />
                    <Input
                      value={enrollName}
                      onChange={(e) => setEnrollName(e.target.value)}
                      placeholder="当前设备备注（如 办公室浏览器）"
                      className="h-8 text-xs"
                    />
                  </div>
                )}

                <div className="flex items-center justify-between gap-4">
                  <div>
                    <div className="text-xs font-medium text-fg">允许新设备绑定</div>
                    <div className="text-[11px] text-fg-faint">
                      可在绑定常用设备后从此处关闭；重新开放须在主机运行 allow-new
                    </div>
                  </div>
                  <Switch
                    checked={devicesInfo.allow_new_devices}
                    disabled={togglingDevices}
                    onCheckedChange={(v) => void handleToggleNewDevices(v)}
                    aria-label="允许新设备绑定"
                  />
                </div>

                <div className="space-y-1.5 pt-2">
                  <div className="text-xs font-medium text-fg-dim">
                    已绑定设备 ({devicesInfo.devices.length})
                  </div>
                  {devicesInfo.devices.length === 0 ? (
                    <p className="text-xs text-fg-faint">暂无已绑定设备记录。</p>
                  ) : (
                    devicesInfo.devices.map((dev) => (
                      <div
                        key={dev.id}
                        className="flex items-center justify-between gap-3 rounded-xl border border-border-soft bg-elevated/40 px-3 py-2 text-xs"
                      >
                        <div className="min-w-0">
                          <div className="font-medium text-fg">{dev.name || "未命名设备"}</div>
                          <div className="truncate font-mono text-[10px] text-fg-faint">
                            {dev.id}
                            {dev.created_at
                              ? ` · 创建于 ${new Date(dev.created_at * 1000).toLocaleDateString()}`
                              : ""}
                          </div>
                        </div>
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => void handleRevokeDevice(dev.id)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          吊销
                        </Button>
                      </div>
                    ))
                  )}
                </div>
              </>
            )}
          </section>

          {/* Backups & Migration */}
          <BackupSection
            authSignature={`${Boolean(devicesInfo?.has_password)}:${Boolean(devicesInfo?.device_auth)}`}
            onRestored={() => void loadDevices()}
          />
        </div>
      </div>
    </div>
  );
}
