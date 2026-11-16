import { useState, type FormEvent } from "react";
import { KeyRound, ShieldAlert, ShieldCheck, WifiOff } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Input } from "../../components/ui/Input";
import { useAuthStore } from "./authStore";

export function AuthGate() {
  const phase = useAuthStore((s) => s.phase);
  const needPassword = useAuthStore((s) => s.needPassword);
  const needEnroll = useAuthStore((s) => s.needEnroll);
  const freeze = useAuthStore((s) => s.freeze);
  const loginWithPassword = useAuthStore((s) => s.loginWithPassword);
  const enrollDevice = useAuthStore((s) => s.enrollDevice);
  const boot = useAuthStore((s) => s.boot);

  const [password, setPassword] = useState("");
  const [pwError, setPwError] = useState("");
  const [pwSubmitting, setPwSubmitting] = useState(false);

  const [code, setCode] = useState("");
  const [deviceName, setDeviceName] = useState("");
  const [enrollError, setEnrollError] = useState("");
  const [enrollSubmitting, setEnrollSubmitting] = useState(false);

  async function handlePasswordSubmit(e: FormEvent) {
    e.preventDefault();
    setPwError("");
    setPwSubmitting(true);
    try {
      await loginWithPassword(password);
      setPassword("");
    } catch (err) {
      setPwError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setPwSubmitting(false);
    }
  }

  async function handleEnrollSubmit(e: FormEvent) {
    e.preventDefault();
    setEnrollError("");
    setEnrollSubmitting(true);
    try {
      await enrollDevice(code, deviceName);
      setCode("");
      setDeviceName("");
    } catch (err) {
      setEnrollError(err instanceof Error ? err.message : "绑定失败");
    } finally {
      setEnrollSubmitting(false);
    }
  }

  return (
    <>
      {phase === "offline" && (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 border-b border-danger/30 bg-danger-soft px-4 py-2 text-xs text-danger"
        >
          <span className="inline-flex items-center gap-2 font-medium">
            <WifiOff className="h-3.5 w-3.5" />
            无法连接 Tentacle 服务，请确认服务仍在运行。
          </span>
          <Button size="sm" variant="secondary" onClick={() => void boot()}>
            重试连接
          </Button>
        </div>
      )}

      <Dialog
        open={phase === "auth_required" && (needPassword || needEnroll)}
        onOpenChange={() => {}}
        hideClose
        preventClose
        title="Tentacle 访问验证"
        description="完成身份或设备验证后进入工作台。"
      >
        <div data-testid="auth-dialog" className="space-y-5">
          {needEnroll && (
            <form data-testid="enroll-form" onSubmit={handleEnrollSubmit} className="space-y-3 rounded-xl border border-border bg-elevated p-4">
              <div className="flex items-center gap-2 text-sm font-semibold text-fg">
                {freeze ? (
                  <ShieldAlert className="h-4 w-4 text-danger" />
                ) : (
                  <ShieldCheck className="h-4 w-4 text-accent" />
                )}
                <span>设备绑定</span>
              </div>
              {freeze && (
                <p className="rounded-lg border border-danger/30 bg-danger-soft p-2.5 text-xs text-danger">
                  设备验证已被主机脚本紧急冻结，冻结前绑定的设备已全部失效，请用新绑定码重新绑定。
                </p>
              )}
              <div className="space-y-2">
                <label htmlFor="enroll-code" className="block text-xs text-fg-dim">
                  主机绑定码
                </label>
                <Input
                  id="enroll-code"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  placeholder="输入主机脚本 gen-code 生成的绑定码"
                  autoComplete="one-time-code"
                  autoFocus={needEnroll}
                />
              </div>
              <div className="space-y-2">
                <label htmlFor="enroll-name" className="block text-xs text-fg-dim">
                  设备名称（可选）
                </label>
                <Input
                  id="enroll-name"
                  value={deviceName}
                  onChange={(e) => setDeviceName(e.target.value)}
                  placeholder="例如：我的笔记本"
                />
              </div>
              {enrollError && (
                <p role="alert" className="text-xs text-danger">
                  {enrollError}
                </p>
              )}
              <Button type="submit" variant="primary" className="w-full" disabled={enrollSubmitting}>
                {enrollSubmitting ? "正在绑定…" : "绑定当前设备"}
              </Button>
            </form>
          )}

          {needPassword && (
            <form data-testid="login-form" onSubmit={handlePasswordSubmit} className="space-y-3 rounded-xl border border-border bg-elevated p-4">
              <div className="flex items-center gap-2 text-sm font-semibold text-fg">
                <KeyRound className="h-4 w-4 text-accent" />
                <span>密码登录</span>
              </div>
              <div className="space-y-2">
                <label htmlFor="login-password" className="block text-xs text-fg-dim">
                  访问密码
                </label>
                <Input
                  id="login-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="输入访问密码"
                  autoComplete="current-password"
                  autoFocus={!needEnroll}
                />
              </div>
              {pwError && (
                <p role="alert" className="text-xs text-danger">
                  {pwError}
                </p>
              )}
              <Button type="submit" variant="primary" className="w-full" disabled={pwSubmitting} data-testid="login-submit">
                {pwSubmitting ? "正在验证…" : "登录工作台"}
              </Button>
            </form>
          )}
        </div>
      </Dialog>
    </>
  );
}
